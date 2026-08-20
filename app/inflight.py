"""Live request feed — what is running, what is queued for a slot, what just ran.

The Routing tab already answers *how many* slots each provider has in use; this
answers the next question: **which** requests those are, and what is stacked up
behind them. `slots.py` keeps only a per-provider counter, and its queue lives
inside an `asyncio.Condition` whose waiters are opaque from the outside — so a
request stuck waiting for capacity is invisible. Registering every proxied
request on arrival makes both sets enumerable.

A finished request is not dropped: it is frozen and moved to the front of a
`_recent` ring buffer, so the console shows a rolling feed (newest first) rather
than a view that empties whenever the proxy goes idle. That history is
deliberately in-memory and lost on restart, exactly like the log ring buffer —
the durable copy of every request is its `event=request` log line. Size:
`INFLIGHT_HISTORY`.

State is process-local, exactly like `slots._in_use`, `registry._down_until` and
the log ring buffer: correct because the app runs a single uvicorn worker.

**Observation only**, with one deliberate exception (`Entry.cancel`, driven from
the admin API — never from the request path). Nothing in the request path reads
this registry to make a decision, and every mutator is a plain attribute/dict
write with no `await` in it, so an entry can never be seen half-updated and a bug
here cannot change admission, routing or failover behavior.

Lifecycle — exactly one owner closes each entry, mirroring the slot lifecycle:

    proxy.proxy_request   begin()             state=queued, registered
    proxy._dispatch       Entry.wait(targets) state=queued  (re-armed per attempt)
                          Entry.run(target)   state=running (slot acquired)
    both handlers         Entry.record(...)   outcome: status + token counts
    non-stream            proxy.proxy_request closes it — the buffered Response
                          is complete by the time the handler returns
    stream                proxy._handle_stream's generator `finally` closes it,
                          the same place the slot is released (the handler
                          returns long before the body is done)

`finish()` is idempotent — it moves the entry into the history exactly once,
using its own removal from `_active` as the guard.

Cancellation: each entry keeps a reference to the asyncio task serving it, so the
console can kill a request (`Entry.cancel`). See that method for why cancelling
the task — rather than setting a flag someone has to poll — is the mechanism.
"""
import asyncio
import time
from collections import deque
from datetime import datetime
from itertools import count
from typing import Optional

from app import clientinfo
from app import config as conf

_ids = count(1)
# id -> live Entry. Dicts keep insertion order, so the live set is arrival-ordered.
_active: dict = {}
# Frozen snapshots of finished requests, newest first (appendleft). Bounded, so a
# busy proxy cannot grow this without limit.
_recent: deque = deque(maxlen=max(1, conf.INFLIGHT_HISTORY))
# id -> {"request", "response", "reasoning", ...}: the prompt and reply for a row,
# kept out of `_active`/`_recent` so the console's 1s poll never carries them.
# Fetched per row, on demand, from /admin/inflight/{id}/body. Bounded to roughly
# the history size — bodies are the one part of an entry big enough to matter.
_bodies: dict = {}


class Entry:
    """One request's live state. Created by `begin`, mutated in place by the
    dispatcher and the handlers, frozen into the history by `finish`."""

    __slots__ = (
        "id", "arrived", "arrived_at", "state", "model", "stream", "op",
        "method", "path", "req_bytes", "client_ip", "svc", "provider",
        "native_model", "candidates", "attempt", "slot_at", "chunks",
        "task", "stream_task", "cancelled", "status", "in_tokens", "out_tokens",
        "skipped", "estimated",
    )

    def __init__(self, model, stream, op, method, path, req_bytes, client_ip, svc):
        self.id = next(_ids)
        # Monotonic for elapsed time (immune to clock steps), wall-clock for the
        # "arrived at" column.
        self.arrived = time.monotonic()
        self.arrived_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.state = "queued"
        self.model = model
        self.stream = stream
        self.op = op
        self.method = method
        self.path = path
        self.req_bytes = req_bytes
        self.client_ip = client_ip
        self.svc = svc
        self.provider = None
        self.native_model = None
        self.candidates = []
        self.attempt = 0
        # Times the slot queue admitted a later request ahead of this one because
        # the provider already had that model loaded (see app/slots.py).
        self.skipped = 0
        self.slot_at = None
        self.chunks = 0
        # The task serving this request — uvicorn's per-request ASGI task, since
        # `begin` is called from the endpoint itself. Cancelling it is how a kill
        # from the console reaches whatever the request is currently blocked on.
        self.task = asyncio.current_task()
        # Set once an SSE body starts flowing; see `bind_stream`.
        self.stream_task = None
        self.cancelled = False
        # Outcome, filled in by `record` once a response is known.
        self.status = None
        self.in_tokens = None
        self.out_tokens = None
        # True while out_tokens is our own live count rather than the upstream's
        # reported usage, so the console can render it as approximate.
        self.estimated = False

    def wait(self, targets) -> None:
        """Waiting for a slot: the initial admission, or a re-queue after a
        failover. `targets` is the still-viable candidate list, so the snapshot
        shows which backends this request could still land on — the useful thing
        to know about something that is stuck."""
        self.state = "queued"
        self.provider = None
        self.native_model = None
        self.slot_at = None
        # Priority order, de-duplicated (several targets can share a provider).
        self.candidates = list(dict.fromkeys(t.provider for t in targets))

    def run(self, provider: str, native_model: Optional[str]) -> None:
        """A slot was acquired on `provider`; the request is now on the wire."""
        self.state = "running"
        self.provider = provider
        self.native_model = native_model
        self.slot_at = time.monotonic()
        self.attempt += 1

    def passed_over(self) -> None:
        """Model affinity let a later request go first. Surfaced on the queued row
        so the reordering is visible rather than looking like a stall."""
        self.skipped += 1

    def chunk(self) -> None:
        """Count one SSE `data:` event, so a streaming row shows visible progress
        instead of a frozen elapsed timer."""
        self.chunks += 1

    def token(self) -> None:
        """Count one generated token, live.

        The upstream reports `usage` only in its final chunk (verified against
        llama.cpp: 1 of 33 chunks), so a running row would otherwise show nothing
        until it finished. Each delta carrying content or reasoning text is one
        generation step, which tracks the reported completion_tokens closely — but
        it is our count, not theirs, so it is flagged `estimated` and replaced by
        the real number the moment usage arrives. Input tokens cannot be known
        early at all: nothing upstream reveals the prompt count until the end.
        """
        self.out_tokens = (self.out_tokens or 0) + 1
        self.estimated = True

    def record(self, status: int, in_tokens: int = 0, out_tokens: int = 0) -> None:
        """Note the outcome, from whichever handler saw the upstream response.

        Called before `finish`, so the history row carries the real status and
        token counts instead of just "it ended". A failover records once per
        attempt; last write wins, which is the attempt the client actually got.

        Reported usage supersedes the live estimate — but only when it says
        something. A backend that never sends usage reports 0 here, and zeroing a
        count we watched tick up would be strictly worse information, so a 0 keeps
        the estimate (and its flag).
        """
        self.status = status
        if in_tokens:
            self.in_tokens = in_tokens
        if out_tokens:
            self.out_tokens = out_tokens
            self.estimated = False

    def set_request(self, body_str: str) -> None:
        """Attach the prompt this request sent, truncated to INFLIGHT_BODY_LIMIT."""
        if not conf.INFLIGHT_BODIES:
            return
        rec = _body_record(self.id)
        rec["request"], rec["request_truncated"] = _clip(body_str)

    def add_response(self, text: str, reasoning: bool = False) -> None:
        """Append to the reply captured for this row — one call per streamed delta,
        or one call with the whole body for a buffered response.

        Reasoning text is kept apart from the answer: on a thinking model the two
        interleave in the stream and reading them merged is worse than either.
        Stops appending at the cap instead of growing (a long generation would
        otherwise be unbounded), and does no formatting work once full.
        """
        if not conf.INFLIGHT_BODIES or not text:
            return
        rec = _body_record(self.id)
        key = "reasoning" if reasoning else "response"
        current = rec[key]
        room = conf.INFLIGHT_BODY_LIMIT - len(current)
        if room <= 0:
            rec[key + "_truncated"] = True
            return
        rec[key] = current + text[:room]
        if len(text) > room:
            rec[key + "_truncated"] = True

    def bind_stream(self) -> None:
        """Record the task that pumps the SSE body, called from the generator.

        Starlette runs a streaming body in a *child* task while the request's own
        task waits on client disconnect. Killing the request task there lands the
        cancellation in Starlette's disconnect listener, which uvicorn reports as
        "Exception in ASGI application" — a traceback for what was a deliberate
        operator action. Cancelling the body task instead ends the generator, so
        the response terminates through the ordinary completion path.
        """
        self.stream_task = asyncio.current_task()

    def cancel(self) -> None:
        """Kill this request by cancelling the task serving it.

        Cancellation, not a polled flag, because the interesting cases are all
        blocked inside somebody else's `await`: a queued request sits in
        `slots.acquire`'s Condition, and a running one sits in an httpx read that
        may never return — which is exactly the request worth killing, and exactly
        the one a flag nobody gets to check could never stop. CancelledError is
        delivered to that await and unwinds the normal cleanup path: slot
        released, upstream connection closed, entry moved into the history.

        Prefers the streaming body task when there is one (see `bind_stream`).
        Once a response is committed there is no status code left to send, so a
        killed stream simply ends; everything else is answered by
        `proxy_request`, which tells a client disconnect from an operator kill by
        this `cancelled` flag and returns a clean error instead of a 500.
        """
        self.cancelled = True
        task = self.stream_task or self.task
        if task is not None and not task.done():
            task.cancel()

    def _final_state(self) -> str:
        if self.cancelled:
            return "cancelled"
        # No status at all means no response ever came back (unreachable backend,
        # slot timeout, client gone) — a failure either way.
        if self.status is None or self.status >= 400:
            return "failed"
        return "done"

    def finish(self, fallback_status: Optional[int] = None) -> None:
        """Deregister and freeze into the history. Idempotent.

        Its own removal from `_active` is the guard: a second call — the entry is
        closed on several unwind paths — finds nothing and returns, so a request
        can never appear twice in the feed.
        """
        if _active.pop(self.id, None) is None:
            return
        if self.status is None:
            # Terminal responses the dispatcher builds itself (slot timeout, 401,
            # backend unreachable) never reach a handler, so take the status from
            # the response the caller is about to return.
            self.status = fallback_status
        now = time.monotonic()
        row = self.as_dict(now)
        row.update({
            "live": False,
            "state": self._final_state(),
            "status": self.status,
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "duration": round(now - self.arrived, 3),
        })
        _recent.appendleft(row)

    def as_dict(self, now: float) -> dict:
        return {
            "id": self.id,
            "live": True,
            "state": self.state,
            "status": self.status,
            "arrived_at": self.arrived_at,
            "finished_at": None,
            "age": round(now - self.arrived, 3),
            # How long it waited for a slot: final once running, still growing
            # while queued.
            "queued_for": round((self.slot_at or now) - self.arrived, 3),
            "running_for": round(now - self.slot_at, 3) if self.slot_at else None,
            "duration": None,
            "model": self.model,
            "provider": self.provider,
            "native_model": self.native_model,
            "candidates": self.candidates,
            "attempt": self.attempt,
            "skipped": self.skipped,
            "stream": self.stream,
            "op": self.op,
            "method": self.method,
            "path": self.path,
            "req_bytes": self.req_bytes,
            "chunks": self.chunks if self.stream else None,
            # Only meaningful once the upstream reported usage, i.e. at the end.
            "in_tokens": self.in_tokens,
            "out_tokens": self.out_tokens,
            "estimated": self.estimated,
            "has_body": self.id in _bodies,
            "cancelled": self.cancelled,
            "client_ip": self.client_ip,
            # Peeked from the cache the per-request log keeps warm — never a
            # blocking lookup, since this renders on every console poll.
            "client_host": clientinfo.cached_host(self.client_ip),
            "svc": self.svc,
        }


def _clip(text: str):
    """`(text, was_truncated)` clipped to the configured per-side cap."""
    limit = conf.INFLIGHT_BODY_LIMIT
    if text is None:
        return "", False
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _body_record(entry_id: int) -> dict:
    """The body record for `entry_id`, creating it on first write.

    Eviction is keyed to the history: once a row has aged out of `_recent` its
    bodies are unreachable from the console anyway, so anything older than the
    newest INFLIGHT_HISTORY ids is dropped. Insertion-ordered dict, so the oldest
    keys are simply the first ones.
    """
    rec = _bodies.get(entry_id)
    if rec is None:
        rec = {
            "request": "", "request_truncated": False,
            "response": "", "response_truncated": False,
            "reasoning": "", "reasoning_truncated": False,
        }
        _bodies[entry_id] = rec
        limit = max(1, conf.INFLIGHT_HISTORY)
        while len(_bodies) > limit:
            _bodies.pop(next(iter(_bodies)))
    return rec


def bodies(entry_id: int):
    """Captured prompt/reply for one row, or None if nothing was captured (body
    capture off, or the row has aged out)."""
    return _bodies.get(entry_id)


def begin(*, model, stream, op, method, path, req_bytes, client_ip, svc) -> Entry:
    """Register a newly arrived request. Starts out `queued`; the dispatcher
    moves it to `running` once it holds a slot."""
    entry = Entry(model, stream, op, method, path, req_bytes, client_ip, svc)
    _active[entry.id] = entry
    return entry


def get(entry_id: int) -> Optional[Entry]:
    """The live entry for `entry_id`, or None if it already finished."""
    return _active.get(entry_id)


def snapshot() -> dict:
    """The feed: live requests newest-first, then the finished history.

    Live rows are pinned above the history so what is happening *now* never
    scrolls away, and within each group the newest is first. Live timings are
    computed against a single `now` so the rows agree with each other; history
    rows were frozen at completion and are returned as they were. Iterating a
    copy of the live values keeps this safe if a request completes mid-render.
    """
    now = time.monotonic()
    live = [e.as_dict(now) for e in list(_active.values())]
    live.reverse()  # arrival order -> newest first
    return {
        "running": sum(1 for r in live if r["state"] == "running"),
        "queued": sum(1 for r in live if r["state"] == "queued"),
        "history": len(_recent),
        "history_limit": _recent.maxlen,
        "requests": live + list(_recent),
    }
