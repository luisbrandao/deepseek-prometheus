"""The in-flight feed's derived numbers.

`Entry` is plain bookkeeping — no awaits, no I/O — so it is driven directly here
against a fixed `now`. The property that matters is that tokens/s is **the log's
number**: divided over upstream time, never over time spent queued, and taken
from the handler's own measured duration once there is one, so the history row
and the `speed_tps` field of the request log cannot disagree. test_proxy.py
closes the loop by pushing a request through and comparing the row to the line.
"""
import pytest

from app import inflight


def _begin(op=None):
    return inflight.begin(
        model="m", stream=True, op=op, method="POST", path="/v1/chat/completions",
        req_bytes=10, client_ip="127.0.0.1", svc=None,
    )


@pytest.mark.asyncio
async def test_queued_row_has_no_tps():
    e = _begin()
    e.wait([])
    assert e.tps(e.arrived + 5) is None
    assert e.as_dict(e.arrived + 5)["tps"] is None


@pytest.mark.asyncio
async def test_live_tps_is_tokens_over_running_time_and_estimated():
    e = _begin()
    e.run("first", "native")
    for _ in range(30):
        e.token()
    row = e.as_dict(e.slot_at + 10)
    assert row["tps"] == 3.0
    assert row["estimated"] is True


@pytest.mark.asyncio
async def test_queue_wait_is_not_in_the_denominator():
    e = _begin()
    e.run("first", "native")
    e.arrived = e.slot_at - 60  # a minute in the queue
    for _ in range(30):
        e.token()
    assert e.tps(e.slot_at + 10) == 3.0


@pytest.mark.asyncio
async def test_no_tokens_yet_means_no_tps():
    e = _begin()
    e.run("first", "native")
    assert e.tps(e.slot_at + 10) is None  # prompt still being processed


@pytest.mark.asyncio
async def test_recorded_duration_replaces_the_clock():
    e = _begin()
    e.run("first", "native")
    for _ in range(30):
        e.token()
    e.record(200, in_tokens=5, out_tokens=20, duration=4.0)
    # The wall clock says something else entirely; the handler's number wins.
    row = e.as_dict(e.slot_at + 1000)
    assert row["tps"] == 5.0
    assert row["estimated"] is False
    assert row["out_tokens"] == 20


@pytest.mark.asyncio
async def test_finished_row_carries_the_final_tps():
    e = _begin()
    e.run("first", "native")
    e.record(200, in_tokens=5, out_tokens=20, duration=4.0)
    e.finish()
    row = inflight._recent[0]
    assert row["state"] == "done"
    assert row["tps"] == 5.0


@pytest.mark.asyncio
async def test_no_output_op_reports_input_tokens_per_second():
    e = _begin(op="embedding")
    e.run("first", "native")
    e.record(200, in_tokens=100, out_tokens=0, duration=2.0)
    assert e.tps(e.slot_at + 1) == 50.0


@pytest.mark.asyncio
async def test_failover_forgets_the_failed_attempts_duration():
    e = _begin()
    e.run("first", "native")
    e.record(500, duration=9.0)
    e.wait([])
    e.run("second", "native")
    assert e.upstream_secs is None
    for _ in range(10):
        e.token()
    assert e.tps(e.slot_at + 2) == 5.0


@pytest.mark.asyncio
async def test_a_backend_that_never_reports_usage_keeps_the_estimate():
    e = _begin()
    e.run("first", "native")
    for _ in range(8):
        e.token()
    e.record(200, in_tokens=0, out_tokens=0, duration=4.0)
    row = e.as_dict(e.slot_at + 99)
    assert row["tps"] == 2.0 and row["estimated"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("tokens,secs", [(13, 0.0731), (2, 0.0049), (317, 7.9), (1, 3.0)])
async def test_tps_rounds_exactly_like_the_log_formats(tokens, secs):
    e = _begin()
    e.run("first", "native")
    e.record(200, in_tokens=1, out_tokens=tokens, duration=secs)
    assert f"{e.tps(e.slot_at):.2f}" == f"{tokens / secs:.2f}"
