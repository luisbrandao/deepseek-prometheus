import logging
from typing import List

from app import config as conf
from app import registry

logger = logging.getLogger("llm-proxy")


def _explicit(raw: str):
    """`provider:model` -> a single forced target (manual override / back-compat).

    The `model` part is a canonical name; it is reverse-mapped to the provider's
    native id for the wire.
    """
    sep = conf.PROVIDER_SEP
    if sep in raw:
        prefix, _, rest = raw.partition(sep)
        provider = conf.PROVIDERS_BY_NAME.get(prefix)
        if provider:
            return [conf.Target(provider.name, provider.to_native(rest), provider.priority)]
    return None


def _from_logical(raw: str):
    """An explicit `models:` entry -> its prioritized targets.

    A target's native id is its explicit `model` if set, otherwise the logical
    (canonical) name reverse-mapped through that provider's model_map.
    """
    lm = conf.LOGICAL_MODELS.get(raw)
    if not lm or not lm.targets:
        return None
    out = []
    for t in lm.targets:
        p = conf.PROVIDERS_BY_NAME.get(t.provider)
        if p is None:
            # A target naming a provider that isn't configured — a half-finished
            # config edit (the provider block removed, the target left behind).
            # It can never be served, and keeping it would hand the dispatcher a
            # provider it cannot look up. Drop it here; the console flags it as
            # `known_provider: false` so it stays visible and fixable.
            logger.warning(
                "Model '%s' has a target on unknown provider '%s' — skipping it. "
                "Remove the target, or re-add the provider.",
                raw, t.provider,
            )
            continue
        model = conf.native_for(lm.name, t.provider, t.model)
        out.append(conf.Target(t.provider, model, t.priority))
    # Every target dangling: treat the entry as absent so resolution can still
    # fall through to auto-group rather than dead-ending on a broken block.
    return out or None


async def _auto_group(raw: str):
    """Every provider whose catalog includes this canonical model, by priority.

    Matches on the canonical name (native ids translated via model_map) but the
    resolved target carries the provider's native id for the wire.
    """
    targets = []
    for p in conf.PROVIDERS:
        for native in await registry.provider_model_ids(p):
            if p.to_canonical(native) == raw:
                targets.append(conf.Target(p.name, native, p.priority))
                break
    targets.sort(key=lambda t: t.priority)
    return targets


def _allow_listed(raw: str):
    """First provider whose `enabled_models` explicitly serves this canonical model.

    Only reachable with `auto_group: false`. With auto-grouping on — the default —
    `_auto_group` already covers every name this could match: for a provider with
    an explicit allow-list it iterates exactly `enabled_models`, and since
    `model_map` is a bijection, testing `to_canonical(native) == raw` accepts the
    same set of names as testing `to_native(raw) in enabled_models`. The
    difference is only multiplicity — auto-group returns every provider that
    serves the name, this returns the first.
    """
    for p in conf.PROVIDERS:
        native = p.to_native(raw)
        if native in p.enabled_models:
            return [conf.Target(p.name, native, p.priority)]
    return []


async def resolve(raw_model: str) -> List[conf.Target]:
    """Resolve a client-supplied model name into prioritized targets.

    Order: alias expansion -> explicit `provider:model` -> explicit `models:`
    entry -> auto-grouped identical ids -> allow-listed single provider.

    Returns an empty list when no backend is known to serve the model, and there
    is deliberately **no** last-resort guess after the final step.

    Sending an unresolved model to `PROVIDERS[0]` used to look like graceful
    degradation and was in fact a trap: a caller asking for a local model during a
    momentary discovery blip got silently routed to whichever backend happened to
    be listed first. When that backend is `require_permission`, an unauthenticated
    caller — who can neither see nor use it — receives a *401 about a model they
    never asked for*, which is unexplainable from the client side. It happened in
    production. A model nothing is known to serve resolves to nothing, and the
    caller gets a straight 404 (`proxy._model_not_found`). Callers must not
    substitute a backend of their own choosing.
    """
    raw = conf.ALIASES.get(raw_model, raw_model)

    explicit = _explicit(raw)
    if explicit is not None:
        return explicit

    logical = _from_logical(raw)
    if logical is not None:
        return logical

    if conf.ROUTING.auto_group:
        return await _auto_group(raw)

    # Auto-grouping off: fall back to a single allow-listed provider. With it on,
    # _auto_group is a strict superset of this (see _allow_listed).
    return _allow_listed(raw)
