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
        model = t.model if t.model is not None else p.to_native(lm.name)
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
    """First provider whose `enabled_models` explicitly serves this canonical
    model. The wire id is that provider's native mapping.

    There is deliberately **no** last-resort guess after this. Sending an
    unresolved model to `PROVIDERS[0]` used to look like graceful degradation and
    was in fact a trap: a caller asking for a local model during a momentary
    discovery blip got silently routed to whichever backend happened to be first
    in the config. If that backend is `require_permission`, an unauthenticated
    caller — who can neither see nor use it — receives a *401 about a model they
    never asked for*. That is unexplainable from the client side, and it happened
    in production. A model nothing is known to serve now resolves to nothing, and
    the caller gets a straight 404 (see `proxy._model_not_found`).
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

    Returns an empty list when no backend is known to serve the model. Callers
    must treat that as "not available" and must not substitute a backend of their
    own choosing; see `_allow_listed` for what that used to cost.
    """
    raw = conf.ALIASES.get(raw_model, raw_model)

    explicit = _explicit(raw)
    if explicit is not None:
        return explicit

    logical = _from_logical(raw)
    if logical is not None:
        return logical

    if conf.ROUTING.auto_group:
        grouped = await _auto_group(raw)
        if grouped:
            return grouped

    return _allow_listed(raw)
