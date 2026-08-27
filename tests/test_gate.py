"""The auth gate and the catalog, driven through the real ASGI app.

These go through `TestClient` rather than calling functions, because the thing
worth testing is the *composition*: the catch-all route, the gate, resolution and
the error mapping all have to agree. The production bug this guards was exactly a
disagreement between them — a caller got a 401 naming a backend they had never
asked for, because an unresolved model fell through to `PROVIDERS[0]`.

No sockets are opened: every model here is allow-listed, so discovery never
probes, and the tests that would reach a backend assert on the response the proxy
produces *before* forwarding.
"""
import pytest
from fastapi.testclient import TestClient

from app import config as conf
from app import registry

CONFIG = """\
models:
  grouped:
    targets:
      - {provider: open, priority: 1}
      - {provider: paid, priority: 2}

providers:
  # Deliberately FIRST in the list and permission-gated: this is the arrangement
  # that produced the original 401-about-the-wrong-backend bug.
  - name: paid
    base_url: "http://127.0.0.1:1/v1"
    require_permission: true
    enabled_models: ["vendor/Paid-Grouped", "vendor/paid-only"]
    model_map:
      "vendor/Paid-Grouped": grouped
      "vendor/paid-only": paid-only

  - name: open
    base_url: "http://127.0.0.1:2/v1"
    enabled_models: ["local/open-grouped", "local/open-only"]
    model_map:
      "local/open-grouped": grouped
      "local/open-only": open-only

auth:
  keys: ["test-key"]
"""

AUTH = {"Authorization": "Bearer test-key"}


@pytest.fixture
def client(load_config, monkeypatch):
    async def fake_cached_live(provider):
        return list(provider.enabled_models)

    monkeypatch.setattr(registry, "_cached_live", fake_cached_live)
    load_config(CONFIG)
    from app import main
    with TestClient(main.app) as c:
        yield c


def ids(payload):
    return {m["id"] for m in payload["data"]}


# ── Catalog visibility ──────────────────────────────────────────────────────

def test_unauthenticated_catalog_hides_restricted_models(client):
    body = client.get("/v1/models").json()
    listed = ids(body)
    assert "open-only" in listed
    assert "paid-only" not in listed, "a require_permission model must not be listed"


def test_authenticated_catalog_shows_everything(client):
    body = client.get("/v1/models", headers=AUTH).json()
    listed = ids(body)
    assert {"open-only", "paid-only", "grouped"} <= listed


def test_logical_model_is_listed_when_any_target_is_visible(client):
    assert "grouped" in ids(client.get("/v1/models").json())


def test_logical_model_hides_its_own_targets(client):
    """Clients use the stable logical name; the underlying native ids stay hidden
    so they don't flap as backends come and go."""
    listed = ids(client.get("/v1/models", headers=AUTH).json())
    assert "local/open-grouped" not in listed
    assert "vendor/Paid-Grouped" not in listed


def test_models_and_v1_models_agree(client):
    assert client.get("/models").json() == client.get("/v1/models").json()


# ── The gate ────────────────────────────────────────────────────────────────

def test_unknown_model_is_404_not_a_guessed_backend(client):
    """The core regression. `paid` is first in the config and permission-gated,
    so the old fallback produced a 401 about `paid`. It must be a clean 404."""
    r = client.post("/v1/chat/completions", json={"model": "nobody-serves-this"})
    assert r.status_code == 404, r.text
    body = r.json()
    assert body["error"]["code"] == "model_not_found"
    assert "nobody-serves-this" in body["error"]["message"]
    # And crucially: it does not mention a backend the caller never asked for.
    assert "paid" not in r.text


def test_restricted_model_without_a_key_is_401(client):
    r = client.post("/v1/chat/completions", json={"model": "paid-only"})
    assert r.status_code == 401, r.text
    assert r.json()["error"]["type"] == "unauthorized"
    assert "paid-only" in r.json()["error"]["message"]


def test_wrong_key_is_treated_as_unauthenticated(client):
    r = client.post(
        "/v1/chat/completions",
        json={"model": "paid-only"},
        headers={"Authorization": "Bearer not-the-key"},
    )
    assert r.status_code == 401


def test_admin_endpoints_are_gated(client):
    for path in ("/admin/logs", "/admin/config", "/admin/routing", "/admin/inflight"):
        assert client.get(path).status_code == 403, f"{path} is not gated"
        assert client.get(path, headers=AUTH).status_code == 200, f"{path} rejects a valid key"


def test_gate_rejection_body_shape_is_stable(client):
    """The gate moved into a router dependency; the body clients see must not.
    A plain HTTPException would render {"detail": ...} instead."""
    r = client.get("/admin/config")
    assert r.status_code == 403
    assert r.json() == {"error": "unauthorized"}


def test_admin_write_endpoints_are_gated_too(client):
    """The gate is on the router, so it covers writes without each one opting in."""
    assert client.put("/admin/config/aliases", json={"aliases": {}}).status_code == 403
    assert client.delete("/admin/config/models/grouped").status_code == 403
    assert client.post("/admin/inflight/1/cancel").status_code == 403


def test_admin_config_never_returns_an_api_key(client, load_config, monkeypatch):
    """`api_key` must not leave the process, including through the config editor."""
    conf.PROVIDERS_BY_NAME["paid"].api_key = "sk-super-secret-value"
    body = client.get("/admin/config", headers=AUTH)
    assert body.status_code == 200
    assert "sk-super-secret-value" not in body.text
    paid = next(p for p in body.json()["providers"] if p["name"] == "paid")
    assert paid["has_api_key"] is True
    assert "api_key" not in paid


def test_admin_routing_never_returns_an_api_key(client):
    conf.PROVIDERS_BY_NAME["paid"].api_key = "sk-another-secret"
    body = client.get("/admin/routing", headers=AUTH)
    assert body.status_code == 200
    assert "sk-another-secret" not in body.text


# ── Browser noise that must not become a model request ──────────────────────

def test_favicon_does_not_fall_into_the_proxy(client):
    """Without this route the browser's automatic request lands in the catch-all,
    is treated as a model-less passthrough, 401s, and pollutes the request feed on
    every page load."""
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/vnd.microsoft.icon"


def test_robots_is_served_not_proxied(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert "Disallow: /" in r.text


@pytest.mark.parametrize("path", ["/ui", "/admin", "/admin/"])
def test_console_paths_redirect_rather_than_401(client, path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code in (307, 308)
    assert r.headers["location"] == "/ui/"


def test_health_needs_no_key(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_metrics_uses_the_llm_proxy_prefix(client):
    text = client.get("/metrics").text
    assert "llm_proxy_requests_total" in text
    assert "deepseek_proxy_" not in text
