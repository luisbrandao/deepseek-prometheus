"""Shared fixtures.

Two things need care here, both consequences of how the app is built:

* `app.config` reads `CONFIG_PATH` at **import** time, so `CONFIG_PATH` has to be
  set before anything imports `app.*`. That happens at the top of this module,
  which pytest loads before collecting any test.
* Almost all of the interesting state is module-global and process-local by
  design (see the invariants in AGENTS.md). Tests therefore have to reset it
  between cases or they leak into each other — `reset_state` does that, and it is
  autouse so no test can forget.
"""
import os
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Must precede the first `import app.*` anywhere in the suite.
FIXTURE_CONFIG = ROOT / "tests" / "fixtures" / "config.yaml"
os.environ.setdefault("CONFIG_PATH", str(FIXTURE_CONFIG))

from app import config as conf  # noqa: E402
from app import inflight, registry, slots  # noqa: E402


@pytest.fixture(autouse=True)
def reset_state():
    """Clear the process-local accounting between tests.

    Mirrors exactly what the modules own: slot occupancy and the affinity
    memory, the discovery caches and down-marks, and the in-flight feed. Anything
    added to those modules should be reset here too.
    """
    yield
    slots._in_use.clear()
    slots._running.clear()
    slots._last_model.clear()
    slots._rr.clear()
    del slots._waiters[:]
    registry._cache.clear()
    registry._last_good.clear()
    registry._down_until.clear()
    registry._locks.clear()
    inflight._active.clear()
    inflight._recent.clear()
    inflight._bodies.clear()


@pytest.fixture
def load_config(tmp_path, monkeypatch):
    """Point the live config at a temp file holding `text`, and load it.

    Returns the path, so a test can inspect or re-read the file afterwards —
    which is what the configwrite tests need. Uses `reload_if_changed`, the same
    entry point the watcher uses, so the test exercises the real swap rather than
    poking globals.
    """
    def _load(text: str) -> Path:
        path = tmp_path / "config.yaml"
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        monkeypatch.setattr(conf, "CONFIG_PATH", str(path))
        # Force a reparse regardless of what the previous signature was.
        conf._last_sig = None
        conf._loaded_digest = None
        assert conf.reload_if_changed() is True
        return path

    return _load


@pytest.fixture
def providers(monkeypatch):
    """Install a set of providers directly, without going through a file.

    The slot tests care only about names, slot budgets and priorities, and
    building `Provider` objects in-process keeps them free of YAML noise.
    """
    def _install(*specs, **routing_overrides):
        provs = [conf.Provider(**spec) for spec in specs]
        monkeypatch.setattr(conf, "PROVIDERS", provs)
        monkeypatch.setattr(conf, "PROVIDERS_BY_NAME", {p.name: p for p in provs})
        if routing_overrides:
            monkeypatch.setattr(conf, "ROUTING", conf.Routing(**routing_overrides))
        return provs

    return _install
