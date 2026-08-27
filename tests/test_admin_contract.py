"""The fields the web console reads from each admin view.

`app/static/app.js` is vanilla JS with no build step and no type checking, so a
renamed or dropped field in an admin payload surfaces as a silently blank cell in
the UI rather than as any kind of error. These tests pin the contract.

The field lists below were taken from the console itself — every `p.<field>` and
`t.<field>` it dereferences. Adding a field is fine; removing or renaming one
must break a test here, not a tab.
"""
import pytest
from fastapi.testclient import TestClient

from app import registry

CONFIG = """\
models:
  grouped:
    targets:
      - {provider: alpha, priority: 1}
      - {provider: beta,  model: "other/pinned-beta", priority: 2}

providers:
  - name: alpha
    base_url: "http://127.0.0.1:1/v1"
    slots: 2
    cache_ttl: 30
    strip_path_prefix: "v1"
    api_key: "sk-not-for-export"
    enabled_models: ["vendor/Alpha-Grouped"]
    model_map:
      "vendor/Alpha-Grouped": grouped

  - name: beta
    base_url: "http://127.0.0.1:2/v1"
    require_permission: true

aliases:
  quick: "alpha:grouped"

routing:
  queue_timeout: 0
  failover: true
  auto_group: true
  down_backoff: 15
  queue_affinity: true
  affinity_max_skips: 3
"""

AUTH = {"Authorization": "Bearer test-key"}


@pytest.fixture
def client(load_config, monkeypatch):
    async def fake_cached_live(provider):
        return list(provider.enabled_models)

    monkeypatch.setattr(registry, "_cached_live", fake_cached_live)
    load_config(CONFIG + '\nauth:\n  keys: ["test-key"]\n')
    from app import main
    with TestClient(main.app) as c:
        yield c


# Every p.<field> the console dereferences, per view.
INFLIGHT_PROVIDER_FIELDS = {"name", "slots", "in_use", "is_down", "resident"}
ROUTING_PROVIDER_FIELDS = {
    "name", "base_url", "slots", "in_use", "is_down", "require_permission",
    "lists_all", "priority",
}
CONFIG_PROVIDER_FIELDS = {
    "name", "base_url", "slots", "priority", "require_permission", "cache_ttl",
    "strip_path_prefix", "lists_all", "enabled_models", "model_map", "fronted",
    "has_api_key",
}


def test_inflight_provider_fields(client):
    body = client.get("/admin/inflight", headers=AUTH).json()
    for p in body["providers"]:
        assert INFLIGHT_PROVIDER_FIELDS <= set(p), f"missing: {INFLIGHT_PROVIDER_FIELDS - set(p)}"


def test_inflight_snapshot_fields(client):
    body = client.get("/admin/inflight", headers=AUTH).json()
    assert {"running", "queued", "history", "history_limit", "requests",
            "providers", "queue_timeout", "queue_affinity",
            "affinity_max_skips"} <= set(body)


def test_routing_provider_fields(client):
    body = client.get("/admin/routing", headers=AUTH).json()
    assert {"auto_group", "config_writable", "providers", "logical_models", "aliases"} <= set(body)
    for p in body["providers"]:
        assert ROUTING_PROVIDER_FIELDS <= set(p), f"missing: {ROUTING_PROVIDER_FIELDS - set(p)}"


def test_config_provider_fields(client):
    body = client.get("/admin/config", headers=AUTH).json()
    assert {"path", "writable", "providers", "logical_models", "aliases", "routing"} <= set(body)
    for p in body["providers"]:
        assert CONFIG_PROVIDER_FIELDS <= set(p), f"missing: {CONFIG_PROVIDER_FIELDS - set(p)}"


def test_no_view_leaks_an_api_key(client):
    for path in ("/admin/config", "/admin/routing", "/admin/inflight"):
        text = client.get(path, headers=AUTH).text
        assert "sk-not-for-export" not in text, f"{path} leaked the api_key"
        assert '"api_key"' not in text, f"{path} exposes an api_key field"


def test_routing_targets_report_a_resolved_model(client):
    """The routing view matches a reorder against the resolved native id, so it
    must report `model` already resolved."""
    body = client.get("/admin/routing", headers=AUTH).json()
    grouped = next(m for m in body["logical_models"] if m["name"] == "grouped")
    by_provider = {t["provider"]: t for t in grouped["targets"]}
    # alpha inherits its id from model_map — the routing view resolves it.
    assert by_provider["alpha"]["model"] == "vendor/Alpha-Grouped"
    assert by_provider["beta"]["model"] == "other/pinned-beta"
    for t in grouped["targets"]:
        assert {"provider", "model", "priority", "is_down", "known_provider"} <= set(t)


def test_config_targets_keep_an_inherited_model_null(client):
    """The editor must see `model: null` for an inherited target, or saving the
    form unchanged writes an explicit pin and changes what the config means."""
    body = client.get("/admin/config", headers=AUTH).json()
    grouped = next(m for m in body["logical_models"] if m["name"] == "grouped")
    by_provider = {t["provider"]: t for t in grouped["targets"]}

    assert by_provider["alpha"]["model"] is None, "an inherited target must stay null"
    assert by_provider["alpha"]["resolved_model"] == "vendor/Alpha-Grouped"
    # A pinned target reports the pin in both places.
    assert by_provider["beta"]["model"] == "other/pinned-beta"
    assert by_provider["beta"]["resolved_model"] == "other/pinned-beta"


def test_config_reports_which_ids_a_group_fronts(client):
    body = client.get("/admin/config", headers=AUTH).json()
    alpha = next(p for p in body["providers"] if p["name"] == "alpha")
    assert alpha["fronted"] == {"vendor/Alpha-Grouped": "grouped"}


def test_config_routing_block_fields(client):
    body = client.get("/admin/config", headers=AUTH).json()
    assert {"queue_timeout", "failover", "auto_group", "down_backoff",
            "queue_affinity", "affinity_max_skips"} <= set(body["routing"])


def test_a_target_on_a_removed_provider_stays_visible_and_flagged(client, load_config):
    """`known_provider: false` is what keeps a half-finished config edit fixable
    in the console instead of just disappearing."""
    from app import config as conf
    conf.LOGICAL_MODELS["orphan"] = conf.LogicalModel(
        name="orphan", targets=[conf.Target(provider="deleted", priority=1)]
    )
    body = client.get("/admin/routing", headers=AUTH).json()
    orphan = next(m for m in body["logical_models"] if m["name"] == "orphan")
    assert orphan["targets"][0]["known_provider"] is False
