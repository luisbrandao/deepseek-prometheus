"""Persist console edits back into the config file, comments and all.

The web console is the intended way to change routing, upstream allow-lists,
logical models and aliases; this module is what makes those edits durable. It
loads `CONFIG_PATH` through ruamel.yaml's round-trip parser, mutates the parsed
document in place, and writes it back — so comments, key order, quoting and
blank lines survive, which matters because the config is a hand-annotated,
git-tracked file on the deploy host.

## Formatting fidelity

Measured against this project's production config (189 lines, 31 comments): a
round-trip preserves every comment and produces a semantically identical
document, changing 48 lines — all of it hand-alignment *padding inside flow
maps* (`{provider: nanoGPT,       priority: 2}` loses its column alignment).
ruamel does not track intra-flow spacing, so that normalization is irreducible
and happens once, on the first console write. `_YAML` is configured to keep
everything ruamel *can* keep: `sequence=4, offset=2` reproduces this project's
two-space-then-dash list indentation (the default collapses it and inflates the
diff five-fold), `preserve_quotes` keeps `"..."` as written, and a very wide
`width` stops long lines being re-wrapped.

## Safety model: abort-don't-corrupt

Every mutation validates before it writes, and any surprise aborts with a reason
while leaving the file untouched:

* the target of the edit must exist (unknown provider, unknown model → refuse),
* the edited document is re-serialized to text, parsed back with **plain PyYAML**
  (the parser the app itself uses), and the parsed result must contain exactly
  the change that was requested,
* the provider set must survive the edit — no mutation here may add or remove a
  backend, which is the blast radius that would take routing down,
* the file must still be writable (a `:ro` mount reports `persisted: false` and
  the caller keeps its live change).

Callers get `(ok, reason)` and must treat a failure as a warning, never a 500.

The write is IN-PLACE (`r+` + truncate), never write-temp-then-rename:
`/app/config.yaml` is a single-file bind mount, so the mount is pinned to the
host file's inode — a rename would swap the directory entry while the container
keeps the old inode (and renaming over a mount point fails outright). The
content is fully validated in memory before the file is opened for writing.

Secrets are never touched: `api_key` values are read and written back verbatim as
the document holds them, and no function here returns one to a caller.
"""
import asyncio
import io
import logging
import os

import yaml as pyyaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from app import config as conf

logger = logging.getLogger("llm-proxy")


def _yaml() -> YAML:
    """A round-trip parser tuned to this project's formatting (see module doc)."""
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


# Serializes the read-modify-write cycle. Created lazily so it binds to the
# running loop, matching the convention in slots/registry.
_lock = None


def _lock_for() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def config_writable() -> bool:
    """Whether the config file accepts writes (False on a `:ro` bind mount)."""
    return os.access(conf.CONFIG_PATH, os.W_OK)


def _read_text() -> str:
    with open(conf.CONFIG_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _write_text(text: str) -> None:
    """In-place overwrite. See the module docstring for why not rename()."""
    with open(conf.CONFIG_PATH, "r+", encoding="utf-8") as f:
        f.write(text)
        f.truncate()


def _serialize(doc) -> str:
    buf = io.StringIO()
    _yaml().dump(doc, buf)
    return buf.getvalue()


def _providers_of(doc):
    return doc.get("providers") or []


def _find_provider(doc, name: str):
    for entry in _providers_of(doc):
        if entry.get("name") == name:
            return entry
    return None


def _check_provider_field(parsed, provider_name: str, field: str, verify):
    """Self-check helper for a per-provider edit.

    Finds `provider_name` in the re-parsed document and hands its `field` value to
    `verify`, which returns None when the written value is what was asked for or a
    reason string when it isn't. A provider that is no longer there at all is
    always a failure — `_commit` guards the provider *list*, but this catches a
    rename or a mangled entry.
    """
    for entry in parsed.get("providers") or []:
        if entry.get("name") == provider_name:
            return verify(entry.get(field))
    return f"self-check failed: provider '{provider_name}' vanished"


def _commit(doc, before_text: str, check):
    """Serialize, validate, write. Returns `(ok, reason)`.

    `check(parsed)` inspects the re-parsed document and returns None when the
    requested change is present and correct, or a reason string when it is not.
    Nothing is written unless that passes — a mutation that didn't take effect the
    way we intended must not reach the file.
    """
    try:
        text = _serialize(doc)
    except Exception as e:  # noqa: BLE001 - never raise into a request
        return False, f"could not serialize config: {type(e).__name__}: {e}"

    try:
        parsed = pyyaml.safe_load(text)
    except Exception as e:  # noqa: BLE001
        return False, f"rewritten config does not parse: {type(e).__name__}: {e}"
    if not isinstance(parsed, dict):
        return False, "rewritten config is not a mapping"

    # A mutation here must never change which backends exist.
    before = pyyaml.safe_load(before_text) or {}
    names_before = [p.get("name") for p in (before.get("providers") or [])]
    names_after = [p.get("name") for p in (parsed.get("providers") or [])]
    if names_before != names_after:
        return False, "refusing to write: the provider list changed"

    problem = check(parsed)
    if problem:
        return False, problem

    if not config_writable():
        return False, "config file is not writable (read-only mount?)"
    try:
        _write_text(text)
    except OSError as e:
        return False, f"could not write config: {e}"
    return True, None


async def _edit(mutate, check):
    """Load → mutate → validate → write, serialized against concurrent edits."""
    async with _lock_for():
        try:
            before = _read_text()
            doc = _yaml().load(before)
        except Exception as e:  # noqa: BLE001
            return False, f"could not read config: {type(e).__name__}: {e}"
        if not isinstance(doc, dict):
            return False, "config is not a mapping"
        problem = mutate(doc)
        if problem:
            return False, problem
        return _commit(doc, before, check)


# --- individual edits --------------------------------------------------------


async def persist_model_priorities(model: str, targets) -> tuple:
    """Write a logical model's target priorities (the Routing tab's reorder).

    Reorder only: the (provider, native model) set must match the file exactly,
    so this can never invent or drop a target.
    """
    wanted = {
        (t.provider, conf.native_for(model, t.provider, t.model)): t.priority for t in targets
    }

    def mutate(doc):
        models = doc.get("models")
        if not isinstance(models, dict) or model not in models:
            return f"no '{model}' entry under models: in the config"
        entries = (models[model] or {}).get("targets")
        if not entries:
            return f"'{model}' has no targets in the config"
        seen = {}
        for entry in entries:
            provider = entry.get("provider")
            native = conf.native_for(model, provider, entry.get("model"))
            key = (provider, native)
            if key not in wanted:
                return f"target {provider}/{native} is in the config but not in the request"
            entry["priority"] = wanted[key]
            seen[key] = True
        missing = set(wanted) - set(seen)
        if missing:
            return f"requested target(s) not found in the config: {sorted(missing)}"
        return None

    def check(parsed):
        entries = ((parsed.get("models") or {}).get(model) or {}).get("targets") or []
        got = {}
        for entry in entries:
            provider = entry.get("provider")
            got[(provider, conf.native_for(model, provider, entry.get("model")))] = entry.get("priority")
        if got != wanted:
            return "self-check failed: priorities in the rewritten file don't match the request"
        return None

    return await _edit(mutate, check)


async def set_enabled_models(provider_name: str, models) -> tuple:
    """Set a provider's `enabled_models` allow-list.

    `models` is a list of native ids, or None/[] meaning "expose everything this
    backend live-reports" (`Provider.lists_all`). Those two states are the whole
    point of the upstream-models editor, so both must be writable.
    """
    wanted = [str(m) for m in (models or [])]

    def mutate(doc):
        entry = _find_provider(doc, provider_name)
        if entry is None:
            return f"unknown provider '{provider_name}'"
        existing = entry.get("enabled_models")
        if isinstance(existing, CommentedSeq):
            # Mutate in place so the list keeps the style it was written in — this
            # config has both flow (`[a, b]`) and block (`- a`) allow-lists, and
            # rebuilding the sequence rewrote one into the other on every save.
            # Surviving items stay at their original index with their original
            # quoting; only genuine additions and removals move.
            keep = set(wanted)
            for i in range(len(existing) - 1, -1, -1):
                if str(existing[i]) not in keep:
                    del existing[i]
            present = {str(v) for v in existing}
            for native in wanted:
                if native not in present:
                    existing.append(native)
            return None
        seq = CommentedSeq(wanted)
        seq.fa.set_flow_style()  # a new list: match how they are written here
        entry["enabled_models"] = seq
        return None

    def check(parsed):
        def verify(got):
            if list(got or []) != wanted:
                return "self-check failed: enabled_models in the rewritten file don't match"
            return None
        return _check_provider_field(parsed, provider_name, "enabled_models", verify)

    return await _edit(mutate, check)


async def set_model_map(provider_name: str, mapping: dict) -> tuple:
    """Set a provider's `model_map`: native id -> the name clients use.

    Keyed by the **native** id, because that is the direction the wire needs and
    the direction `Provider.__post_init__` inverts. Native ids containing a colon
    (`qwen3.8-max:thinking`) are written quoted: plain YAML accepts them since the
    colon isn't followed by a space, but quoting is what the rest of this file does
    with native ids and it cannot be broken by a later edit that adds a space.
    """
    wanted = {str(k): str(v) for k, v in (mapping or {}).items()}

    def mutate(doc):
        entry = _find_provider(doc, provider_name)
        if entry is None:
            return f"unknown provider '{provider_name}'"
        if not wanted:
            if "model_map" in entry:
                del entry["model_map"]
            return None
        existing = entry.get("model_map")
        target = existing if isinstance(existing, CommentedMap) else CommentedMap()
        # Mutate in place and leave untouched entries strictly alone: ruamel keeps
        # each scalar's original quote style, so re-saving an unchanged mapping
        # must not rewrite `"a": "b"` as `a: b`. Rebuilding the map lost that and
        # made a no-op save produce a diff.
        for key in [k for k in target if str(k) not in wanted]:
            del target[key]
        by_str = {str(k): k for k in target}
        for native, canonical in wanted.items():
            key = by_str.get(native)
            if key is not None:
                if str(target[key]) != canonical:
                    target[key] = canonical
                continue
            target[DoubleQuotedScalarString(native) if ":" in native else native] = canonical
        entry["model_map"] = target
        return None

    def check(parsed):
        def verify(got):
            if {str(k): str(v) for k, v in (got or {}).items()} != wanted:
                return "self-check failed: model_map in the rewritten file doesn't match"
            return None
        return _check_provider_field(parsed, provider_name, "model_map", verify)

    return await _edit(mutate, check)


async def set_aliases(aliases: dict) -> tuple:
    """Replace the whole `aliases:` map. The console owns it as a unit — it is a
    flat name→target dictionary, so per-key surgery would buy nothing."""
    wanted = {str(k): str(v) for k, v in (aliases or {}).items()}

    def mutate(doc):
        if wanted:
            existing = doc.get("aliases")
            target = existing if isinstance(existing, CommentedMap) else CommentedMap()
            for key in [k for k in target if k not in wanted]:
                del target[key]
            for key, value in wanted.items():
                target[key] = value
            doc["aliases"] = target
        elif "aliases" in doc:
            # An empty map reads as "no aliases"; drop the key rather than leaving
            # `aliases: {}` behind.
            del doc["aliases"]
        return None

    def check(parsed):
        got = parsed.get("aliases") or {}
        if {str(k): str(v) for k, v in got.items()} != wanted:
            return "self-check failed: aliases in the rewritten file don't match"
        return None

    return await _edit(mutate, check)


async def set_logical_model(name: str, targets) -> tuple:
    """Create or replace one `models:` entry from `[{provider, model?, priority}]`.

    Existing entries are mutated in place so their comments survive; a new entry
    is appended in flow style, matching how they are written by hand here.
    """
    wanted = []
    for t in targets:
        item = {"provider": str(t["provider"]), "priority": int(t["priority"])}
        native = t.get("model")
        if native:
            item["model"] = str(native)
        wanted.append(item)

    def mutate(doc):
        models = doc.get("models")
        if not isinstance(models, (dict, CommentedMap)):
            models = CommentedMap()
            doc["models"] = models
        seq = CommentedSeq()
        for item in wanted:
            row = CommentedMap()
            row["provider"] = item["provider"]
            if "model" in item:
                # Quoted, matching how native ids are written by hand throughout
                # this config — an id like `local.qwen-medium:low` contains a colon
                # and reads much less ambiguously with quotes.
                row["model"] = DoubleQuotedScalarString(item["model"])
            row["priority"] = item["priority"]
            row.fa.set_flow_style()
            seq.append(row)
        existing = models.get(name)
        if isinstance(existing, (dict, CommentedMap)):
            existing["targets"] = seq
        else:
            entry = CommentedMap()
            entry["targets"] = seq
            models[name] = entry
        return None

    def check(parsed):
        entry = (parsed.get("models") or {}).get(name)
        if not isinstance(entry, dict):
            return f"self-check failed: '{name}' missing from the rewritten file"
        got = []
        for row in entry.get("targets") or []:
            item = {"provider": row.get("provider"), "priority": row.get("priority")}
            if row.get("model"):
                item["model"] = row["model"]
            got.append(item)
        if got != wanted:
            return "self-check failed: targets in the rewritten file don't match"
        return None

    return await _edit(mutate, check)


async def delete_logical_model(name: str) -> tuple:
    """Remove one `models:` entry. Its clients fall back to whatever the plain
    resolution order finds — usually auto-group across the same backends."""

    def mutate(doc):
        models = doc.get("models")
        if not isinstance(models, (dict, CommentedMap)) or name not in models:
            return f"no '{name}' entry under models: in the config"
        del models[name]
        if not models:
            del doc["models"]
        return None

    def check(parsed):
        if name in (parsed.get("models") or {}):
            return f"self-check failed: '{name}' is still in the rewritten file"
        return None

    return await _edit(mutate, check)
