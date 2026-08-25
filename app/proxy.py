import asyncio
import gzip
import json
import logging
import math
import time
import zlib
from datetime import datetime, timedelta
from typing import Optional, Union

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from app import config as conf
from app import auth, clientinfo, inflight, registry, router, slots
from app.config import Provider
from app.metrics import (
    ERRORS_TOTAL,
    FAILOVERS_TOTAL,
    REQUEST_DURATION,
    REQUESTS_TOTAL,
    TOKENS_INPUT_TOTAL,
    TOKENS_OUTPUT_TOTAL,
)

logger = logging.getLogger("llm-proxy")
# Pure-logfmt, prefix-free per-request events (configured in app.main).
event_logger = logging.getLogger("llm-proxy.event")


def _build_url(provider: Provider, path: str) -> str:
    return f"{provider.base_url}/{conf.strip_prefix(provider, path)}"


def _build_headers(provider: Provider, request: Request) -> dict:
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)
    if provider.api_key:
        headers["authorization"] = f"Bearer {provider.api_key}"
    else:
        headers.pop("authorization", None)
    # Operator-configured per-backend headers (e.g. OpenRouter attribution:
    # HTTP-Referer / X-Title). Applied as defaults — a header the client already
    # sent wins, so per-app attribution still passes through. Keys are lowercased
    # to match the forwarded set (HTTP header names are case-insensitive) and
    # avoid sending a duplicate.
    for k, v in provider.headers.items():
        headers.setdefault(k.lower(), v)
    # Forward the caller's User-Agent verbatim. When the caller sent none, httpx
    # would otherwise inject its own `python-httpx/x.y` default, which surfaces at
    # the backend (e.g. OpenRouter) as a bogus/"missing" client. Ensuring the key
    # is always present stops that substitution; absent a caller UA we advertise
    # the proxy itself rather than httpx.
    headers.setdefault("user-agent", "llm-proxy")
    # Only advertise encodings we can decode with the stdlib. Otherwise a
    # backend may reply with brotli, which we'd be unable to uncompress.
    headers["accept-encoding"] = "gzip, deflate"
    return headers


def _decompress(raw: bytes, encoding: str) -> bytes:
    """Decompress an upstream response body based on its Content-Encoding.

    Handles gzip, deflate, brotli and zstd. Unknown or empty encodings are
    returned unchanged. If decompression fails the raw bytes are returned so
    the proxy never crashes on an unexpected body.
    """
    encoding = (encoding or "").strip().lower()
    if not encoding or encoding == "identity":
        return raw

    try:
        if encoding == "gzip":
            return gzip.decompress(raw)
        if encoding == "deflate":
            try:
                return zlib.decompress(raw)
            except zlib.error:
                # Raw deflate stream without zlib header/trailer.
                return zlib.decompress(raw, -zlib.MAX_WBITS)
        if encoding == "br":
            import brotli  # type: ignore

            return brotli.decompress(raw)
        if encoding == "zstd":
            import zstandard  # type: ignore

            return zstandard.ZstdDecompressor().decompress(raw)
    except Exception as e:  # noqa: BLE001 - never let decompression crash the proxy
        logger.warning(f"Failed to decompress '{encoding}' response: {e}")
        return raw

    logger.warning(f"Unknown Content-Encoding '{encoding}', passing body through")
    return raw


def _log_curl(method: str, url: str, headers: dict, body: str) -> None:
    cmd = f"{method} {url}"
    for k, v in headers.items():
        if k.lower() == "authorization":
            cmd += f"\n  -H '{k}: Bearer ***'"
        else:
            cmd += f"\n  -H '{k}: {v}'"
    if body:
        try:
            parsed = json.loads(body)
            pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
            cmd += f"\n  -d '{pretty}'"
        except (json.JSONDecodeError, ValueError):
            cmd += f"\n  -d '{body}'"
    logger.info(f"Request:\n{cmd}")


def _logfmt(fields: dict) -> str:
    """Render an ordered dict as a logfmt line (key=value, space-separated).

    Values are quoted when they would otherwise break tokenization (contain
    whitespace, quotes or '='). None values are dropped so absent fields just
    don't appear. Parses cleanly in Loki/Grafana with `| logfmt`.
    """
    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        s = str(value)
        if s == "" or any(c in s for c in ' "=\n\r\t'):
            s = '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ").replace("\t", " ") + '"'
        parts.append(f"{key}={s}")
    return " ".join(parts)


def _err_kind(status: int) -> Optional[str]:
    """A short, stable error category for an HTTP status (None when not an error)."""
    if status < 400:
        return None
    if status in (401, 403):
        return "unauthorized"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limited"
    if status < 500:
        return "invalid_request"
    return "upstream_error"


def _op_kind(path: str, model: str):
    """Classify no-output operations so the request log can tag them and skip the
    meaningless output-tokens/sec. Embeddings and rerankers return no completion
    tokens, so `out` is always 0 and a tokens/s figure would be a constant 0.00.

    Matched against both the request path (`/v1/embeddings`, `/v1/rerank`, …) and
    the model id (e.g. a `*-Reranker-*` / `*-Embedding-*` name), so it still works
    when a backend serves these over a nonstandard path. Returns None for ordinary
    generation. Extend the keyword list for other no-output ops (e.g. moderation).
    """
    hay = f"{path} {model}".lower()
    if "embed" in hay:
        return "embedding"
    if "rerank" in hay:
        return "rerank"
    return None


async def _emit_request_log(
    request: Request, provider: str, model: str, status: int,
    in_tokens: int, out_tokens: int, duration: float, stream: bool,
) -> None:
    """Emit the single, always-on, parseable line summarizing one request.

    Carries who called (ip/host/service), what ran (provider/model), the
    outcome (status, token counts, speed) and whether it streamed. Runs after
    the response is delivered (background task / stream finally) so the
    reverse-DNS lookup never adds latency to the client.

    A model-less passthrough (non-chat / multipart body, nothing to resolve) is
    logged as `event=passthrough` keyed by request path — never as a bogus
    `model=unknown`, which would pollute the per-model view.
    """
    ip = clientinfo.client_ip(request)
    host = await clientinfo.client_host(ip)
    ua = (request.headers.get("user-agent") or "").strip() or None
    fields = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "level": "info",
    }
    if model == "unknown":
        fields.update({
            "event": "passthrough",
            "provider": provider,
            "method": request.method,
            "path": request.url.path,
            "status": status,
            "stream": "true" if stream else "false",
        })
    else:
        # Embeddings and rerankers return no completion tokens, so out-tokens/sec
        # is always 0.00 and skews throughput dashboards. Tag the op and report
        # input throughput (in_tps) instead of speed_tps. The None-valued fields
        # are dropped by _logfmt, so ordinary generation lines are unchanged.
        op = _op_kind(request.url.path, model)
        fields.update({
            "event": "request",
            "provider": provider,
            "model": model,
            "op": op,
            "status": status,
            "stream": "true" if stream else "false",
            "in": in_tokens,
            "out": out_tokens,
            # H:MM:SS, rounded UP to the whole second so a sub-second request
            # reads 0:00:01, never a misleading 0:00:00.
            "dur": str(timedelta(seconds=math.ceil(duration))),
            "speed_tps": None if op else f"{out_tokens / duration if duration > 0 else 0:.2f}",
            "in_tps": f"{in_tokens / duration if duration > 0 else 0:.2f}" if op else None,
        })
    fields.update({
        "client_ip": ip,
        "client_host": host,
        "svc": clientinfo.service_from_ua(ua),
        "ua": ua,
        "err": _err_kind(status),
    })
    event_logger.info(_logfmt(fields))


def _log_upstream_error(provider: str, model: str, status: int, body: str) -> None:
    """Log an upstream error response with its body, always.

    LOG_OUTPUT only gates successful-response logging; a backend rejecting a
    request (bad param, auth, overload) must be diagnosable from the proxy log
    alone — the body is where Google/OpenRouter/ollama say *why*.
    """
    snippet = body.strip()
    try:
        snippet = json.dumps(json.loads(snippet), indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, ValueError):
        pass
    if len(snippet) > 4000:
        snippet = snippet[:4000] + "... [truncated]"
    qualifier = "passthrough" if model == "unknown" else f"model: {model}"
    logger.warning(f"Upstream error {status} from '{provider}' ({qualifier}):\n{snippet}")


def _record_metrics(provider: str, model: str, in_tokens: int, out_tokens: int, duration: float) -> None:
    REQUESTS_TOTAL.labels(provider=provider, model=model).inc()
    TOKENS_INPUT_TOTAL.labels(provider=provider, model=model).inc(in_tokens)
    TOKENS_OUTPUT_TOTAL.labels(provider=provider, model=model).inc(out_tokens)
    REQUEST_DURATION.labels(provider=provider, model=model).observe(duration)


def _backend_error(provider: Provider, model: str, exc: Exception) -> Response:
    """Build a clean OpenAI-style error when an upstream backend is unreachable.

    Backends come and go, so a connection failure is an expected condition, not
    a crash: we translate it into a 502/504 the client can understand instead of
    letting it surface as an unhandled 500.
    """
    if isinstance(exc, httpx.TimeoutException):
        status, kind = 504, "upstream_timeout"
    else:
        status, kind = 502, "upstream_unavailable"

    logger.warning(f"Backend '{provider.name}' unreachable: {type(exc).__name__}: {exc}")
    if model != "unknown":
        ERRORS_TOTAL.labels(provider=provider.name, model=model, status_code=str(status)).inc()

    payload = {
        "error": {
            "message": f"Upstream backend '{provider.name}' is unavailable: {exc}",
            "type": kind,
            "code": status,
        }
    }
    return Response(
        content=json.dumps(payload),
        status_code=status,
        media_type="application/json",
    )


async def _handle_non_stream(
    request: Request, provider: Provider, path: str, body: bytes, body_str: str, model: str,
    entry=None,
) -> Response:
    url = _build_url(provider, path)
    headers = _build_headers(provider, request)
    method = request.method.upper()
    pname = provider.name

    if conf.LOG_INPUT:
        _log_curl(method, url, headers, body_str)

    start = time.time()

    # Connection failures propagate as httpx.RequestError so the dispatcher can
    # fail over to the next backend; the response is fully buffered here.
    async with httpx.AsyncClient(timeout=600.0) as client:
        async with client.stream(method, url, headers=headers, content=body) as resp:
            # Read the raw, undecoded bytes so we control decompression
            # ourselves (httpx cannot decode brotli/zstd without extra libs
            # and would otherwise pass compressed bytes straight through).
            raw = b"".join([chunk async for chunk in resp.aiter_raw()])
            status_code = resp.status_code
            is_error = resp.is_error
            resp_headers = dict(resp.headers)
            content_encoding = resp.headers.get("content-encoding", "")

    duration = time.time() - start

    resp_bytes = _decompress(raw, content_encoding)
    resp_body = resp_bytes.decode("utf-8", errors="replace")

    in_tokens = 0
    out_tokens = 0
    try:
        data = json.loads(resp_body)
        usage = data.get("usage", {})
        in_tokens = usage.get("prompt_tokens", 0)
        out_tokens = usage.get("completion_tokens", 0)
    except (json.JSONDecodeError, AttributeError):
        pass

    if is_error:
        _log_upstream_error(pname, model, status_code, resp_body)
    elif conf.LOG_OUTPUT:
        pretty_body = resp_body
        try:
            parsed = json.loads(resp_body)
            pretty_body = json.dumps(parsed, indent=2, ensure_ascii=False)
        except (json.JSONDecodeError, ValueError):
            pass
        logger.info(f"Response ({status_code}):\n{pretty_body}")

    if model != "unknown":
        if is_error:
            ERRORS_TOTAL.labels(provider=pname, model=model, status_code=str(status_code)).inc()
        else:
            _record_metrics(pname, model, in_tokens, out_tokens, duration)

    if entry is not None:
        entry.record(status_code, in_tokens, out_tokens)
        entry.add_response(resp_body)

    resp_headers.pop("content-length", None)
    resp_headers.pop("transfer-encoding", None)
    resp_headers.pop("content-encoding", None)

    return Response(
        content=resp_body,
        status_code=status_code,
        headers=resp_headers,
        background=BackgroundTask(
            _emit_request_log, request, pname, model, status_code,
            in_tokens, out_tokens, duration, False,
        ),
    )


async def _handle_stream(
    request: Request, provider: Provider, path: str, body: bytes, body_str: str, model: str,
    on_complete=None, entry=None,
) -> Union[Response, StreamingResponse]:
    url = _build_url(provider, path)
    headers = _build_headers(provider, request)
    method = request.method.upper()
    pname = provider.name

    if conf.LOG_INPUT:
        _log_curl(method, url, headers, body_str)

    # Pre-flight the connection: a StreamingResponse commits its status code
    # before the body generator runs, so we must learn whether the backend is
    # reachable *now* — otherwise an offline backend would yield a 200 with an
    # empty body instead of a clean error. A failure here propagates as
    # httpx.RequestError so the dispatcher can fail over before any bytes are
    # committed to the client (failover is impossible mid-stream).
    # The clock starts before the request is sent so duration covers the full
    # exchange, including prompt processing on the backend. A backend that
    # buffers the whole completion before responding (e.g. an aggregator)
    # spends all its time before the first byte — timing only the body read
    # would yield near-zero durations and absurd tokens/sec.
    start = time.time()
    client = httpx.AsyncClient(timeout=600.0)
    stream_cm = client.stream(method, url, headers=headers, content=body)
    try:
        resp = await stream_cm.__aenter__()
    except BaseException:
        # RequestError (the dispatcher fails over on it) or cancellation (an
        # operator kill landing while we connect) — either way this client would
        # otherwise leak its connection. Re-raised unchanged.
        await client.aclose()
        raise

    # Upstream rejected the request outright (4xx/5xx). A StreamingResponse
    # commits a 200 before its generator runs, which would bury the error in a
    # bogus SSE stream the client can't interpret. Buffer the (small) error
    # body instead and relay it verbatim with the upstream's real status.
    if resp.is_error:
        try:
            raw = b"".join([chunk async for chunk in resp.aiter_raw()])
            status_code = resp.status_code
            resp_headers = dict(resp.headers)
            content_encoding = resp.headers.get("content-encoding", "")
        finally:
            await stream_cm.__aexit__(None, None, None)
            await client.aclose()
            if on_complete is not None:
                await on_complete()

        resp_body = _decompress(raw, content_encoding).decode("utf-8", errors="replace")
        if entry is not None:
            entry.record(status_code)
            entry.add_response(resp_body)
        _log_upstream_error(pname, model, status_code, resp_body)
        if model != "unknown":
            ERRORS_TOTAL.labels(provider=pname, model=model, status_code=str(status_code)).inc()

        resp_headers.pop("content-length", None)
        resp_headers.pop("transfer-encoding", None)
        resp_headers.pop("content-encoding", None)
        return Response(
            content=resp_body,
            status_code=status_code,
            headers=resp_headers,
            background=BackgroundTask(
                _emit_request_log, request, pname, model, status_code, 0, 0, 0.0, True,
            ),
        )

    async def generate():
        # Runs in Starlette's body task, not the request task — bind it so a kill
        # cancels the generator rather than the disconnect listener above it.
        if entry is not None:
            entry.bind_stream()
        in_tokens = 0
        out_tokens = 0
        buffer = ""
        delta_contents = [] if conf.LOG_OUTPUT else None
        reasoning_contents = [] if conf.LOG_OUTPUT else None
        final_chunk = None
        resp_model = model
        status_code = resp.status_code
        error = resp.is_error

        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
                text = chunk.decode("utf-8", errors="replace")
                buffer += text
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        continue
                    if entry is not None:
                        entry.chunk()
                    try:
                        data = json.loads(payload)
                        resp_model = data.get("model", resp_model)
                        usage = data.get("usage")
                        if usage:
                            in_tokens = usage.get("prompt_tokens", in_tokens)
                            out_tokens = usage.get("completion_tokens", out_tokens)
                            final_chunk = data
                        choices = data.get("choices") or []
                        delta = choices[0].get("delta") or {} if choices else {}
                        content = delta.get("content") or ""
                        reasoning = delta.get("reasoning_content") or ""
                        if entry is not None and (content or reasoning):
                            # One delta carrying text == one generation step. The
                            # only live token signal there is; see Entry.token.
                            entry.token()
                            entry.add_response(content)
                            entry.add_response(reasoning, reasoning=True)
                        if delta_contents is not None:
                            if content:
                                delta_contents.append(content)
                            if reasoning:
                                reasoning_contents.append(reasoning)
                    except json.JSONDecodeError:
                        pass
        except asyncio.CancelledError:
            # Killed from the console. The 200 was committed with the first chunk,
            # so there is no status code left to send the client — ending the SSE
            # stream here is all a kill can mean. Returning instead of propagating
            # keeps uvicorn from logging a deliberate operator action as an
            # "Exception in ASGI application" traceback. `error` stays True so the
            # truncated generation isn't recorded as throughput metrics.
            error = True
            if entry is None or not entry.cancelled:
                # Shutdown, or the client vanished — not ours to swallow.
                raise
            logger.info(
                f"Stream for request #{entry.id} cut after {entry.chunks} chunk(s) — cancelled"
            )
        except Exception as e:
            error = True
            logger.error(f"Stream error from '{pname}' (model: {model}): {type(e).__name__}: {e}")
        finally:
            # Bookkeeping first, and all of it sync, so none of it can be skipped
            # by an await below raising. This is the stream's single close point
            # for the in-flight entry: proxy_request hands it off to us, since the
            # handler returns long before the body is done. record-then-finish, so
            # the history row carries the outcome and not just "it ended".
            duration = time.time() - start
            if entry is not None:
                entry.record(status_code, in_tokens, out_tokens)
                entry.finish()
            await stream_cm.__aexit__(None, None, None)
            await client.aclose()
            if on_complete is not None:
                await on_complete()
            if model != "unknown":
                if error:
                    ERRORS_TOTAL.labels(provider=pname, model=model, status_code=str(status_code)).inc()
                else:
                    _record_metrics(pname, model, in_tokens, out_tokens, duration)
            await _emit_request_log(
                request, pname, model, status_code, in_tokens, out_tokens, duration, True,
            )
            if delta_contents is not None:
                full_text = "".join(delta_contents)
                reasoning_text = "".join(reasoning_contents) if reasoning_contents else ""
                log_data = {}
                if final_chunk:
                    log_data = final_chunk
                else:
                    log_data = {
                        "model": resp_model,
                        "usage": {"prompt_tokens": in_tokens, "completion_tokens": out_tokens},
                    }
                log_data["_assembled_content"] = full_text
                if reasoning_text:
                    log_data["_reasoning_content"] = reasoning_text
                logger.info(
                    f"Stream response ({status_code}):\n{json.dumps(log_data, indent=2, ensure_ascii=False)}"
                )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _routing_for(provider: Provider, model: str):
    """Resolve the upstream routing object for a model, or None.

    Looks up `provider.provider_routing` by resolved model id, falling back to
    a "*" default. A list value pins strictly (order + allow_fallbacks=false);
    a dict is passed through verbatim (full OpenRouter `provider` control).
    """
    routing = provider.provider_routing
    if not routing:
        return None
    spec = routing.get(model, routing.get("*"))
    if isinstance(spec, list):
        return {"order": spec, "allow_fallbacks": False}
    if isinstance(spec, dict):
        return spec
    return None


def _build_body(payload: dict, provider: Provider, model: str):
    """Rewrite the request body for a chosen target: set the real model id,
    inject upstream routing (OpenRouter `provider`), and ask for usage on streams.

    Each defers to the client: an explicit `provider` or `stream_options` wins.
    """
    p = dict(payload)
    p["model"] = model
    if "provider" not in p:
        routing = _routing_for(provider, model)
        if routing is not None:
            p["provider"] = routing
    # For streaming, ask the upstream to emit a final `usage` chunk so our token
    # metrics are reliable without depending on the client to request it.
    if p.get("stream"):
        opts = dict(p.get("stream_options") or {})
        opts.setdefault("include_usage", True)
        p["stream_options"] = opts
    # Drop fields a strict backend would reject (e.g. Google 400s on `num_ctx`).
    for f in provider.strip_fields:
        p.pop(f, None)
    body = json.dumps(p, ensure_ascii=False).encode("utf-8")
    return body, body.decode("utf-8")


def _should_failover(status: int) -> bool:
    """Whether an upstream HTTP error status should trigger trying the next target.

    A backend that *answered* with 503/500/429 is as unusable for this request as
    one we couldn't reach at all, so it gets the same failover treatment as a
    connection error. A deliberate 4xx (bad request, auth) is left out — every
    backend would reject it identically, so relay it instead of burning retries.
    """
    return conf.ROUTING.failover and status in conf.ROUTING.failover_statuses


async def _dispatch(
    request: Request, path: str, payload: dict, is_stream: bool, targets: list, entry
) -> Union[Response, StreamingResponse]:
    """Acquire a slot on the best available target and forward, failing over to
    the next-priority target when a backend errors out (when failover is enabled).

    Failover fires on a connection-level `httpx.RequestError` *and* on a retryable
    upstream HTTP status (`Routing.failover_statuses`, e.g. 503). Once targets are
    exhausted the client gets the last upstream error verbatim — its real status
    and body, not a synthetic 502 — so the reason for the failure survives.
    """
    remaining = list(targets)
    last_provider = None
    last_exc = None

    while remaining:
        # Mark queued *before* acquiring: for a request that has to wait, this is
        # the only moment we can record what it is waiting on. Re-armed on every
        # iteration so a failover shows up as queued again, with the shortened
        # candidate list.
        entry.wait(remaining)
        try:
            target = await slots.acquire(
                remaining, conf.ROUTING.queue_timeout, on_skip=entry.passed_over
            )
        except slots.SlotTimeout:
            return Response(
                content=json.dumps({"error": {"message": "No backend slot available (queue timeout)", "type": "slot_timeout", "code": 503}}),
                status_code=503,
                media_type="application/json",
            )

        provider = conf.PROVIDERS_BY_NAME.get(target.provider)
        if provider is None:
            # The config hot-reloaded away this provider between resolution and
            # admission — now genuinely possible, since the console can delete a
            # backend while a request is queued for it. Give the slot back and
            # move on rather than raising a KeyError into the client.
            await slots.release(target.provider, target.model)
            remaining = [t for t in remaining if t.provider != target.provider]
            logger.warning(
                "Provider '%s' disappeared from the config while a request was "
                "queued for it; %d target(s) left for model '%s'",
                target.provider, len(remaining), target.model,
            )
            continue
        last_provider = provider
        entry.run(target.provider, target.model)
        body, body_str = _build_body(payload, provider, target.model)

        try:
            if is_stream:
                async def _release(p=target.provider, m=target.model):
                    await slots.release(p, m)

                # Slot is released by the generator's finally once streaming ends,
                # or — on a pre-first-byte error — via on_complete inside the handler.
                resp = await _handle_stream(
                    request, provider, path, body, body_str, target.model,
                    on_complete=_release, entry=entry,
                )
            else:
                resp = await _handle_non_stream(
                    request, provider, path, body, body_str, target.model, entry=entry
                )
                await slots.release(target.provider, target.model)
        except asyncio.CancelledError:
            # An operator kill (or a client disconnect) while we were forwarding.
            # Nothing has released the slot on this path: the non-stream branch
            # never reached its release, and for a stream the generator that owns
            # the release never ran. Release here, then keep unwinding.
            await slots.release(target.provider, target.model)
            raise
        except httpx.RequestError as e:
            await slots.release(target.provider, target.model)
            last_exc = e
            if not conf.ROUTING.failover:
                return _backend_error(provider, target.model, e)
            registry.mark_down(target.provider, conf.ROUTING.down_backoff)
            FAILOVERS_TOTAL.labels(provider=target.provider).inc()
            remaining = [t for t in remaining if t.provider != target.provider]
            logger.warning(
                f"Failover: '{target.provider}' failed ({type(e).__name__}); "
                f"{len(remaining)} target(s) left for model '{target.model}'"
            )
            continue

        # The backend answered with a retryable error status (the connection was
        # fine, the response wasn't). Try the next target if any remain; otherwise
        # fall through and relay this error to the client verbatim.
        if _should_failover(resp.status_code):
            others = [t for t in remaining if t.provider != target.provider]
            if others:
                registry.mark_down(target.provider, conf.ROUTING.down_backoff)
                FAILOVERS_TOTAL.labels(provider=target.provider).inc()
                remaining = others
                logger.warning(
                    f"Failover: '{target.provider}' returned {resp.status_code}; "
                    f"{len(others)} target(s) left for model '{target.model}'"
                )
                continue

        # A genuinely good response clears any down-mark; an error we're relaying
        # (terminal, no targets left) must not — the backend is still unhealthy.
        if resp.status_code < 400:
            registry.clear_down(target.provider)
        return resp

    # Every candidate failed with a connection error.
    if last_exc is None:
        last_exc = httpx.ConnectError("no backends available")
    return _backend_error(last_provider, "unknown", last_exc)


def _cancelled_response() -> Response:
    """Terminal response for a request killed from the console. 503 with an
    OpenAI-shaped error body, matching the slot-timeout response, so clients
    handle it as an ordinary upstream failure rather than a protocol surprise."""
    payload = {
        "error": {
            "message": "Request cancelled by the proxy operator",
            "type": "cancelled",
            "code": 503,
        }
    }
    return Response(content=json.dumps(payload), status_code=503, media_type="application/json")


def _model_not_found(model: str, request: Request) -> Response:
    """404 for a model no backend is known to serve.

    Shaped like OpenAI's own unknown-model error (`invalid_request_error` /
    `model_not_found`) so off-the-shelf clients surface the message instead of
    showing a bare failure. Logged with the caller, because the interesting
    question is always *who* asked for a name the proxy doesn't have — a typo, a
    stale client-side model list, or a backend that dropped out of discovery.
    """
    ip = clientinfo.client_ip(request)
    ua = (request.headers.get("user-agent") or "").strip() or None
    logger.warning(
        "Model '%s' is not available — no backend serves it (client %s, svc %s). "
        "Returning 404 rather than guessing a backend.",
        model, ip, clientinfo.service_from_ua(ua),
    )
    payload = {
        "error": {
            "message": (
                f"The model '{model}' does not exist or is not available on this proxy"
            ),
            "type": "invalid_request_error",
            "param": "model",
            "code": "model_not_found",
        }
    }
    return Response(content=json.dumps(payload), status_code=404, media_type="application/json")


def _unauthorized(model: str) -> Response:
    payload = {
        "error": {
            "message": f"Model '{model}' requires authentication" if model else "Authentication required",
            "type": "unauthorized",
            "code": 401,
        }
    }
    return Response(content=json.dumps(payload), status_code=401, media_type="application/json")


async def proxy_request(request: Request, path: str) -> Union[Response, StreamingResponse]:
    """Entry point for every proxied call. Parses the body once, registers the
    request in the in-flight view, then hands off to `_route`.

    The in-flight entry is closed here for anything that returns a finished
    response. A `StreamingResponse` is the one exception: it is still in flight
    when we return it, so `_handle_stream`'s generator closes the entry in its
    `finally` — the same place the slot is released.
    """
    body = await request.body()
    body_str = body.decode("utf-8", errors="replace")

    payload = None
    raw_model = None
    is_stream = False
    try:
        payload = json.loads(body_str)
        raw_model = payload.get("model")
        is_stream = payload.get("stream", False)
    except (json.JSONDecodeError, AttributeError):
        # Not a JSON object (or not JSON at all): treated as a passthrough below.
        payload = None

    ua = (request.headers.get("user-agent") or "").strip() or None
    entry = inflight.begin(
        model=raw_model or None,
        stream=bool(is_stream),
        op=_op_kind(request.url.path, raw_model or ""),
        method=request.method,
        path=request.url.path,
        req_bytes=len(body),
        client_ip=clientinfo.client_ip(request),
        svc=clientinfo.service_from_ua(ua),
    )
    entry.set_request(body_str)
    try:
        resp = await _route(request, path, body, body_str, payload, entry)
    except asyncio.CancelledError:
        entry.finish()
        if not entry.cancelled:
            # The client went away (or we're shutting down): normal cancellation,
            # let it propagate untouched.
            raise
        # Killed from the console. Answer the client rather than letting uvicorn
        # turn the cancellation into a 500 with a traceback. Safe to stop the
        # unwind here: the cancellation has already been delivered, so the awaits
        # needed to send this response still run.
        logger.warning(
            f"Request #{entry.id} ({entry.model or entry.path}"
            f"{' on ' + entry.provider if entry.provider else ''}) cancelled from the console"
        )
        return _cancelled_response()
    except BaseException:
        entry.finish()
        raise
    if not isinstance(resp, StreamingResponse):
        entry.finish(fallback_status=resp.status_code)
    return resp


async def _route(
    request: Request, path: str, body: bytes, body_str: str, payload, entry
) -> Union[Response, StreamingResponse]:
    """Resolve the model, apply the auth gate and dispatch. `payload` is the
    already-parsed JSON body (a dict, or None for a non-JSON passthrough)."""
    authorized = auth.is_authorized(request)
    raw_model = payload.get("model") if payload is not None else None
    is_stream = payload.get("stream", False) if payload is not None else False

    # No JSON model (e.g. non-chat passthrough): forward to the first provider
    # the caller is actually allowed to use, untouched and without slot gating.
    # First *permitted*, not first configured: picking PROVIDERS[0] blindly meant
    # an unauthenticated caller got "Authentication required" whenever the config
    # happened to list a paid backend first — another 401 about a backend they
    # never chose.
    if not raw_model:
        if not conf.PROVIDERS:
            return Response(content="No providers configured", status_code=503)
        provider = next(
            (p for p in conf.PROVIDERS if authorized or not p.require_permission), None
        )
        if provider is None:
            return _unauthorized("")
        # No slot is taken on this path, so it is never queued — running from the
        # moment it is forwarded.
        entry.run(provider.name, None)
        try:
            if is_stream:
                return await _handle_stream(
                    request, provider, path, body, body_str, "unknown", entry=entry
                )
            return await _handle_non_stream(
                request, provider, path, body, body_str, "unknown", entry=entry
            )
        except httpx.RequestError as e:
            return _backend_error(provider, "unknown", e)

    targets = await router.resolve(raw_model)
    if not targets:
        if not conf.PROVIDERS:
            return Response(content="No providers configured", status_code=503)
        # Nothing serves this model. Say so — never fall back to an arbitrary
        # backend, which is how a local-model request used to surface as a 401
        # from a paid one.
        return _model_not_found(raw_model, request)

    # Gate: unauthenticated callers can only reach open backends. If the model
    # lives solely behind permission-required backends, reject with 401.
    if not authorized:
        targets = [t for t in targets if not auth.restricted(t.provider)]
        if not targets:
            return _unauthorized(raw_model)

    # Prefer backends not currently marked down, but keep them as a last resort.
    healthy = [t for t in targets if not registry.is_down(t.provider)]
    candidates = healthy or targets

    return await _dispatch(request, path, payload, is_stream, candidates, entry)
