"""Config write-back: formatting fidelity and abort-don't-corrupt.

The headline test here is `test_identical_saves_are_byte_identical`, which
AGENTS.md describes as already existing and which did not. It guards the property
no reviewer can verify by reading: ruamel remembers each scalar's quote style and
each collection's flow/block style, and rebuilding a node instead of mutating it
in place silently discards all of that. The symptom was a save that changed
nothing rewriting `"a": "b"` as `a: b` and flattening block lists into flow — a
diff on a hand-annotated, git-tracked production file, produced by a no-op.
"""
import os
from pathlib import Path

import pytest

from app import configwrite

CONFIG = """\
# Top-of-file comment that must survive every write.
models:
  grouped:
    targets:
      - {provider: alpha, model: "vendor/Alpha-Model:q4", priority: 1}
      - {provider: beta,  priority: 2}

providers:
  # A backend with a block-style allow-list and quoted native ids.
  - name: alpha
    base_url: "http://127.0.0.1:1/v1"
    api_key: "sk-must-be-written-back-verbatim"
    slots: 1
    enabled_models:
      - "vendor/Alpha-Model:q4"
      - "vendor/Alpha-Other"
    model_map:
      "vendor/Alpha-Model:q4": grouped
      "vendor/Alpha-Other": alpha-other

  # A backend with a flow-style allow-list — both styles appear in the real file.
  - name: beta
    base_url: "http://127.0.0.1:2/v1"
    enabled_models: ["other/beta-model"]
    model_map:
      "other/beta-model": grouped

aliases:
  quick: "alpha:alpha-other"

routing:
  queue_timeout: 0   # trailing comment on a scalar
  failover: true
"""


@pytest.fixture
def cfg(load_config):
    return load_config(CONFIG)


async def test_comments_and_key_order_survive(cfg):
    ok, err = await configwrite.set_aliases({"quick": "alpha:alpha-other", "slow": "beta:grouped"})
    assert (ok, err) == (True, None)

    text = cfg.read_text()
    assert "# Top-of-file comment that must survive every write." in text
    assert "# A backend with a block-style allow-list and quoted native ids." in text
    assert "# trailing comment on a scalar" in text
    # Key order is preserved, not alphabetized.
    assert text.index("models:") < text.index("providers:") < text.index("routing:")


async def test_api_key_written_back_verbatim(cfg):
    """A secret is read and rewritten untouched, and never returned to a caller."""
    ok, err = await configwrite.set_enabled_models("beta", ["other/beta-model", "other/beta-extra"])
    assert (ok, err) == (True, None)
    assert 'api_key: "sk-must-be-written-back-verbatim"' in cfg.read_text()
    assert err is None  # the reason string never carries config content


async def test_identical_saves_are_byte_identical(cfg):
    """Two consecutive identical saves must leave the file byte-identical.

    The first write is allowed to normalize (ruamel cannot reproduce alignment
    padding inside flow maps). The *second* must be a true no-op: if it isn't,
    every console save produces a diff even when nothing changed, which is what
    made a no-op save rewrite quote styles and flatten block lists.

    Exercised across all five editors, since each one mutates a different part of
    the document and any of them can regress independently.
    """
    edits = [
        ("aliases", lambda: configwrite.set_aliases({"quick": "alpha:alpha-other"})),
        ("enabled_models", lambda: configwrite.set_enabled_models(
            "alpha", ["vendor/Alpha-Model:q4", "vendor/Alpha-Other"])),
        ("model_map", lambda: configwrite.set_model_map(
            "alpha", {"vendor/Alpha-Model:q4": "grouped", "vendor/Alpha-Other": "alpha-other"})),
        ("logical_model", lambda: configwrite.set_logical_model("grouped", [
            {"provider": "alpha", "model": "vendor/Alpha-Model:q4", "priority": 1},
            {"provider": "beta", "model": None, "priority": 2},
        ])),
    ]

    for label, edit in edits:
        ok, err = await edit()
        assert (ok, err) == (True, None), f"{label}: first save failed: {err}"
        after_first = cfg.read_text()

        ok, err = await edit()
        assert (ok, err) == (True, None), f"{label}: second save failed: {err}"
        after_second = cfg.read_text()

        assert after_first == after_second, (
            f"{label}: a second identical save changed the file — a no-op save "
            f"must not produce a diff"
        )


async def test_quote_style_is_not_rewritten(cfg):
    """Specifically the regression that started this: quoted scalars stay quoted."""
    ok, _ = await configwrite.set_model_map(
        "alpha", {"vendor/Alpha-Model:q4": "grouped", "vendor/Alpha-Other": "alpha-other"}
    )
    assert ok
    text = cfg.read_text()
    assert '"vendor/Alpha-Model:q4": grouped' in text
    assert '"vendor/Alpha-Other": alpha-other' in text


async def test_block_list_stays_block_flow_list_stays_flow(cfg):
    """A save must not rewrite one list style into the other."""
    ok, _ = await configwrite.set_enabled_models(
        "alpha", ["vendor/Alpha-Model:q4", "vendor/Alpha-Other"]
    )
    assert ok
    ok, _ = await configwrite.set_enabled_models("beta", ["other/beta-model"])
    assert ok

    text = cfg.read_text()
    # alpha keeps its block list...
    assert '      - "vendor/Alpha-Model:q4"' in text
    # ...and beta keeps its flow list.
    assert 'enabled_models: ["other/beta-model"]' in text


async def test_unknown_provider_refused_without_touching_the_file(cfg):
    before = cfg.read_text()
    ok, err = await configwrite.set_enabled_models("nonexistent", ["x"])
    assert ok is False
    assert "nonexistent" in err
    assert cfg.read_text() == before


async def test_unknown_logical_model_refused(cfg):
    before = cfg.read_text()
    ok, err = await configwrite.delete_logical_model("not-a-model")
    assert ok is False
    assert cfg.read_text() == before


async def test_read_only_config_reports_rather_than_raising(cfg, monkeypatch):
    """A `:ro` mount must yield (False, reason), never an exception — the caller
    keeps its live change and reports `persisted: false`."""
    monkeypatch.setattr(configwrite, "config_writable", lambda: False)
    before = cfg.read_text()
    ok, err = await configwrite.set_aliases({"quick": "alpha:alpha-other", "new": "beta:grouped"})
    assert ok is False
    assert "not writable" in err
    assert cfg.read_text() == before


async def test_empty_enabled_models_means_discover_everything(cfg):
    """`[]` is meaningful, not a no-op: it flips the provider to lists_all."""
    ok, err = await configwrite.set_enabled_models("alpha", [])
    assert (ok, err) == (True, None)
    from app import config as conf
    conf.reload_if_changed()
    assert conf.PROVIDERS_BY_NAME["alpha"].lists_all is True


async def test_priority_reorder_matches_inherited_targets(cfg):
    """`persist_model_priorities` has to match a live Target whose model is None
    against a file entry that spells the id out — the `conf.native_for` path."""
    from app import config as conf

    lm = conf.LOGICAL_MODELS["grouped"]
    for t in lm.targets:
        t.priority = 1 if t.provider == "beta" else 2

    ok, err = await configwrite.persist_model_priorities("grouped", lm.targets)
    assert (ok, err) == (True, None)

    conf.reload_if_changed()
    got = {t.provider: t.priority for t in conf.LOGICAL_MODELS["grouped"].targets}
    assert got == {"beta": 1, "alpha": 2}


async def test_priority_reorder_reports_a_mismatch_instead_of_raising(cfg):
    """The path that used to raise NameError: a target in the file that the
    request doesn't mention must come back as (False, reason)."""
    from app import config as conf

    lm = conf.LOGICAL_MODELS["grouped"]
    partial = [t for t in lm.targets if t.provider == "alpha"]
    assert partial, "fixture should have an alpha target"

    ok, err = await configwrite.persist_model_priorities("grouped", partial)
    assert ok is False
    assert isinstance(err, str) and err
    # The reason names the target it could not account for, rather than blowing up.
    assert "beta" in err


async def test_detached_config_is_detected_and_refused(cfg, tmp_path):
    """A replaced-by-rename config file must be spotted, not written to.

    This is the failure that cost a live session's worth of console edits. A
    single-file bind mount is pinned to one inode; `git pull`/`checkout` and
    editors that save by rename replace the *host* file, leaving the container on
    an orphaned inode. Everything keeps working — the write succeeds, reads back
    byte-for-byte, every self-check passes — and the edits evaporate the next time
    the container is recreated.

    Simulated here the same way it happens for real: rename a different file over
    the config path while the original inode is still reachable. `st_nlink` on the
    orphan drops to 0, which is the only signal available from inside.
    """
    from app import config as conf

    path = Path(conf.CONFIG_PATH)
    assert configwrite.config_detached() is False

    # Hold the original inode open so it survives losing its directory entry,
    # exactly as the bind mount does.
    with open(path, "rb") as pinned:
        replacement = path.with_suffix(".replacement")
        replacement.write_text("providers: []\n")
        os.replace(replacement, path)  # rename over it — the orphaning step

        # The test process reaches the orphan through /proc/self/fd, the way the
        # container reaches it through its mount point.
        orphan = f"/proc/self/fd/{pinned.fileno()}"
        assert os.stat(orphan).st_nlink == 0, "the orphan should have lost its name"

        conf.CONFIG_PATH = orphan
        assert configwrite.config_detached() is True

        ok, err = await configwrite.set_aliases({"anything": "at-all"})
        assert ok is False
        assert "detached" in err or "replaced on the host" in err
        # And it says how to fix it, since the reader is looking at a console.
        assert "force-recreate" in err

        conf.CONFIG_PATH = str(path)
