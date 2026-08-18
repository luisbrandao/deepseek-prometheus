"""Live registry of in-flight requests — what is running, what is queued for a slot.

The Routing tab already answers *how many* slots each provider has in use; this
answers the next question: **which** requests those are, and what is stacked up
behind them. `slots.py` keeps only a per-provider counter, and its queue lives
inside an `asyncio.Condition` whose waiters are opaque from the outside — so a
request stuck waiting for capacity is invisible. Registering every proxied
request on arrival and dropping it on completion makes both sets enumerable.

State is process-local, exactly like `slots._in_use`, `registry._down_until` and
the log ring buffer: correct because the app runs a single uvicorn worker.

**Observation only.** Nothing in the request path reads this registry to make a
decision, and every mutator is a plain attribute/dict write with no `await` in
it, so an entry can never be seen half-updated and a bug here cannot change
admission, routing or failover behavior.

Lifecycle — exactly one owner closes each entry, mirroring the slot lifecycle:

    proxy.proxy_request   begin()             state=queued, registered
    proxy._dispatch       Entry.wait(targets) state=queued  (re-armed per attempt)
                          Entry.run(target)   state=running (slot acquired)
    non-stream            proxy.proxy_request closes it — the buffered Response
                          is complete by the time the handler returns
    stream                proxy._handle_stream's generator `finally` closes it,
                          the same place the slot is released (the handler
                          returns long before the body is done)

`finish()` is idempotent, so a double close is harmless.
"""
import time
from datetime import datetime
from itertools import count
from typing import Optional

from app import clientinfo

_ids = count(1)
# id -> Entry. Dicts keep insertion order, so a snapshot is arrival-ordered
# (oldest first) for free.
_active: dict = {}


class Entry:
    """One request's live state. Created by `begin`, mutated in place by the
    dispatcher, removed by `finish`."""

    __slots__ = (
        "id", "arrived", "arrived_at", "state", "model", "stream", "op",
        "method", "path", "req_bytes", "client_ip", "svc", "provider",
        "native_model", "candidates", "attempt", "slot_at", "chunks",
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
        self.slot_at = None
        self.chunks = 0

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

    def chunk(self) -> None:
        """Count one SSE `data:` event, so a streaming row shows visible progress
        instead of a frozen elapsed timer. Deliberately a chunk count, not a token
        count: the upstream only reports usage in its final chunk."""
        self.chunks += 1

    def finish(self) -> None:
        """Deregister. Idempotent — a missing id is not an error."""
        _active.pop(self.id, None)

    def as_dict(self, now: float) -> dict:
        return {
            "id": self.id,
            "state": self.state,
            "arrived_at": self.arrived_at,
            "age": round(now - self.arrived, 3),
            # How long it waited for a slot: final once running, still growing
            # while queued.
            "queued_for": round((self.slot_at or now) - self.arrived, 3),
            "running_for": round(now - self.slot_at, 3) if self.slot_at else None,
            "model": self.model,
            "provider": self.provider,
            "native_model": self.native_model,
            "candidates": self.candidates,
            "attempt": self.attempt,
            "stream": self.stream,
            "op": self.op,
            "method": self.method,
            "path": self.path,
            "req_bytes": self.req_bytes,
            "chunks": self.chunks if self.stream else None,
            "client_ip": self.client_ip,
            # Peeked from the cache the per-request log keeps warm — never a
            # blocking lookup, since this renders on every console poll.
            "client_host": clientinfo.cached_host(self.client_ip),
            "svc": self.svc,
        }


def begin(*, model, stream, op, method, path, req_bytes, client_ip, svc) -> Entry:
    """Register a newly arrived request. Starts out `queued`; the dispatcher
    moves it to `running` once it holds a slot."""
    entry = Entry(model, stream, op, method, path, req_bytes, client_ip, svc)
    _active[entry.id] = entry
    return entry


def snapshot() -> dict:
    """Arrival-ordered view of everything in flight, plus the two counts.

    Timings are computed against a single `now` so the rows are consistent with
    each other. Iterating a copy of the values keeps this safe if a request
    completes mid-render.
    """
    now = time.monotonic()
    requests = [e.as_dict(now) for e in list(_active.values())]
    return {
        "running": sum(1 for r in requests if r["state"] == "running"),
        "queued": sum(1 for r in requests if r["state"] == "queued"),
        "requests": requests,
    }
