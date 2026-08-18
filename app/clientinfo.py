"""Best-effort identification of the calling client, for the per-request log.

Pulls out three things, none of which gate the request — they exist purely to
make a log line answer "who called, from which machine, with what tool":

* `client_ip`   — the caller's address, honoring `X-Forwarded-For` /
  `X-Real-IP` when proxy headers are trusted (with `network_mode: host` and no
  front proxy this is already the real LAN IP).
* `client_host` — reverse-DNS of that IP, resolved off the event loop, time
  bounded and cached (positive and negative), so a chatty client triggers at
  most one lookup per TTL. Falls back to None when DNS has nothing.
* service token — the leading product of the `User-Agent`, a rough "what app".
"""
import asyncio
import socket
import time
from typing import Optional

from fastapi import Request
from fastapi.concurrency import run_in_threadpool

from app import config as conf

# ip -> (hostname_or_None, expiry_monotonic).
_dns_cache: dict = {}
_POS_TTL = 3600.0
_NEG_TTL = 300.0


def client_ip(request: Request) -> str:
    if conf.TRUST_PROXY_HEADERS:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
        xri = request.headers.get("x-real-ip")
        if xri:
            return xri.strip()
    return request.client.host if request.client else "-"


def _lookup(ip: str) -> Optional[str]:
    """Blocking reverse DNS, result cached. Runs in a worker thread so a slow
    or black-holing resolver never stalls the event loop."""
    try:
        host = socket.gethostbyaddr(ip)[0]
    except OSError:  # herror/gaierror/timeout all subclass OSError
        host = None
    _dns_cache[ip] = (host, time.monotonic() + (_POS_TTL if host else _NEG_TTL))
    return host


async def client_host(ip: str) -> Optional[str]:
    if not conf.RESOLVE_CLIENT_HOST or not ip or ip == "-":
        return None
    cached = _dns_cache.get(ip)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    try:
        return await asyncio.wait_for(run_in_threadpool(_lookup, ip), conf.CLIENT_DNS_TIMEOUT)
    except asyncio.TimeoutError:
        # The worker keeps running and populates the cache for next time; we
        # just don't block this request's log line waiting on it.
        return None


def cached_host(ip: str) -> Optional[str]:
    """Reverse-DNS name for `ip` if it is already cached, else None.

    A non-blocking peek, for callers that must never wait on a resolver — the
    in-flight snapshot renders on every console poll. The per-request log's own
    `client_host` lookup keeps the cache warm, so a repeat caller usually has a
    name here anyway.
    """
    cached = _dns_cache.get(ip)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    return None


def service_from_ua(ua: Optional[str]) -> Optional[str]:
    """Short service token from the User-Agent's leading product, e.g.
    'OpenWebUI/0.5 (extra)' -> 'OpenWebUI'. Heuristic, best-effort."""
    if not ua:
        return None
    return ua.strip().split()[0].split("/")[0] or None
