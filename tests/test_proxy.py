"""The request lifecycle: failover, slot release, and the request log.

Upstream is an `httpx.MockTransport` installed on the shared client, so the full
path runs — resolution, gate, slot acquire, body rewrite, forward, failover,
decompression, metrics, log — with no sockets and with exact control over what
each backend "answers".

The property under test throughout is the one that cannot be seen in review:
**every acquired slot is released exactly once**. Each test asserts occupancy is
back to zero afterwards, because a leak is invisible until a backend silently
stops draining its queue.
"""
import json
import json as _json
import logging
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config as conf
from app import clientinfo, registry, slots, upstream


def mock_response(status=200, *, json=None, content=None, headers=None):
    """An upstream response the proxy can actually *stream*.

    `httpx.Response(200, json=...)` arrives with its content already loaded, so
    `client.stream()` + `aiter_raw()` — which is how both handlers read a body —
    raises StreamConsumed. Wrapping the bytes in a ByteStream gives MockTransport
    a response that behaves like a real one on the wire.
    """
    if json is not None:
        content = _json.dumps(json).encode()
    content = content or b""
    merged = {"content-type": "application/json", "content-length": str(len(content))}
    merged.update(headers or {})
    return httpx.Response(status, stream=httpx.ByteStream(content), headers=merged)


CONFIG = """\
models:
  grouped:
    targets:
      - {provider: first,  priority: 1}
      - {provider: second, priority: 2}

providers:
  - name: first
    base_url: "http://first.invalid/v1"
    slots: 1
    enabled_models: ["vendor/First-Native"]
    model_map:
      "vendor/First-Native": grouped

  - name: second
    base_url: "http://second.invalid/v1"
    slots: 1
    enabled_models: ["vendor/Second-Native"]
    model_map:
      "vendor/Second-Native": grouped

routing:
  # Deliberately NOT the production default of 0 (wait forever). Both backends
  # have a single slot, so a leaked slot would make the next request queue
  # forever and hang the suite instead of failing it. With a short timeout a leak
  # surfaces as a 503 within seconds, which is a test failure you can read.
  # The wait-forever path is covered directly in test_slots.py.
  queue_timeout: 3
  failover: true
  down_backoff: 15
  failover_statuses: [429, 500, 502, 503, 504]
"""


def completion(model="m", prompt=11, out=7, text="hi"):
    return {
        "id": "cmpl-1",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": out},
    }


@pytest.fixture
def upstreams(load_config, monkeypatch):
    """Install a mock upstream and return the list that records what it saw.

    `handler(request) -> httpx.Response` is supplied per test via `set`.
    """
    async def fake_cached_live(provider):
        return list(provider.enabled_models)

    monkeypatch.setattr(registry, "_cached_live", fake_cached_live)
    # Keep the per-request log off the resolver.
    monkeypatch.setattr(conf, "RESOLVE_CLIENT_HOST", False)
    monkeypatch.setattr(clientinfo, "_dns_cache", {})

    load_config(CONFIG)

    seen = []
    state = {"handler": lambda r: mock_response(200, json=completion())}

    def transport_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return state["handler"](request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport_handler))
    monkeypatch.setattr(upstream, "forward_client", lambda: client)

    class Harness:
        requests = seen

        def set(self, handler):
            state["handler"] = handler

        @staticmethod
        def host(request):
            return request.url.host

        @staticmethod
        def body(request):
            return json.loads(request.content.decode())

    yield Harness()


@pytest.fixture
def client(upstreams):
    from app import main
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def events(caplog):
    """Capture the logfmt per-request event lines."""
    logger = logging.getLogger("llm-proxy.event")
    logger.propagate = True  # it is normally isolated from root
    caplog.set_level(logging.INFO, logger="llm-proxy.event")
    yield lambda: [
        r.getMessage() for r in caplog.records if r.name == "llm-proxy.event"
    ]
    logger.propagate = False


def idle():
    return {name: slots.in_use(name) for name in ("first", "second")}


# ── Happy path ──────────────────────────────────────────────────────────────

def test_request_reaches_the_best_priority_backend(client, upstreams):
    r = client.post("/v1/chat/completions", json={"model": "grouped"})
    assert r.status_code == 200
    assert upstreams.host(upstreams.requests[0]) == "first.invalid"
    assert idle() == {"first": 0, "second": 0}


def test_model_id_is_rewritten_to_the_native_name(client, upstreams):
    client.post("/v1/chat/completions", json={"model": "grouped"})
    assert upstreams.body(upstreams.requests[0])["model"] == "vendor/First-Native"


def test_slot_is_released_after_a_successful_request(client, upstreams):
    for _ in range(3):
        assert client.post("/v1/chat/completions", json={"model": "grouped"}).status_code == 200
    assert idle() == {"first": 0, "second": 0}, "a slot leaked across requests"


def test_client_facing_name_is_logged_as_asked(client, upstreams, events):
    client.post("/v1/chat/completions", json={"model": "grouped"})
    line = next(l for l in events() if "event=request" in l)
    # The native id stays in `model=` for dashboard compatibility...
    assert "model=vendor/First-Native" in line
    # ...and the name the client actually sent is its own field.
    assert "asked=grouped" in line


def test_asked_is_omitted_when_it_equals_the_native_id(load_config, monkeypatch, events):
    """An unmapped model logs exactly as it did before this field existed."""
    async def fake_cached_live(provider):
        return list(provider.enabled_models)

    monkeypatch.setattr(registry, "_cached_live", fake_cached_live)
    monkeypatch.setattr(conf, "RESOLVE_CLIENT_HOST", False)
    load_config("""\
        providers:
          - name: only
            base_url: "http://only.invalid/v1"
            enabled_models: ["plain-model"]
        """)
    c = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: mock_response(200, json=completion()))
    )
    monkeypatch.setattr(upstream, "forward_client", lambda: c)

    from app import main
    with TestClient(main.app) as tc:
        tc.post("/v1/chat/completions", json={"model": "plain-model"})

    line = next(l for l in events() if "event=request" in l)
    assert "model=plain-model" in line
    assert "asked=" not in line


def test_token_counts_reach_the_log(client, upstreams, events):
    upstreams.set(lambda r: mock_response(200, json=completion(prompt=42, out=13)))
    client.post("/v1/chat/completions", json={"model": "grouped"})
    line = next(l for l in events() if "event=request" in l)
    assert "in=42" in line and "out=13" in line


def test_history_row_tokens_per_second_is_the_logged_number(client, upstreams, events):
    """The In-flight tab's tok/s and the request log's speed_tps must be one
    number: the row takes the handler's measured duration rather than running a
    clock of its own, so the two cannot drift apart."""
    upstreams.set(lambda r: mock_response(200, json=completion(prompt=42, out=13)))
    client.post("/v1/chat/completions", json={"model": "grouped"})
    line = next(l for l in events() if "event=request" in l)
    logged = float(re.search(r"speed_tps=(\S+)", line).group(1))
    row = client.get("/admin/inflight").json()["requests"][0]
    assert row["live"] is False and row["out_tokens"] == 13
    assert row["tps"] == logged > 0
    # Every field fillFlightRow dereferences for its number cells — the console
    # is untyped, so a dropped one is a blank cell, not an error.
    assert {"tps", "estimated", "in_tokens", "out_tokens", "duration", "queued_for",
            "running_for", "chunks", "req_bytes"} <= set(row)


# ── Failover ────────────────────────────────────────────────────────────────

def test_retryable_status_fails_over_to_the_next_target(client, upstreams):
    def handler(request):
        if request.url.host == "first.invalid":
            return mock_response(503, json={"error": "overloaded"})
        return mock_response(200, json=completion())

    upstreams.set(handler)
    r = client.post("/v1/chat/completions", json={"model": "grouped"})
    assert r.status_code == 200
    assert [upstreams.host(x) for x in upstreams.requests] == ["first.invalid", "second.invalid"]
    assert idle() == {"first": 0, "second": 0}, "failover leaked a slot"


def test_connection_error_fails_over(client, upstreams):
    def handler(request):
        if request.url.host == "first.invalid":
            raise httpx.ConnectError("refused", request=request)
        return mock_response(200, json=completion())

    upstreams.set(handler)
    r = client.post("/v1/chat/completions", json={"model": "grouped"})
    assert r.status_code == 200
    assert idle() == {"first": 0, "second": 0}


def test_failed_backend_is_marked_down(client, upstreams):
    def handler(request):
        if request.url.host == "first.invalid":
            return mock_response(503)
        return mock_response(200, json=completion())

    upstreams.set(handler)
    client.post("/v1/chat/completions", json={"model": "grouped"})
    assert registry.is_down("first") is True
    assert registry.is_down("second") is False


def test_a_good_response_clears_a_down_mark(client, upstreams):
    registry.mark_down("first", 60)
    r = client.post("/v1/chat/completions", json={"model": "grouped"})
    assert r.status_code == 200
    # `first` was skipped as down, `second` served it and stays healthy.
    assert registry.is_down("second") is False


def test_non_retryable_4xx_is_relayed_not_retried(client, upstreams):
    """Every backend would reject a bad request identically, so relay it."""
    upstreams.set(lambda r: mock_response(400, json={"error": {"message": "bad param"}}))
    r = client.post("/v1/chat/completions", json={"model": "grouped"})
    assert r.status_code == 400
    assert len(upstreams.requests) == 1, "a 400 must not burn a failover"
    assert "bad param" in r.text
    assert idle() == {"first": 0, "second": 0}


def test_exhausted_targets_relay_the_last_upstream_error_verbatim(client, upstreams):
    """The real status and body survive, rather than becoming a synthetic 502."""
    upstreams.set(lambda r: mock_response(503, json={"error": {"message": "all full"}}))
    r = client.post("/v1/chat/completions", json={"model": "grouped"})
    assert r.status_code == 503
    assert "all full" in r.text
    assert len(upstreams.requests) == 2
    assert idle() == {"first": 0, "second": 0}


def test_all_backends_unreachable_yields_a_clean_502(client, upstreams):
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    upstreams.set(handler)
    r = client.post("/v1/chat/completions", json={"model": "grouped"})
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "upstream_unavailable"
    assert idle() == {"first": 0, "second": 0}


def test_timeout_maps_to_504(client, upstreams):
    def handler(request):
        raise httpx.ConnectTimeout("slow", request=request)

    upstreams.set(handler)
    r = client.post("/v1/chat/completions", json={"model": "grouped"})
    assert r.status_code == 504
    assert r.json()["error"]["type"] == "upstream_timeout"


# ── Streaming ───────────────────────────────────────────────────────────────

SSE = (
    b'data: {"model":"m","choices":[{"delta":{"content":"He"}}]}\n\n'
    b'data: {"model":"m","choices":[{"delta":{"content":"llo"}}]}\n\n'
    b'data: {"model":"m","choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n'
    b"data: [DONE]\n\n"
)


def test_stream_releases_its_slot_when_the_body_finishes(client, upstreams):
    upstreams.set(lambda r: mock_response(
        200, content=SSE, headers={"content-type": "text/event-stream"}
    ))
    r = client.post("/v1/chat/completions", json={"model": "grouped", "stream": True})
    assert r.status_code == 200
    # The stream is relayed byte-for-byte: the proxy parses deltas to count
    # tokens but must not reassemble or reshape what the client receives.
    assert r.content == SSE
    assert idle() == {"first": 0, "second": 0}, "a streaming slot leaked"


def test_stream_asks_upstream_for_usage(client, upstreams):
    upstreams.set(lambda r: mock_response(
        200, content=SSE, headers={"content-type": "text/event-stream"}
    ))
    client.post("/v1/chat/completions", json={"model": "grouped", "stream": True})
    body = upstreams.body(upstreams.requests[0])
    assert body["stream_options"]["include_usage"] is True


def test_stream_records_usage_in_the_log(client, upstreams, events):
    upstreams.set(lambda r: mock_response(
        200, content=SSE, headers={"content-type": "text/event-stream"}
    ))
    client.post("/v1/chat/completions", json={"model": "grouped", "stream": True})
    line = next(l for l in events() if "event=request" in l)
    assert "in=5" in line and "out=2" in line
    assert "stream=true" in line
    assert "asked=grouped" in line


def test_stream_history_row_tokens_per_second_is_the_logged_number(client, upstreams, events):
    upstreams.set(lambda r: mock_response(
        200, content=SSE, headers={"content-type": "text/event-stream"}
    ))
    client.post("/v1/chat/completions", json={"model": "grouped", "stream": True})
    line = next(l for l in events() if "event=request" in l)
    logged = float(re.search(r"speed_tps=(\S+)", line).group(1))
    row = client.get("/admin/inflight").json()["requests"][0]
    # The reported usage replaced the live estimate, so no ~ on the history row.
    assert row["out_tokens"] == 2 and row["estimated"] is False
    assert row["tps"] == logged > 0


def test_stream_error_before_first_byte_fails_over(client, upstreams):
    """Failover is impossible mid-stream, so the pre-flight has to catch it."""
    def handler(request):
        if request.url.host == "first.invalid":
            return mock_response(503, json={"error": "busy"})
        return mock_response(200, content=SSE, headers={"content-type": "text/event-stream"})

    upstreams.set(handler)
    r = client.post("/v1/chat/completions", json={"model": "grouped", "stream": True})
    assert r.status_code == 200
    assert [upstreams.host(x) for x in upstreams.requests] == ["first.invalid", "second.invalid"]
    assert idle() == {"first": 0, "second": 0}


def test_stream_upstream_error_is_relayed_with_its_real_status(client, upstreams):
    """A StreamingResponse would commit a 200 and bury the error in a bogus SSE
    stream, so an error body is buffered and relayed instead."""
    upstreams.set(lambda r: mock_response(400, json={"error": {"message": "bad stream"}}))
    r = client.post("/v1/chat/completions", json={"model": "grouped", "stream": True})
    assert r.status_code == 400
    assert "bad stream" in r.text
    assert idle() == {"first": 0, "second": 0}


# ── Passthrough ─────────────────────────────────────────────────────────────

def test_model_less_body_passes_through_without_a_slot(client, upstreams):
    upstreams.set(lambda r: mock_response(200, json={"ok": True}))
    r = client.post("/v1/embeddings", content=b"not json at all")
    assert r.status_code == 200
    assert idle() == {"first": 0, "second": 0}


def test_passthrough_is_logged_as_passthrough(client, upstreams, events):
    upstreams.set(lambda r: mock_response(200, json={"ok": True}))
    client.post("/v1/whatever", content=b"~~~")
    assert any("event=passthrough" in l for l in events())
    assert not any("model=unknown" in l for l in events())


# ── Decompression ───────────────────────────────────────────────────────────

def test_gzip_response_is_decompressed(client, upstreams):
    import gzip

    payload = json.dumps(completion(out=3)).encode()
    upstreams.set(lambda r: mock_response(
        200,
        content=gzip.compress(payload),
        headers={"content-encoding": "gzip", "content-type": "application/json"},
    ))
    r = client.post("/v1/chat/completions", json={"model": "grouped"})
    assert r.status_code == 200
    assert r.json()["usage"]["completion_tokens"] == 3
    assert "content-encoding" not in r.headers


def test_only_decodable_encodings_are_advertised_upstream(client, upstreams):
    client.post("/v1/chat/completions", json={"model": "grouped"})
    assert upstreams.requests[0].headers["accept-encoding"] == "gzip, deflate"
