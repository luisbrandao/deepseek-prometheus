"""Slot admission and the release invariant.

`slots.py` is the one module where a bug is invisible in review and fatal in
production: every acquired slot must be released exactly once, on every exit path
including timeout, cancellation and client disconnect. A leak doesn't crash
anything — it silently shrinks a backend's capacity until the queue stops
draining, which looks like a slow backend rather than a proxy bug.

These tests drive `acquire`/`release` directly. They cover the four properties
AGENTS.md calls invariants: priority admission with round-robin inside a tier,
queue-when-full with a synchronous handoff, affinity reordering bounded by
`affinity_max_skips`, and no slot stranded by a timeout or a cancellation.
"""
import asyncio

import pytest

from app import config as conf
from app import slots


def T(provider, model=None, priority=100):
    return conf.Target(provider=provider, model=model, priority=priority)


async def test_free_slot_is_taken_immediately(providers):
    providers({"name": "a", "base_url": "http://x/v1", "slots": 1})
    target = await slots.acquire([T("a", "m")])
    assert target.provider == "a"
    assert slots.in_use("a") == 1


async def test_unlimited_provider_never_queues(providers):
    providers({"name": "a", "base_url": "http://x/v1", "slots": None})
    for _ in range(50):
        await slots.acquire([T("a", "m")])
    assert slots.in_use("a") == 50


async def test_best_priority_tier_wins(providers):
    providers(
        {"name": "a", "base_url": "http://x/v1", "slots": 1},
        {"name": "b", "base_url": "http://y/v1", "slots": 1},
    )
    target = await slots.acquire([T("a", "m", 1), T("b", "m", 2)])
    assert target.provider == "a"


async def test_falls_to_next_tier_when_best_is_full(providers):
    providers(
        {"name": "a", "base_url": "http://x/v1", "slots": 1},
        {"name": "b", "base_url": "http://y/v1", "slots": 1},
    )
    first = await slots.acquire([T("a", "m", 1), T("b", "m", 2)])
    second = await slots.acquire([T("a", "m", 1), T("b", "m", 2)])
    assert {first.provider, second.provider} == {"a", "b"}


async def test_round_robin_within_a_tie_tier(providers):
    """Equal priority means share the load, not always pick the first."""
    providers(
        {"name": "a", "base_url": "http://x/v1", "slots": 4},
        {"name": "b", "base_url": "http://y/v1", "slots": 4},
    )
    picked = []
    for _ in range(4):
        t = await slots.acquire([T("a", "m", 1), T("b", "m", 1)])
        picked.append(t.provider)
    assert picked.count("a") == 2 and picked.count("b") == 2, picked


async def test_queues_when_full_then_release_hands_the_slot_over(providers):
    providers({"name": "a", "base_url": "http://x/v1", "slots": 1})
    held = await slots.acquire([T("a", "m")])

    waiter = asyncio.create_task(slots.acquire([T("a", "m")]))
    await asyncio.sleep(0)  # let it reach the queue
    assert slots.queue_depth() == 1
    assert not waiter.done()

    await slots.release(held.provider, held.model)
    granted = await asyncio.wait_for(waiter, 1)
    assert granted.provider == "a"
    # Occupancy never dipped to zero: the slot was handed straight over.
    assert slots.in_use("a") == 1
    assert slots.queue_depth() == 0


async def test_a_new_arrival_cannot_barge_past_the_queue(providers):
    """The released slot is transferred to a chosen waiter in the same
    synchronous step, so it is never observable as free."""
    providers({"name": "a", "base_url": "http://x/v1", "slots": 1})
    held = await slots.acquire([T("a", "m")])

    queued = asyncio.create_task(slots.acquire([T("a", "m")]))
    await asyncio.sleep(0)
    assert slots.queue_depth() == 1

    await slots.release(held.provider, held.model)
    # The queued request has the slot before anything else can ask for it.
    assert queued.done() or slots.in_use("a") == 1
    await asyncio.wait_for(queued, 1)

    latecomer = asyncio.create_task(slots.acquire([T("a", "m")]))
    await asyncio.sleep(0)
    assert not latecomer.done()
    latecomer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await latecomer


async def test_slot_timeout_raises_and_strands_nothing(providers):
    providers({"name": "a", "base_url": "http://x/v1", "slots": 1})
    held = await slots.acquire([T("a", "m")])

    with pytest.raises(slots.SlotTimeout):
        await slots.acquire([T("a", "m")], timeout=0.05)

    assert slots.queue_depth() == 0
    await slots.release(held.provider, held.model)
    assert slots.in_use("a") == 0


async def test_cancelled_waiter_leaks_no_slot(providers):
    """A kill lands on a queued request: it must leave the queue and must not
    take capacity with it."""
    providers({"name": "a", "base_url": "http://x/v1", "slots": 1})
    held = await slots.acquire([T("a", "m")])

    waiter = asyncio.create_task(slots.acquire([T("a", "m")]))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert slots.queue_depth() == 0
    await slots.release(held.provider, held.model)
    assert slots.in_use("a") == 0


async def test_slot_granted_in_the_instant_of_giving_up_is_handed_on(providers):
    """The timeout/cancel race: `acquire`'s finally must return a slot that was
    granted between the grant and its resumption, or capacity is stranded."""
    providers({"name": "a", "base_url": "http://x/v1", "slots": 1})
    held = await slots.acquire([T("a", "m")])

    waiter_a = asyncio.create_task(slots.acquire([T("a", "m")]))
    await asyncio.sleep(0)
    waiter_b = asyncio.create_task(slots.acquire([T("a", "m")]))
    await asyncio.sleep(0)
    assert slots.queue_depth() == 2

    # Cancel the first waiter, then release. Whichever way the race falls, the
    # slot must end up with waiter_b rather than nowhere.
    waiter_a.cancel()
    await slots.release(held.provider, held.model)

    with pytest.raises(asyncio.CancelledError):
        await waiter_a
    granted = await asyncio.wait_for(waiter_b, 1)
    assert granted.provider == "a"
    assert slots.in_use("a") == 1


async def test_release_below_zero_is_ignored(providers):
    """A double release must not manufacture capacity."""
    providers({"name": "a", "base_url": "http://x/v1", "slots": 1})
    held = await slots.acquire([T("a", "m")])
    await slots.release(held.provider, held.model)
    await slots.release(held.provider, held.model)
    assert slots.in_use("a") == 0


# ── Model affinity ──────────────────────────────────────────────────────────

async def test_affinity_prefers_the_resident_model(providers):
    """The canonical case from the module docstring: modelA running on a
    single-slot backend, queue of [modelB, modelA]. FIFO would swap twice;
    affinity serves modelA first and swaps once."""
    providers(
        {"name": "a", "base_url": "http://x/v1", "slots": 1},
        queue_affinity=True, affinity_max_skips=3,
    )
    # Occupy the slot; the affinity tests release by name, not by target.
    await slots.acquire([T("a", "modelA")])

    want_b = asyncio.create_task(slots.acquire([T("a", "modelB")]))
    await asyncio.sleep(0)
    want_a = asyncio.create_task(slots.acquire([T("a", "modelA")]))
    await asyncio.sleep(0)

    await slots.release("a", "modelA")

    # modelA jumped the queue because the backend still has it loaded.
    granted = await asyncio.wait_for(want_a, 1)
    assert granted.model == "modelA"
    assert not want_b.done()

    await slots.release("a", "modelA")
    granted_b = await asyncio.wait_for(want_b, 1)
    assert granted_b.model == "modelB"


async def test_affinity_off_is_strict_fifo(providers):
    providers(
        {"name": "a", "base_url": "http://x/v1", "slots": 1},
        queue_affinity=False, affinity_max_skips=3,
    )
    # Occupy the slot; the affinity tests release by name, not by target.
    await slots.acquire([T("a", "modelA")])

    want_b = asyncio.create_task(slots.acquire([T("a", "modelB")]))
    await asyncio.sleep(0)
    want_a = asyncio.create_task(slots.acquire([T("a", "modelA")]))
    await asyncio.sleep(0)

    await slots.release("a", "modelA")
    granted = await asyncio.wait_for(want_b, 1)
    assert granted.model == "modelB"
    assert not want_a.done()
    want_a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await want_a


async def test_starvation_bound_forces_fifo(providers):
    """`affinity_max_skips` is the only thing stopping a hot model from starving
    a cold one, and it is checked *before* affinity in `_choose`."""
    providers(
        {"name": "a", "base_url": "http://x/v1", "slots": 1},
        queue_affinity=True, affinity_max_skips=2,
    )
    # Occupy the slot; the affinity tests release by name, not by target.
    await slots.acquire([T("a", "modelA")])

    cold = asyncio.create_task(slots.acquire([T("a", "modelB")]))
    await asyncio.sleep(0)

    # A steady stream of the resident model keeps skipping the cold waiter.
    for _ in range(2):
        hot = asyncio.create_task(slots.acquire([T("a", "modelA")]))
        await asyncio.sleep(0)
        await slots.release("a", "modelA")
        got = await asyncio.wait_for(hot, 1)
        assert got.model == "modelA"

    assert not cold.done(), "cold waiter should still be waiting at this point"

    # It has now been skipped twice; the cap must admit it next regardless of
    # what is loaded.
    another_hot = asyncio.create_task(slots.acquire([T("a", "modelA")]))
    await asyncio.sleep(0)
    await slots.release("a", "modelA")

    got_cold = await asyncio.wait_for(cold, 1)
    assert got_cold.model == "modelB", "starvation cap did not fire"
    another_hot.cancel()
    with pytest.raises(asyncio.CancelledError):
        await another_hot


async def test_affinity_never_holds_a_slot_idle(providers):
    """Affinity only reorders waiters. With nothing resident it must still admit
    somebody rather than waiting for a better match."""
    providers(
        {"name": "a", "base_url": "http://x/v1", "slots": 1},
        queue_affinity=True, affinity_max_skips=3,
    )
    # Occupy the slot; the affinity tests release by name, not by target.
    await slots.acquire([T("a", "modelA")])
    waiter = asyncio.create_task(slots.acquire([T("a", "modelZ")]))
    await asyncio.sleep(0)
    await slots.release("a", "modelA")
    granted = await asyncio.wait_for(waiter, 1)
    assert granted.model == "modelZ"


async def test_on_skip_is_reported_to_the_caller(providers):
    """The console shows the skip count on a queued row, so the callback has to
    fire once per reordering."""
    providers(
        {"name": "a", "base_url": "http://x/v1", "slots": 1},
        queue_affinity=True, affinity_max_skips=5,
    )
    skips = []
    # Occupy the slot; the affinity tests release by name, not by target.
    await slots.acquire([T("a", "modelA")])

    cold = asyncio.create_task(
        slots.acquire([T("a", "modelB")], on_skip=lambda: skips.append(1))
    )
    await asyncio.sleep(0)
    hot = asyncio.create_task(slots.acquire([T("a", "modelA")]))
    await asyncio.sleep(0)

    await slots.release("a", "modelA")
    await asyncio.wait_for(hot, 1)
    assert skips == [1]

    await slots.release("a", "modelA")
    await asyncio.wait_for(cold, 1)


async def test_poke_admits_after_capacity_is_raised(providers, monkeypatch):
    """A raised slot budget creates capacity with no release happening, and
    queued waiters are only ever woken by an admission decision."""
    provs = providers({"name": "a", "base_url": "http://x/v1", "slots": 1})
    await slots.acquire([T("a", "m")])  # fill the single slot
    waiter = asyncio.create_task(slots.acquire([T("a", "m")]))
    await asyncio.sleep(0)
    assert not waiter.done()

    provs[0].slots = 2  # the config edit
    await slots.poke()

    granted = await asyncio.wait_for(waiter, 1)
    assert granted.provider == "a"
    assert slots.in_use("a") == 2
