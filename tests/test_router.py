"""Model resolution, and the one rule that matters most: never guess a backend.

`resolve` returning `[]` means "nothing serves this model" and must stay empty.
The last-resort fallback to `PROVIDERS[0]` that used to sit at the end of the
chain caused a production incident: an unauthenticated caller asking for a free
local model, during a momentary discovery blip, was routed to whichever backend
happened to be listed first — a `require_permission` one — and got a 401 about a
model they could neither see nor name. Test it explicitly, because the bug looks
like graceful degradation right up until it isn't.
"""
import pytest

from app import config as conf
from app import registry, router


@pytest.fixture
def no_network(monkeypatch):
    """Live discovery answers from `enabled_models` only — no sockets."""
    async def fake_cached_live(provider):
        return list(provider.enabled_models)

    monkeypatch.setattr(registry, "_cached_live", fake_cached_live)
    return fake_cached_live


CONFIG = """\
models:
  grouped:
    targets:
      - {provider: alpha, priority: 1}
      - {provider: beta,  priority: 2}
  pinned:
    targets:
      - {provider: alpha, model: "vendor/Pinned-Model:q8", priority: 1}

providers:
  - name: alpha
    base_url: "http://127.0.0.1:1/v1"
    enabled_models: ["vendor/Alpha-Grouped", "vendor/Pinned-Model:q8", "vendor/alpha-only"]
    model_map:
      "vendor/Alpha-Grouped": grouped
      "vendor/alpha-only": alpha-only

  - name: beta
    base_url: "http://127.0.0.1:2/v1"
    require_permission: true
    enabled_models: ["other/beta-grouped", "other/beta-only"]
    model_map:
      "other/beta-grouped": grouped
      "other/beta-only": beta-only

aliases:
  quick: "alpha:alpha-only"

routing:
  auto_group: true
"""


@pytest.fixture
def cfg(load_config, no_network):
    return load_config(CONFIG)


async def test_unknown_model_resolves_to_nothing(cfg):
    """The whole point: no guessed backend, ever."""
    assert await router.resolve("a-model-nobody-serves") == []


async def test_unknown_model_does_not_fall_back_to_first_provider(cfg):
    """Specifically that it isn't PROVIDERS[0] — the shape of the old bug."""
    targets = await router.resolve("totally-made-up")
    assert not targets
    assert conf.PROVIDERS[0].name == "alpha"  # there *is* a first provider to fall to


async def test_alias_expands_then_resolves(cfg):
    targets = await router.resolve("quick")
    assert [t.provider for t in targets] == ["alpha"]
    # The alias points at `alpha:alpha-only`; the wire id is the native one.
    assert targets[0].model == "vendor/alpha-only"


async def test_explicit_provider_prefix_pins_one_backend(cfg):
    targets = await router.resolve("beta:beta-only")
    assert len(targets) == 1
    assert targets[0].provider == "beta"
    assert targets[0].model == "other/beta-only"


async def test_unknown_prefix_is_not_treated_as_a_provider(cfg):
    """A colon in a model id is ordinary; only a *known* provider name counts."""
    assert await router.resolve("notaprovider:something") == []


async def test_logical_model_inherits_native_ids_per_provider(cfg):
    """One canonical name, two backends, two different native ids on the wire."""
    targets = await router.resolve("grouped")
    got = {t.provider: t.model for t in targets}
    assert got == {"alpha": "vendor/Alpha-Grouped", "beta": "other/beta-grouped"}


async def test_logical_targets_come_back_in_priority_order(cfg):
    targets = await router.resolve("grouped")
    assert [t.provider for t in targets] == ["alpha", "beta"]
    assert [t.priority for t in targets] == [1, 2]


async def test_explicit_pin_overrides_the_model_map(cfg):
    targets = await router.resolve("pinned")
    assert [(t.provider, t.model) for t in targets] == [("alpha", "vendor/Pinned-Model:q8")]


async def test_target_on_a_removed_provider_is_dropped(cfg, monkeypatch):
    """A half-finished config edit — the provider block gone, the target left
    behind — must not hand the dispatcher a provider it cannot look up."""
    monkeypatch.setitem(conf.LOGICAL_MODELS, "orphan", conf.LogicalModel(
        name="orphan", targets=[conf.Target(provider="deleted-backend", priority=1)]
    ))
    assert await router.resolve("orphan") == []


async def test_auto_group_spans_providers_by_canonical_name(cfg, monkeypatch):
    """Two backends serving the same canonical name group into one model."""
    monkeypatch.setattr(conf, "LOGICAL_MODELS", {})  # no explicit entry in the way
    for p in conf.PROVIDERS:
        p.model_map["shared/native-%s" % p.name] = "shared-name"
        p.__post_init__()
        p.enabled_models.append("shared/native-%s" % p.name)

    targets = await router.resolve("shared-name")
    assert {t.provider for t in targets} == {"alpha", "beta"}


async def test_auto_group_disabled_still_resolves_an_allow_listed_model(cfg, monkeypatch):
    """`_allow_listed` is the fallback that only matters with auto_group off —
    the reason it stays in the tree at all."""
    monkeypatch.setattr(conf, "LOGICAL_MODELS", {})
    monkeypatch.setattr(conf, "ROUTING", conf.Routing(auto_group=False))
    targets = await router.resolve("alpha-only")
    assert [t.provider for t in targets] == ["alpha"]
    assert targets[0].model == "vendor/alpha-only"


async def test_discovery_failure_keeps_the_last_known_catalog(load_config, monkeypatch):
    """Caching `[]` on a failed probe made every model on that backend
    unresolvable for a full cache_ttl, which is what triggered the routing bug."""
    load_config("""\
        providers:
          - name: alpha
            base_url: "http://127.0.0.1:1/v1"
            cache_ttl: 60
        routing:
          auto_group: true
        """)
    provider = conf.PROVIDERS_BY_NAME["alpha"]

    calls = {"n": 0}

    async def flaky_fetch(p):
        calls["n"] += 1
        if calls["n"] == 1:
            return ["vendor/model-one"]
        raise RuntimeError("probe timed out mid model-swap")

    monkeypatch.setattr(registry, "_fetch_live", flaky_fetch)

    assert await registry._cached_live(provider) == ["vendor/model-one"]

    # Expire the cache and let the next probe fail.
    registry._cache.clear()
    assert await registry._cached_live(provider) == ["vendor/model-one"], (
        "a failed probe must serve the last known catalog, not an empty list"
    )


async def test_allow_listed_model_resolves_with_auto_group_on(cfg):
    """The refactor folded `_allow_listed` into the auto_group-off branch, so an
    allow-listed model must still resolve through `_auto_group` when it is on."""
    targets = await router.resolve("alpha-only")
    assert [t.provider for t in targets] == ["alpha"]
    assert targets[0].model == "vendor/alpha-only"


async def test_auto_group_subsumes_allow_listed(cfg):
    """The equivalence the refactor rests on: with auto_group on there is no name
    `_allow_listed` can resolve that `_auto_group` cannot.

    Both are driven by `enabled_models`, and `model_map` is a bijection, so
    matching `to_canonical(native) == raw` accepts the same names as matching
    `to_native(raw) in enabled_models`. If that ever stops holding, folding
    `_allow_listed` behind `auto_group: false` would start dropping models.
    """
    for provider in conf.PROVIDERS:
        for native in provider.enabled_models:
            canonical = provider.to_canonical(native)
            grouped = await router._auto_group(canonical)
            allowed = router._allow_listed(canonical)
            assert not (allowed and not grouped), (
                f"'{canonical}' resolves via _allow_listed but not _auto_group"
            )
