"""Shared httpx clients for every outbound call, and the timeouts they use.

Two clients, both process-wide: one for forwarding requests, one for the short
discovery probes. They exist so connections are **reused**. Building an
`AsyncClient` per request — which this module replaced — threw the connection
pool away after a single call, so every request to a remote backend paid a fresh
TCP handshake plus a full TLS negotiation before the prompt went out. Local
backends barely noticed; DeepSeek/OpenRouter/Google paid it on every call.

## Why the timeouts are split

`httpx.Timeout(600.0)` sets *every* phase to 600s — connect included. That is
almost right and one part badly wrong:

* **read: 600s is correct and deliberate.** A large model loading, or processing
  a long prompt, routinely spends minutes before its first byte. A backend that
  buffers the whole completion (an aggregator) spends all of it there.
* **connect: 600s is a trap.** A TCP handshake completes in the kernel before the
  request is even sent, so no amount of model-loading time lands here. A host
  that *refuses* fails instantly; the only way to wait on connect is a host that
  black-holes SYN — powered off behind a router, a firewall DROP rule, a box off
  the wifi — and that connection is never going to succeed. Meanwhile the request
  holds its slot for the whole wait. On a single-slot local backend that is a
  ten-minute outage caused by one request, and `down_backoff` never engages
  because nothing has failed yet.

So: long read, short connect. `registry`'s probe timeout was already right and
is kept here next to its sibling.

## Pool notes

`keepalive_expiry` is deliberately below the idle timeout typical of upstream
servers and CDNs, so we drop an idle connection before the far end does. When we
lose that race anyway, httpx raises `RemoteProtocolError` — a subclass of
`RequestError` — which the dispatcher already treats as a connection failure and
fails over on, so a stale pooled connection degrades to the existing retry path
rather than to an error the client sees.

Both clients are created lazily so they bind to uvicorn's running loop rather
than the import-time one, matching `registry._lock_for` and `slots._Waiter`.
`aclose()` is called from `main`'s lifespan on shutdown.
"""
import httpx


# Forwarding: minutes of read for generation, seconds of connect for a handshake.
FORWARD_TIMEOUT = httpx.Timeout(600.0, connect=5.0)

# Live model discovery. Short on purpose: a powered-off backend must fail fast
# instead of stalling the listing for a long default.
PROBE_TIMEOUT = httpx.Timeout(5.0, connect=3.0)

# Generous but bounded. The proxy's own slot budgets are the real concurrency
# limit; this only stops a pathological case from opening sockets without end.
FORWARD_LIMITS = httpx.Limits(
    max_connections=200,
    max_keepalive_connections=50,
    keepalive_expiry=45.0,
)

_forward = None
_probe = None


def forward_client() -> httpx.AsyncClient:
    """The shared client for proxied requests. Never `aclose()` this from a
    request path — it is process-wide and closed by the lifespan."""
    global _forward
    if _forward is None:
        _forward = httpx.AsyncClient(timeout=FORWARD_TIMEOUT, limits=FORWARD_LIMITS)
    return _forward


def probe_client() -> httpx.AsyncClient:
    """The shared client for `/v1/models` discovery probes."""
    global _probe
    if _probe is None:
        _probe = httpx.AsyncClient(timeout=PROBE_TIMEOUT)
    return _probe


async def aclose() -> None:
    """Close both pools. Called once, from the lifespan's shutdown side."""
    global _forward, _probe
    for client in (_forward, _probe):
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
    _forward = None
    _probe = None
