"""Build identity: the module, the endpoint, the metric.

The point of all of this is one question a deploy could not previously answer:
*is the process actually running the build the compose file pins?* A stale
container serving `master-50` behind a compose file pinning `master-52` looked
identical from the outside.

`app/version.py` reads its environment at import, so these tests reload the
module under a patched environment rather than trying to mutate constants.
"""
import contextlib
import importlib
import os

import pytest
from fastapi.testclient import TestClient

from app import registry, version

BUILD_VARS = ("APP_VERSION", "APP_REVISION")


@contextlib.contextmanager
def build(**env):
    """Re-import version.py under a given environment, for the duration of the block.

    A context manager rather than a plain function because the module has to stay
    patched *while the assertions run*: `version.py` reads its environment once at
    import, so restoring the environment means reloading again, and doing that
    before the caller looks would hand back the restored values. (It did, on the
    first attempt — three tests saw "dev".)
    """
    saved = {k: os.environ.get(k) for k in BUILD_VARS}
    try:
        for k in BUILD_VARS:
            os.environ.pop(k, None)
        os.environ.update({k: v for k, v in env.items() if v is not None})
        yield importlib.reload(version)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(version)


# ── The module ──────────────────────────────────────────────────────────────

def test_outside_an_image_the_version_is_dev():
    with build() as v:
        assert v.VERSION == "dev"
        assert v.REVISION is None
        assert v.IS_RELEASE is False
        assert v.summary() == "dev"


def test_a_ci_build_reports_its_tag_and_commit():
    with build(APP_VERSION="master-52", APP_REVISION="1e5837a3deadbeef") as v:
        assert v.VERSION == "master-52"
        assert v.REVISION == "1e5837a3deadbeef"
        assert v.IS_RELEASE is True
        assert v.short_revision() == "1e5837a3"
        assert v.summary() == "master-52 (1e5837a3)"


def test_blank_build_args_are_treated_as_absent():
    """`docker build` with an undefined ARG sets the variable to "", not absent.
    Without this rule every local build would report a version of ""."""
    with build(APP_VERSION="", APP_REVISION="") as v:
        assert v.VERSION == "dev"
        assert v.REVISION is None
        assert v.IS_RELEASE is False


def test_whitespace_is_stripped():
    with build(APP_VERSION="  master-7  ", APP_REVISION="  abc123  ") as v:
        assert v.VERSION == "master-7"
        assert v.REVISION == "abc123"


def test_a_version_without_a_revision_still_summarizes():
    with build(APP_VERSION="master-52") as v:
        assert v.summary() == "master-52"
        assert v.short_revision() is None
        assert v.as_dict() == {"version": "master-52", "revision": None, "release": True}


def test_reload_leaves_no_residue():
    """The context manager must restore the module, or it leaks into every test
    that runs after it."""
    before = version.VERSION
    with build(APP_VERSION="master-999"):
        pass
    assert version.VERSION == before


# ── The endpoint ────────────────────────────────────────────────────────────

CONFIG = """\
providers:
  - name: only
    base_url: "http://127.0.0.1:1/v1"
    enabled_models: ["plain-model"]
"""


@pytest.fixture
def client(load_config, monkeypatch):
    async def fake_cached_live(provider):
        return list(provider.enabled_models)

    monkeypatch.setattr(registry, "_cached_live", fake_cached_live)
    load_config(CONFIG)
    from app import main
    with TestClient(main.app) as c:
        yield c


def test_health_reports_the_build(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert set(body) == {"status", "version", "revision", "release"}


def test_health_needs_no_key(client):
    """The console reads this before sign-in, and the deploy curls it."""
    assert client.get("/health").status_code == 200


def test_health_version_matches_the_module(client):
    assert client.get("/health").json()["version"] == version.VERSION


# ── The metric ──────────────────────────────────────────────────────────────

def test_build_info_metric_is_exposed(client):
    text = client.get("/metrics").text
    line = next(
        (l for l in text.splitlines() if l.startswith("llm_proxy_build_info{")), None
    )
    assert line is not None, "llm_proxy_build_info is missing from /metrics"
    # Always 1 — the value carries no information, the labels do.
    assert line.rstrip().endswith("1.0")
    assert f'version="{version.VERSION}"' in line


def test_build_info_has_both_labels(client):
    text = client.get("/metrics").text
    line = next(l for l in text.splitlines() if l.startswith("llm_proxy_build_info{"))
    assert "version=" in line and "revision=" in line
