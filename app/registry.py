import asyncio
import logging
import time

from app import config as conf
from app import upstream

logger = logging.getLogger("llm-proxy")

# provider name -> (expires_at_epoch, [model_id, ...])
_cache = {}

# provider name -> (fetched_at_epoch, [model_id, ...]) for the last probe that
# actually succeeded, used to ride out a failing one.
_last_good = {}

# How long a provider's last successful catalog stands in for a failing probe.
# A local backend mid-model-swap, or one momentary timeout, must not blank its
# entire model list: that made every model on that box unresolvable for a full
# cache_ttl — and until the router stopped guessing, sent those requests to
# whichever backend happened to be first in the config. After the grace window a
# backend that is still failing really is gone, and its models drop out.
STALE_GRACE = 300.0

# Re-probe interval while serving a stale catalog. Deliberately shorter than
# cache_ttl: we are knowingly serving something we could not confirm, so we want
# to correct it quickly — but not on every request, which would hammer a dead
# backend.
STALE_RETRY = 15.0

# provider name -> asyncio.Lock, created lazily so each binds to the running
# loop. Coalesces concurrent cold-cache misses into a single upstream probe
# (single-flight) instead of letting every in-flight request fetch in parallel.
_locks = {}


def _lock_for(provider_name: str) -> asyncio.Lock:
    lock = _locks.get(provider_name)
    if lock is None:
        lock = asyncio.Lock()
        _locks[provider_name] = lock
    return lock

# provider name -> epoch until which the backend is considered down. Set when a
# request fails against it so the router can skip it during failover, and
# cleared on the next success.
_down_until = {}


def clear_cache() -> None:
    """Drop every cached live-discovery list. Called after a config reload:
    a provider's base_url / enabled_models / cache_ttl may have changed, so
    the next request re-probes instead of serving a list from the old config.

    The last-known-good catalogs go too — a changed base_url points somewhere
    else entirely, and a stale list from the previous endpoint would be worse
    than no list at all.
    """
    _cache.clear()
    _last_good.clear()


def mark_down(provider_name: str, seconds: float) -> None:
    _down_until[provider_name] = time.time() + seconds


def clear_down(provider_name: str) -> None:
    _down_until.pop(provider_name, None)


def is_down(provider_name: str) -> bool:
    return _down_until.get(provider_name, 0) > time.time()


async def provider_model_ids(provider: conf.Provider):
    """Effective model ids a provider serves: configured list or live-discovered."""
    if provider.lists_all:
        return await _cached_live(provider)
    return list(provider.enabled_models)


async def _fetch_live(provider: conf.Provider):
    url = f"{provider.base_url}/{conf.strip_prefix(provider, 'v1/models')}"
    headers = {}
    if provider.api_key:
        headers["authorization"] = f"Bearer {provider.api_key}"
    # Shared client (see app/upstream.py): probes reuse connections and carry
    # the short probe timeout, so a powered-off backend fails fast instead of
    # blocking the listing.
    resp = await upstream.probe_client().get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return [m["id"] for m in data.get("data", []) if m.get("id")]


async def _cached_live(provider: conf.Provider):
    entry = _cache.get(provider.name)
    if entry and entry[0] > time.time():
        return entry[1]

    # Single-flight: only the first concurrent miss probes; the rest wait here
    # and pick up the fresh cache below, instead of stampeding the backend.
    async with _lock_for(provider.name):
        entry = _cache.get(provider.name)
        if entry and entry[0] > time.time():
            return entry[1]
        try:
            ids = await _fetch_live(provider)
            _last_good[provider.name] = (time.time(), ids)
            ttl = provider.cache_ttl
        except Exception as e:
            # Probe failed. Prefer this backend's last known catalog over
            # declaring it modelless: a swap-induced timeout is far more common
            # than a box actually losing its models, and blanking the list makes
            # every model on it unresolvable (see STALE_GRACE). Re-probe sooner
            # than usual while we are serving something unconfirmed.
            stale = _last_good.get(provider.name)
            if stale and time.time() - stale[0] <= STALE_GRACE:
                ids = stale[1]
                ttl = min(provider.cache_ttl, STALE_RETRY)
                logger.warning(
                    "Model discovery failed for %s (%s: %s); serving its last known "
                    "catalog of %d model(s) and re-probing in %.0fs",
                    provider.name, type(e).__name__, e, len(ids), ttl,
                )
            else:
                # Never seen a catalog, or the last one is too old to trust.
                # Backends are expected to come and go, so this is not an error.
                logger.warning(f"Failed to fetch models from {provider.name}: {e}")
                ids = []
                ttl = provider.cache_ttl
        # Stamp expiry AFTER the probe (which may have been slow), so we don't
        # re-hit a slow backend on every request.
        _cache[provider.name] = (time.time() + ttl, ids)
        return ids


async def list_models(authorized: bool = True) -> dict:
    """Aggregate models, presenting clean server-less names clients can use directly.

    Listed (deduped, in this order): aliases, explicit logical models, and bare
    model ids (a model served by several backends appears once). Backend-prefixed
    `provider:model` ids are not listed — they still work for pinning a specific
    backend, but advertising them would just duplicate the clean names. Offline
    backends drop out.

    When `authorized` is False, backends with `require_permission` are excluded:
    their models are hidden, and shared models are listed with only the open
    backends as owners.
    """
    sep = conf.PROVIDER_SEP
    data = []
    seen = set()

    def visible(provider_name: str) -> bool:
        p = conf.PROVIDERS_BY_NAME.get(provider_name)
        return authorized or not (p and p.require_permission)

    def add(mid: str, owner: str):
        if mid in seen:
            return
        seen.add(mid)
        data.append({"id": mid, "object": "model", "owned_by": owner})

    # Aliases first (e.g. `chat` -> deepseek:deepseek-chat).
    for alias, target in conf.ALIASES.items():
        owner = target.split(sep, 1)[0] if sep in target else "alias"
        if sep in target and not visible(owner):
            continue
        add(alias, owner)

    # Explicit logical models (may span backends with differing real ids). Listed
    # if at least one of its targets is visible to the caller.
    for name, lm in conf.LOGICAL_MODELS.items():
        owners = [t.provider for t in lm.targets if visible(t.provider)]
        if not owners:
            continue
        add(name, ",".join(owners) or "logical")

    # Things a logical model already fronts, hidden from the flat list below so
    # clients use the stable logical name and the underlying native ids (e.g.
    # per-quant variants) don't flap in/out as backends come and go. We hide by
    # canonical name (covers model_map-inherited targets) and by the concrete
    # (provider, native id) of each target (covers explicit per-quant overrides).
    logical_names = set(conf.LOGICAL_MODELS.keys())
    logical_target_natives = set()
    for lm in conf.LOGICAL_MODELS.values():
        for t in lm.targets:
            p = conf.PROVIDERS_BY_NAME.get(t.provider)
            native = t.model if t.model is not None else (p.to_native(lm.name) if p else lm.name)
            logical_target_natives.add((t.provider, native))

    # Probe only the backends visible to this caller, concurrently.
    live_providers = [p for p in conf.PROVIDERS if p.lists_all and visible(p.name)]
    live_results = await asyncio.gather(
        *(_cached_live(p) for p in live_providers),
        return_exceptions=True,
    )
    live_ids = {}
    for provider, result in zip(live_providers, live_results):
        if isinstance(result, Exception):
            logger.warning(f"Model discovery failed for {provider.name}: {result}")
            live_ids[provider.name] = []
        else:
            live_ids[provider.name] = result

    # Group each model by its canonical name (native ids translated through the
    # provider's model_map) across the visible backends that serve it, skipping
    # names a logical model already fronts.
    by_model = {}
    for provider in conf.PROVIDERS:
        if not visible(provider.name):
            continue
        ids = live_ids.get(provider.name, []) if provider.lists_all else provider.enabled_models
        for native in ids:
            if (provider.name, native) in logical_target_natives:
                continue
            canon = provider.to_canonical(native)
            if canon in logical_names:
                continue
            by_model.setdefault(canon, []).append(provider.name)

    # Each canonical name once. When several backends serve it, they share the
    # entry (and the proxy load-balances behind it).
    for canon, owners in by_model.items():
        add(canon, ",".join(owners) if len(owners) > 1 else owners[0])

    return {"object": "list", "data": data}
