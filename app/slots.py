"""Per-provider concurrency control: priority admission plus a model-affinity queue.

Each provider has a slot budget (its `slots` config; None = unlimited). A request
carries an ordered list of candidate targets (highest priority first). If a slot is
free the request takes it immediately — best priority tier first, round-robining
within a tier so equal-priority backends share load evenly. If nothing is free the
request joins an explicit queue and waits for a slot to be handed to it.

## Handoff, not wake-and-rescan

A released slot is transferred **directly** to a chosen waiter, in the same
synchronous step as the release. The slot is never observable as free while
somebody is queued for it, so a freshly arrived request cannot barge past the
queue and there is no wake-up race to lose. Every mutation in this module is
synchronous with no `await` in between, which is why there is no lock: no other
coroutine can ever observe half-updated accounting.

## Model affinity — why the queue reorders

Local backends (llama-swap, ollama) hold one model resident and swap on demand. A
swap costs seconds of load time and discards a warm cache, so it dominates the
latency of a short request. Strict FIFO provokes the worst case: with modelA
running on a single-slot backend and a queue of [modelB, modelA], FIFO serves
modelB — swapping modelA out, then straight back in for the modelA queued right
behind it. Two swaps to serve two requests.

So when capacity frees on a provider, admission prefers a waiter whose model that
provider **already has resident** (running there now, or the one it just finished)
over one that would force a swap. In that example modelA goes first and is served
with no reload; modelB swaps in once, afterwards. Same two requests, one swap.

This is a scheduling heuristic, not a correctness feature: it only reorders
*waiting* requests, never changes which backends a request is eligible for, and
never holds a slot idle waiting for a better match.

`Routing.affinity_max_skips` bounds the unfairness — a waiter passed over that
many times is admitted next no matter what is loaded, so a steady stream of modelA
cannot starve modelB indefinitely. `affinity_max_skips: 0` (or
`queue_affinity: false`) restores strict FIFO.

State is process-local, which is correct here because the app runs as a single
uvicorn worker. Running multiple workers would split the accounting and must use
a shared store instead.
"""
import asyncio
from itertools import groupby

from app import config as conf
from app.metrics import (
    QUEUE_AFFINITY_GRANTS,
    QUEUE_STARVATION_YIELDS,
    QUEUE_WAITING,
    SLOTS_IN_USE,
)

_in_use = {}       # provider name -> current in-flight count
_rr = {}           # round-robin cursor per set of equal-priority free providers
_waiters = []      # queued _Waiter objects in arrival order (FIFO baseline)
# provider -> {native model id: in-flight count}: what each backend is working on.
_running = {}
# provider -> native model id of the most recent release. For a single-slot backend
# this *is* the model still resident once it falls idle, which is the whole basis
# of the affinity decision.
_last_model = {}


class SlotTimeout(Exception):
    """Raised when no slot becomes free within the configured queue timeout."""


class _Waiter:
    """One queued request. `future` is resolved with the Target it was granted."""

    __slots__ = ("targets", "future", "on_skip", "skipped", "granted", "taken")

    def __init__(self, targets, on_skip):
        self.targets = targets
        self.future = asyncio.get_event_loop().create_future()
        self.on_skip = on_skip
        self.skipped = 0
        self.granted = None
        self.taken = False

    def skip(self) -> None:
        """Note that a later request was admitted ahead of this one."""
        self.skipped += 1
        if self.on_skip is not None:
            try:
                self.on_skip()
            except Exception:  # noqa: BLE001 - reporting must never break admission
                pass


def in_use(provider_name: str) -> int:
    """Current in-flight count for a provider (0 if idle). Read-only view of the
    live slot accounting, for introspection (e.g. the admin routing view)."""
    return _in_use.get(provider_name, 0)


def resident_model(provider_name: str):
    """The native model this backend most recently ran — what it is expected to
    still have loaded, and therefore what affinity favors. None if it hasn't run
    anything yet. Introspection only (the console shows it on the provider chip)."""
    return _last_model.get(provider_name)


def queue_depth() -> int:
    """How many requests are currently queued for a slot."""
    return len(_waiters)


def _capacity(provider_name):
    p = conf.PROVIDERS_BY_NAME.get(provider_name)
    return p.slots if p else None  # None => unlimited


def _free(provider_name: str) -> bool:
    cap = _capacity(provider_name)
    if cap is None:
        return True
    return _in_use.get(provider_name, 0) < cap


def _pick_free(targets, advance: bool = True):
    """Best free target: first priority tier with a free provider, round-robined.

    `targets` is already priority-sorted. Within a tier we rotate across the
    providers that currently have a free slot so equal-priority backends share
    load; a single free provider is returned directly. None => nothing free.

    `advance=False` probes without moving the round-robin cursor, so admission can
    ask "what would this waiter get?" for several waiters and only the one actually
    admitted spends a rotation.
    """
    for prio, tier in groupby(targets, key=lambda t: t.priority):
        free = [t for t in tier if _free(t.provider)]
        if not free:
            continue
        if len(free) == 1:
            return free[0]
        key = (prio,) + tuple(t.provider for t in free)
        i = _rr.get(key, 0) % len(free)
        if advance:
            _rr[key] = i + 1
        return free[i]
    return None


def _take(target) -> None:
    p = target.provider
    _in_use[p] = _in_use.get(p, 0) + 1
    SLOTS_IN_USE.labels(provider=p).set(_in_use[p])
    if target.model is not None:
        models = _running.setdefault(p, {})
        models[target.model] = models.get(target.model, 0) + 1


def _resident(provider_name: str, model) -> bool:
    """Is `model` already loaded on this backend — i.e. can it be served without a
    swap? True while another request is running it, and afterwards for the most
    recently finished model (a backend does not unload until something displaces
    it). Best-effort by nature: we cannot see inside the backend, we can only
    remember what we last asked it to run."""
    if model is None:
        return False
    if _running.get(provider_name, {}).get(model):
        return True
    return _last_model.get(provider_name) == model


def _choose():
    """`(waiter, target)` for the next admission, or `(None, None)` if nobody can run.

    FIFO baseline, with one reordering: a waiter whose model is already resident on
    the provider it would land on is admitted ahead of waiters that would force a
    model swap. A waiter that has been passed over `affinity_max_skips` times wins
    outright — that check comes first, and is what bounds the unfairness.
    """
    eligible = []
    for w in _waiters:
        if w.future.done():
            continue  # timed out or cancelled; its own finally will unqueue it
        t = _pick_free(w.targets, advance=False)
        if t is not None:
            eligible.append((w, t))
    if not eligible:
        return None, None

    max_skips = conf.ROUTING.affinity_max_skips
    for w, t in eligible:
        if w.skipped >= max_skips:
            # Passed over often enough. Serve it now, whatever that costs in swaps.
            QUEUE_STARVATION_YIELDS.labels(provider=t.provider).inc()
            return w, t

    if not conf.ROUTING.queue_affinity:
        return eligible[0]

    for i, (w, t) in enumerate(eligible):
        if _resident(t.provider, t.model):
            if i:
                # Everything eligible ahead of it just got passed over.
                for ahead, _ in eligible[:i]:
                    ahead.skip()
                QUEUE_AFFINITY_GRANTS.labels(provider=t.provider).inc()
            return w, t
    return eligible[0]


def _drain() -> None:
    """Hand out every slot that a queued request can use, best waiter first.

    Fully synchronous, so the release-and-transfer is atomic from any other
    coroutine's point of view.
    """
    while _waiters:
        w, _probe = _choose()
        if w is None:
            return
        # Re-pick so the admitted waiter (and only it) spends a round-robin
        # rotation. State is unchanged since the probe, so this is the same target.
        target = _pick_free(w.targets)
        if target is None:  # unreachable; guards against a future probe/pick drift
            return
        _take(target)
        _unqueue(w)
        w.granted = target
        if not w.future.done():
            w.future.set_result(target)


def _unqueue(w) -> None:
    try:
        _waiters.remove(w)
    except ValueError:
        pass


async def acquire(targets, timeout: float = 0.0, on_skip=None):
    """Reserve a slot on the best available target, queueing if all are busy.

    `targets` is ordered by priority. `timeout` of 0 (or None) waits forever;
    otherwise SlotTimeout is raised once the deadline passes. `on_skip` is invoked
    each time model affinity admits a later request ahead of this one — reporting
    only (the console shows it on the queued row).
    """
    target = _pick_free(targets)
    if target is not None:
        _take(target)
        return target

    w = _Waiter(targets, on_skip)
    _waiters.append(w)
    QUEUE_WAITING.inc()
    try:
        if timeout and timeout > 0:
            try:
                # wait_for hands back a result that landed as the deadline passed,
                # so a slot granted in that instant is not dropped.
                target = await asyncio.wait_for(w.future, timeout)
            except asyncio.TimeoutError:
                raise SlotTimeout()
        else:
            target = await w.future
        w.taken = True
        return target
    finally:
        QUEUE_WAITING.dec()
        _unqueue(w)
        if w.granted is not None and not w.taken:
            # A slot was handed to us in the instant we gave up — timed out, or the
            # request was cancelled from the console between the grant and our
            # resumption. Hand it straight on rather than stranding capacity.
            _release(w.granted.provider, w.granted.model)


async def poke() -> None:
    """Re-run admission after a config reload: a raised slot budget (or a new
    provider) creates capacity without any release happening, and queued requests
    are only ever woken by an admission decision."""
    _drain()


async def release(provider_name: str, model=None) -> None:
    """Give a slot back, then immediately hand it to the best queued request.

    `model` is the native id that just finished. It is what tells the affinity
    queue which model the backend still has loaded, so always pass it — a release
    without it looks like the backend left nothing resident and admission falls
    back to FIFO for that slot.
    """
    _release(provider_name, model)


def _release(provider_name: str, model=None) -> None:
    current = _in_use.get(provider_name, 0)
    if current <= 0:
        return
    _in_use[provider_name] = current - 1
    SLOTS_IN_USE.labels(provider=provider_name).set(current - 1)
    if model is not None:
        models = _running.get(provider_name)
        if models:
            remaining = models.get(model, 0) - 1
            if remaining > 0:
                models[model] = remaining
            else:
                models.pop(model, None)
        # The backend keeps this model loaded until something displaces it, so it
        # is the one a queued request can have without paying for a swap.
        _last_model[provider_name] = model
    _drain()
