import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app import auth, configwrite, inflight, logbuffer, registry, slots, upstream
from app import config as conf
from app.metrics import metrics_response
from app.proxy import proxy_request
from app.registry import list_models

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"

# One module-level logger, as in every other module here. It was previously
# fetched inline at each call site — including once as getLogger(__name__), which
# put that single line on the `app.main` logger while the rest of the process logs
# under `llm-proxy`, so the console's logger column disagreed with itself.
logger = logging.getLogger("llm-proxy")


class _LocalTimeFormatter(logging.Formatter):
    """Stamp every line with a local-time ISO-8601 timestamp that carries an
    explicit UTC offset, e.g. `2026-06-17T02:48:13-03:00`.

    Two problems this solves: the timestamp is unambiguous no matter what
    timezone the reader (Loki, a teammate) assumes, and "local" follows the
    container's TZ — so set `TZ` (e.g. America/Sao_Paulo) and ship tzdata in
    the image and the wall-clock matches where the box actually is.
    """

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds")


# All logs go to stdout (Python's StreamHandler — and uvicorn — default to
# stderr, which means `docker logs <c> | grep` silently misses everything until
# you add `2>&1`). One stream, greppable by default.
_formatter = _LocalTimeFormatter(LOG_FORMAT)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_formatter)
logging.basicConfig(level=logging.INFO, handlers=[_handler])

# Per-request events are emitted as pure logfmt (`ts=.. level=.. event=request ..`)
# on a dedicated logger with a message-only formatter — no human prefix to trip
# Loki's `| logfmt`. Kept off the root handler via propagate=False so it isn't
# double-stamped. Everything else keeps the readable `<ts> LEVEL <msg>` format.
_event_handler = logging.StreamHandler(sys.stdout)
_event_handler.setFormatter(logging.Formatter("%(message)s"))
_event_logger = logging.getLogger("llm-proxy.event")
_event_logger.setLevel(logging.INFO)
_event_logger.addHandler(_event_handler)
_event_logger.propagate = False


# Mirror every log line into an in-memory ring buffer so the /admin/logs view can
# tail logs without a file or docker-socket access. Attached to the root logger
# (catches `llm-proxy` and anything else that propagates) and, separately, to the
# event logger (propagate=False, so its lines never reach root). uvicorn's own
# loggers are wired up in _unify_logging, after uvicorn installs its handlers.
def _attach_buffer(target: logging.Logger) -> None:
    if logbuffer.handler not in target.handlers:
        target.addHandler(logbuffer.handler)


_attach_buffer(logging.getLogger())
_attach_buffer(_event_logger)


# Access-log paths that are pure noise: the web console tails logs by polling
# /admin/logs every ~1.5s and the in-flight view by polling /admin/inflight every
# ~1s, which would otherwise flood the very view you're watching (and docker
# logs). Matched as a substring of the rendered access line.
_ACCESS_LOG_MUTE = ("/admin/logs", "/admin/inflight")


class _MutePollingFilter(logging.Filter):
    """Drop uvicorn access-log records for the console's own polling endpoints."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - never let a filter crash logging
            return True
        return not any(p in msg for p in _ACCESS_LOG_MUTE)


def _unify_logging() -> None:
    """Align uvicorn's loggers with the rest of the app's format and stream.

    uvicorn installs its own handlers (the `INFO:     ...` style) on the
    uvicorn/uvicorn.access/uvicorn.error loggers with propagate=False, so they
    ignore basicConfig. Re-point them at our formatter — and at stdout — so
    every line matches and lives on one greppable stream.
    """
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        for handler in uvicorn_logger.handlers:
            handler.setFormatter(_formatter)
            # uvicorn's handlers are plain StreamHandlers on stderr; move them to
            # stdout. Guard against FileHandler (a StreamHandler subclass) so we
            # never redirect a file-backed handler.
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                handler.setStream(sys.stdout)
        # uvicorn's loggers have propagate=False, so the buffer on root won't see
        # them — attach it directly so access/error lines show up in /admin/logs.
        _attach_buffer(uvicorn_logger)

    # Mute the console's own log-tail polling at the access logger, so those lines
    # reach neither stdout nor the ring buffer (otherwise the Logging tab floods
    # itself every poll).
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _MutePollingFilter) for f in access.filters):
        access.addFilter(_MutePollingFilter())


async def _apply_config_change() -> bool:
    """Pull a just-written config edit into the live process, now.

    The watcher would find it within CONFIG_RELOAD_INTERVAL, but a console edit
    has to be in effect before the response lands — otherwise the UI re-reads and
    renders the pre-edit state, which reads as "my change didn't save". Same side
    effects as the watcher: drop the discovery cache (allow-lists may have moved)
    and wake slot waiters (capacity may have).
    """
    try:
        if not conf.reload_if_changed():
            return False
    except Exception as e:  # noqa: BLE001 - a write we validated should parse, but
        logger.warning("Config written but reload failed: %s: %s", type(e).__name__, e)
        return False
    registry.clear_cache()
    await slots.poke()
    logger.info(
        "Config reloaded after a console edit: %d providers, %d logical models, %d aliases",
        len(conf.PROVIDERS), len(conf.LOGICAL_MODELS), len(conf.ALIASES),
    )
    return True


async def _config_reload_loop():
    """Hot-apply edits to the config file, no restart needed.

    Polls `conf.reload_if_changed()` every CONFIG_RELOAD_INTERVAL seconds. On a
    change: the module globals are swapped (in-flight requests finish under the
    old Provider objects), the live-discovery cache is dropped so new
    base_urls/allow-lists take effect immediately, and queued slot waiters are
    woken in case the edit created capacity. A bad edit (YAML typo, torn
    read mid-write by an external editor) is logged and skipped — the running
    config stays until the file parses again.

    The console's own routing edits (configwrite) also land here one tick
    later; that reload is a no-op state-wise since memory already matches.
    """
    while True:
        await asyncio.sleep(conf.CONFIG_RELOAD_INTERVAL)
        try:
            if conf.reload_if_changed():
                registry.clear_cache()
                await slots.poke()
                logger.info(
                    "Config reloaded from %s: %d providers, %d logical models, %d aliases",
                    conf.CONFIG_PATH,
                    len(conf.PROVIDERS),
                    len(conf.LOGICAL_MODELS),
                    len(conf.ALIASES),
                )
        except FileNotFoundError:
            # Transient hole while an editor/deploy replaces the file; the
            # next tick sees the settled result. Keep the running config.
            pass
        except Exception as e:  # noqa: BLE001 - a bad edit must never kill the app
            logger.warning(
                "Config reload failed — keeping the running config: %s: %s",
                type(e).__name__,
                e,
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs after uvicorn has configured its logging, so our reformat sticks.
    _unify_logging()
    reload_task = (
        asyncio.create_task(_config_reload_loop())
        if conf.CONFIG_RELOAD_INTERVAL > 0
        else None
    )
    try:
        yield
    finally:
        if reload_task is not None:
            reload_task.cancel()
        # Close the shared upstream connection pools (see app/upstream.py).
        await upstream.aclose()


app = FastAPI(title="LLM Proxy", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    data, status, headers = metrics_response()
    return Response(content=data, status_code=status, headers=headers)


@app.get("/logging")
async def get_logging():
    return {"log_input": conf.LOG_INPUT, "log_output": conf.LOG_OUTPUT}


@app.post("/logging")
async def set_logging(request: Request):
    """Toggle request/response logging at runtime, no restart needed.

    Body: {"log_input": bool, "log_output": bool} — both keys optional.
    Gated by the same bearer keys as restricted backends (inert when unset).
    """
    if not auth.is_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    body = await request.json()
    for key, attr in (("log_input", "LOG_INPUT"), ("log_output", "LOG_OUTPUT")):
        if key in body:
            value = body[key]
            if not isinstance(value, bool):
                return JSONResponse(
                    {"error": f"{key} must be a boolean"}, status_code=422
                )
            setattr(conf, attr, value)
    logger.info(
        "Logging flags updated: LOG_INPUT=%s LOG_OUTPUT=%s",
        conf.LOG_INPUT,
        conf.LOG_OUTPUT,
    )
    return {"log_input": conf.LOG_INPUT, "log_output": conf.LOG_OUTPUT}


@app.get("/models")
@app.get("/v1/models")
async def models(request: Request):
    return await list_models(authorized=auth.is_authorized(request))


# --- Admin API (backend for the /ui web dashboard) -----------------------------
# Everything under /admin is gated by the same bearer keys as restricted backends
# and POST /logging. The gate is required, not cosmetic: the log buffer can hold
# full request/response bodies once LOG_INPUT/LOG_OUTPUT are on. Provider
# serialization deliberately omits api_key — secrets never leave the process.


class _AdminForbidden(HTTPException):
    """403 for the admin gate, with a dedicated handler below.

    A plain HTTPException would render `{"detail": ...}`; this keeps the
    `{"error": "unauthorized"}` body the endpoints returned before the gate moved
    into a dependency, so anything scripting /admin/* sees no change.
    """

    def __init__(self):
        super().__init__(status_code=403, detail="unauthorized")


def require_admin(request: Request) -> None:
    """Dependency enforcing the admin bearer gate. Raises 403 when it fails.

    A dependency rather than a helper each handler remembers to call: the gate is
    load-bearing (the log buffer can hold full prompt and response bodies once
    LOG_INPUT/LOG_OUTPUT are on), and the old two-line prologue failed silently —
    a new endpoint that omitted it was simply unprotected, with nothing to catch
    it. Attached to `admin` below, so every route on that router inherits it and
    the invariant is structural instead of remembered.
    """
    if not auth.is_authorized(request):
        raise _AdminForbidden()


# Every /admin/* route lives on this router, so the gate above applies to all of
# them by construction. Mounted on the app after the routes are declared, and
# still above the catch-all proxy route.
admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


def _levelno(name: str) -> int:
    value = logging.getLevelName(name)
    return value if isinstance(value, int) else logging.INFO


def _provider_view(p, *, live=False, editable=False, fronted=None) -> dict:
    """One provider, serialized for an admin view. **Never** includes `api_key`.

    Three views used to hand-write overlapping dicts of these fields, which meant
    adding a `Provider` field needed edits in up to three places and the field
    sets had already drifted apart for no reason. They nest cleanly, so this is
    one function with two switches:

    * `live` adds the runtime state the in-flight and routing views need
      (occupancy, health, resident model) — read from `slots`/`registry`, not the
      config.
    * `editable` adds the settings the Config tab edits, including `has_api_key`
      in place of the key itself, so the secret stays in-process.
    """
    out = {
        "name": p.name,
        "base_url": p.base_url,
        "slots": p.slots,
        "priority": p.priority,
        "require_permission": p.require_permission,
        "lists_all": p.lists_all,
    }
    if live:
        out.update({
            "in_use": slots.in_use(p.name),
            "is_down": registry.is_down(p.name),
            # What this backend most recently ran, i.e. what it is expected to
            # still have loaded — the basis of the queue's affinity decision.
            "resident": slots.resident_model(p.name),
        })
    if editable:
        out.update({
            "cache_ttl": p.cache_ttl,
            "strip_path_prefix": p.strip_path_prefix,
            "enabled_models": list(p.enabled_models),
            "model_map": dict(p.model_map),
            "fronted": (fronted or {}).get(p.name, {}),
            "has_api_key": bool(p.api_key),
        })
    return out


def _serialize_targets(model_name: str, lm, raw: bool = False) -> list:
    """A logical model's targets, for either the routing view or the editor.

    The two differ in exactly one thing, and it matters: `raw=False` reports
    `model` already resolved, which is the stable identity the routing view
    matches a reorder against. `raw=True` reports `model` **as written** (null
    when inherited) plus a separate `resolved_model`, because an editor that got
    the resolved id back would write it into the file on the next save — silently
    turning every inherited target into an explicit pin and changing what the
    config means without changing what it says.
    """
    out = []
    for t in lm.targets:
        resolved = conf.native_for(model_name, t.provider, t.model)
        row = {
            "provider": t.provider,
            "priority": t.priority,
            "is_down": registry.is_down(t.provider),
            "known_provider": t.provider in conf.PROVIDERS_BY_NAME,
        }
        if raw:
            row["model"] = t.model
            row["resolved_model"] = resolved
        else:
            row["model"] = resolved
        out.append(row)
    return out


@admin.get("/logs")
async def admin_logs(request: Request, since: int = 0, level: str = "DEBUG"):
    """Recent log lines with seq > `since` (the UI's live-tail cursor).

    `last_seq` always reflects the newest buffered line — even one filtered out
    by `level` — so the cursor advances past filtered lines instead of re-pulling
    them on every poll.
    """
    new = logbuffer.handler.entries(since)
    last_seq = new[-1]["seq"] if new else since
    threshold = _levelno((level or "DEBUG").upper())
    if threshold > logging.DEBUG:
        new = [e for e in new if _levelno(e["level"]) >= threshold]
    return {"entries": new, "last_seq": last_seq}


@admin.get("/upstream-models")
async def admin_upstream_models(request: Request, provider: str = ""):
    """Probe each backend's real /v1/models concurrently — the 'bypass' button.

    Shows every id a backend actually serves, regardless of its enabled_models
    allow-list, so each endpoint's full catalog is visible in one place.
    """

    async def probe(p):
        try:
            ids = await registry._fetch_live(p)
            return {"provider": p.name, "ok": True, "ids": sorted(ids)}
        except Exception as e:  # noqa: BLE001 - report per-backend, never 500 the page
            return {"provider": p.name, "ok": False, "error": f"{type(e).__name__}: {e}"}

    # `?provider=name` probes just one backend: the Config tab needs a single
    # catalog when a provider is expanded, and probing all eight (cloud APIs
    # included) to fill one checkbox list would take seconds.
    wanted = [p for p in conf.PROVIDERS if not provider or p.name == provider]
    if provider and not wanted:
        return JSONResponse({"error": f"unknown provider '{provider}'"}, status_code=404)
    results = await asyncio.gather(*(probe(p) for p in wanted))
    return {"providers": results}


@admin.get("/inflight")
async def admin_inflight(request: Request):
    """Everything currently in flight: requests holding a slot and requests
    queued behind one, arrival-ordered, plus each provider's slot occupancy.

    Read-only snapshot of process-local state (see app/inflight.py) — cheap
    enough for the console to poll every second. The provider block is the same
    slot accounting the Routing tab shows, repeated here so the queue and the
    capacity that gates it read side by side.
    """
    snap = inflight.snapshot()
    snap["providers"] = [_provider_view(p, live=True) for p in conf.PROVIDERS]
    snap["queue_timeout"] = conf.ROUTING.queue_timeout
    snap["queue_affinity"] = conf.ROUTING.queue_affinity
    snap["affinity_max_skips"] = conf.ROUTING.affinity_max_skips
    return snap


@admin.post("/inflight/{request_id}/cancel")
async def admin_cancel_inflight(request_id: int, request: Request):
    """Kill one in-flight request — the console's per-row Kill button.

    Cancels the asyncio task serving it (see `inflight.Entry.cancel`), which
    unwinds the normal cleanup: slot released, upstream connection closed, entry
    deregistered. Works on a queued request and on one blocked in an upstream
    read that may never return.

    404 means the id is unknown, which almost always means the request finished on
    its own between the console's last poll and the click — not an error worth
    alarming anyone about.

    This is logged here, at WARNING, because it is a deliberate operator action
    *and* because the killed request may lose its own `event=request` line: the
    cancellation can interrupt the background task that emits it.
    """
    entry = inflight.get(request_id)
    if entry is None:
        return JSONResponse(
            {"error": f"no in-flight request with id {request_id} — it already finished"},
            status_code=404,
        )
    logger.warning(
        "Cancelling in-flight request #%d: %s (%s) on %s, %s for %.1fs — operator action",
        entry.id,
        entry.model or entry.path,
        entry.state,
        entry.provider or "no backend yet",
        "streaming" if entry.stream else "buffered",
        max(0.0, time.monotonic() - entry.arrived),
    )
    entry.cancel()
    return {"id": entry.id, "status": "cancelled", "state": entry.state}


@admin.get("/inflight/{request_id}/body")
async def admin_inflight_body(request_id: int, request: Request):
    """The prompt and reply captured for one row of the feed.

    Deliberately not part of the snapshot: an agentic client's prompt runs to
    hundreds of KiB, and the console polls every second. Fetched per row instead,
    only when someone opens it.

    404 covers three cases that are all "nothing to show": capture disabled
    (`INFLIGHT_BODIES=false`), the row aged out of the history, or the request
    carried no body at all.
    """
    if not conf.INFLIGHT_BODIES:
        return JSONResponse(
            {"error": "body capture is disabled (INFLIGHT_BODIES=false)"}, status_code=404
        )
    rec = inflight.bodies(request_id)
    if rec is None:
        return JSONResponse(
            {"error": f"no captured body for request {request_id}"}, status_code=404
        )
    return {"id": request_id, "limit": conf.INFLIGHT_BODY_LIMIT, **rec}




def _fronted_natives() -> dict:
    """`{provider: {native_id: group_name}}` — which backend ids a logical model
    already fronts.

    The editor needs it to answer the only question that matters about a pinned
    id: *what does a client actually have to send for this?* A group fronting an
    id means clients use the group's name and the raw id is hidden from
    `/v1/models` (see `registry.list_models`), so showing the id alone is
    misleading. Computed here rather than in the browser because
    `conf.native_for` already knows how an inherited target resolves.
    """
    out = {}
    for name, lm in conf.LOGICAL_MODELS.items():
        for t in lm.targets:
            out.setdefault(t.provider, {}).setdefault(
                conf.native_for(name, t.provider, t.model), name
            )
    return out


def _config_snapshot() -> dict:
    """The config as the console edits it. **Never** includes `api_key`: a
    provider reports only whether one is set (`has_api_key`), preserving the rule
    that secrets do not leave the process."""
    fronted = _fronted_natives()
    return {
        "path": conf.CONFIG_PATH,
        "writable": configwrite.config_writable(),
        "providers": [
            _provider_view(p, editable=True, fronted=fronted) for p in conf.PROVIDERS
        ],
        "logical_models": [
            {"name": name, "targets": _serialize_targets(name, lm, raw=True)}
            for name, lm in conf.LOGICAL_MODELS.items()
        ],
        "aliases": dict(conf.ALIASES),
        "routing": {
            "queue_timeout": conf.ROUTING.queue_timeout,
            "failover": conf.ROUTING.failover,
            "auto_group": conf.ROUTING.auto_group,
            "down_backoff": conf.ROUTING.down_backoff,
            "queue_affinity": conf.ROUTING.queue_affinity,
            "affinity_max_skips": conf.ROUTING.affinity_max_skips,
        },
    }


async def _persisted(ok: bool, error, what: str):
    """Shared tail of every config mutation: reload on success, then hand back the
    fresh snapshot so the console re-renders from the file rather than from what it
    hoped it wrote."""
    if ok:
        logger.info("Config edit from the console: %s", what)
        await _apply_config_change()
    else:
        logger.warning("Config edit refused (%s): %s", what, error)
    body = _config_snapshot()
    body["persisted"] = ok
    body["error"] = error
    return JSONResponse(body, status_code=200 if ok else 422)


def _bad(message: str):
    return JSONResponse({"error": message}, status_code=422)


@admin.get("/config")
async def admin_config(request: Request):
    """The editable config: providers (no secrets), logical models, aliases,
    routing. Backs the console's Config tab."""
    return _config_snapshot()


@admin.put("/config/providers/{name}/enabled-models")
async def admin_set_enabled_models(name: str, request: Request):
    """Set which upstream models a backend may serve.

    An empty list is meaningful, not a no-op: it means "expose everything this
    backend live-reports" (`Provider.lists_all`), which is how the ollama boxes are
    configured. So the editor can move a provider in both directions.
    """
    if name not in conf.PROVIDERS_BY_NAME:
        return JSONResponse({"error": f"unknown provider '{name}'"}, status_code=404)
    body = await request.json()
    models = body.get("models")
    if models is None:
        models = []
    if not isinstance(models, list) or any(
        not isinstance(m, str) or not m.strip() for m in models
    ):
        return _bad('body must be {"models": ["native-id", ...]} of non-empty strings')
    cleaned = list(dict.fromkeys(m.strip() for m in models))
    ok, error = await configwrite.set_enabled_models(name, cleaned)
    return await _persisted(ok, error, f"{name}.enabled_models = {len(cleaned)} model(s)")


@admin.put("/config/providers/{name}/model-map")
async def admin_set_model_map(name: str, request: Request):
    """Rename a backend's native ids for clients: `{"model_map": {native: canonical}}`.

    The mapping must be a **bijection** — two native ids cannot share one canonical
    name on the same provider. `Provider.__post_init__` builds the reverse lookup by
    inverting this dict, so a collision silently makes one of them unreachable by
    canonical name (verified: the last one wins). That is refused here rather than
    written, with the advice that the multi-backend case wants a group instead.
    """
    if name not in conf.PROVIDERS_BY_NAME:
        return JSONResponse({"error": f"unknown provider '{name}'"}, status_code=404)
    body = await request.json()
    mapping = body.get("model_map")
    if mapping is None:
        mapping = {}
    if not isinstance(mapping, dict):
        return _bad('body must be {"model_map": {"native-id": "canonical-name", ...}}')
    cleaned = {}
    for native, canonical in mapping.items():
        native, canonical = str(native).strip(), str(canonical).strip()
        if not native or not canonical:
            return _bad("every mapping needs both a native id and a name")
        if any(c.isspace() for c in native) or any(c.isspace() for c in canonical):
            return _bad(
                f"'{native or canonical}' contains a space — a model id is a single "
                f"token; enter the native id and the name in separate fields"
            )
        if native in cleaned:
            return _bad(f"native id '{native}' is listed twice")
        cleaned[native] = canonical
    dupes = {c for c in cleaned.values() if list(cleaned.values()).count(c) > 1}
    if dupes:
        return _bad(
            f"two native ids both map to {sorted(dupes)} — model_map must be one-to-one, "
            f"or the reverse lookup silently picks one. To serve one name from several "
            f"backends, make a group instead."
        )
    ok, error = await configwrite.set_model_map(name, cleaned)
    return await _persisted(ok, error, f"{name}.model_map = {len(cleaned)} mapping(s)")


@admin.put("/config/aliases")
async def admin_set_aliases(request: Request):
    """Replace the alias map. An alias shadows everything else in resolution, so
    one pointing at an unknown provider would silently break a model name — those
    are refused rather than written."""
    body = await request.json()
    aliases = body.get("aliases")
    if not isinstance(aliases, dict):
        return _bad('body must be {"aliases": {"name": "target", ...}}')
    cleaned = {}
    for key, value in aliases.items():
        key, value = str(key).strip(), str(value).strip()
        if not key or not value:
            return _bad("alias names and targets must both be non-empty")
        if conf.PROVIDER_SEP in value:
            prefix = value.split(conf.PROVIDER_SEP, 1)[0]
            if prefix not in conf.PROVIDERS_BY_NAME:
                return _bad(
                    f"alias '{key}' points at unknown provider '{prefix}' — "
                    f"use 'provider:model' with a configured provider, or a bare model name"
                )
        cleaned[key] = value
    ok, error = await configwrite.set_aliases(cleaned)
    return await _persisted(ok, error, f"aliases = {sorted(cleaned)}")


@admin.put("/config/models/{name}")
async def admin_set_logical_model(name: str, request: Request):
    """Create or replace a logical model (a "group"): one client-facing name in
    front of an ordered list of backend targets."""
    name = name.strip()
    if not name:
        return _bad("model name must not be empty")
    if name in conf.ALIASES:
        return _bad(
            f"'{name}' is already an alias — an alias is resolved first, so the "
            f"logical model would never be reached. Remove the alias first."
        )
    body = await request.json()
    targets = body.get("targets")
    if not isinstance(targets, list) or not targets:
        return _bad('body must be {"targets": [{"provider", "priority", "model"?}, ...]}')
    cleaned = []
    for item in targets:
        if not isinstance(item, dict):
            return _bad("each target must be an object")
        provider = str(item.get("provider", "")).strip()
        if provider not in conf.PROVIDERS_BY_NAME:
            return _bad(f"unknown provider '{provider}'")
        try:
            priority = int(item.get("priority", 100))
        except (TypeError, ValueError):
            return _bad(f"target {provider}: priority must be an integer")
        if priority < 1:
            return _bad(f"target {provider}: priority must be 1 or greater")
        native = item.get("model")
        native = str(native).strip() if native else None
        cleaned.append({"provider": provider, "priority": priority, "model": native})
    seen = {(t["provider"], t["model"]) for t in cleaned}
    if len(seen) != len(cleaned):
        return _bad("duplicate (provider, model) target in the list")
    cleaned.sort(key=lambda t: t["priority"])
    ok, error = await configwrite.set_logical_model(name, cleaned)
    return await _persisted(ok, error, f"models['{name}'] = {len(cleaned)} target(s)")


@admin.delete("/config/models/{name}")
async def admin_delete_logical_model(name: str, request: Request):
    """Drop a logical model. Its clients fall back to the ordinary resolution
    order, which for same-named backends is auto-group."""
    if name not in conf.LOGICAL_MODELS:
        return JSONResponse({"error": f"unknown logical model '{name}'"}, status_code=404)
    ok, error = await configwrite.delete_logical_model(name)
    return await _persisted(ok, error, f"deleted models['{name}']")


@admin.get("/routing")
async def admin_routing(request: Request):
    """The routing graph: providers (with live slot/health state), explicit
    logical models and their prioritized targets, and aliases. No api_key."""
    providers = [_provider_view(p, live=True) for p in conf.PROVIDERS]
    logical_models = [
        {"name": name, "editable": True, "targets": _serialize_targets(name, lm)}
        for name, lm in conf.LOGICAL_MODELS.items()
    ]
    return {
        "auto_group": conf.ROUTING.auto_group,
        "config_writable": configwrite.config_writable(),
        "providers": providers,
        "logical_models": logical_models,
        "aliases": conf.ALIASES,
    }


@admin.post("/routing/{model}")
async def admin_set_routing(model: str, request: Request):
    """Rearrange a logical model's target priorities: applied live, then persisted
    into the config file (surgical priority-only rewrite; see app/configwrite.py).

    Reorder only — the (provider, model) set must match exactly. The new priorities
    are written to the live Targets and the list re-sorted so the slot picker's
    priority tiers stay correct on the next request. Persistence is best-effort:
    on a read-only mount (or unrecognized config format) the live change stands
    and the response says `persisted: false` with the reason.
    """
    lm = conf.LOGICAL_MODELS.get(model)
    if lm is None:
        return JSONResponse({"error": f"unknown logical model '{model}'"}, status_code=404)

    body = await request.json()
    incoming = body.get("targets")
    if not isinstance(incoming, list) or not incoming:
        return JSONResponse(
            {"error": 'body must be {"targets": [{"provider","model","priority"}, ...]}'},
            status_code=422,
        )
    try:
        wanted = {(item["provider"], item["model"]): int(item["priority"]) for item in incoming}
    except (KeyError, TypeError, ValueError):
        return JSONResponse(
            {"error": "each target needs provider, model and an integer priority"},
            status_code=422,
        )

    existing = {
        (t.provider, conf.native_for(model, t.provider, t.model)): t for t in lm.targets
    }
    if set(wanted) != set(existing):
        return JSONResponse(
            {"error": "targets must match the model's existing (provider, model) set exactly — reorder only, no add/remove"},
            status_code=422,
        )

    for key, target in existing.items():
        target.priority = wanted[key]
    lm.targets.sort(key=lambda t: t.priority)
    logger.info(
        "Routing priorities updated for '%s': %s",
        model,
        ", ".join(f"{t.provider}={t.priority}" for t in lm.targets),
    )

    persisted, persist_error = await configwrite.persist_model_priorities(model, lm.targets)
    if not persisted:
        logger.warning(
            "Routing change for '%s' applied live but NOT persisted: %s", model, persist_error
        )
    return {
        "name": model,
        "editable": True,
        "targets": _serialize_targets(model, lm),
        "persisted": persisted,
        "persist_error": persist_error,
    }


@app.exception_handler(_AdminForbidden)
async def _admin_forbidden_handler(request: Request, exc: _AdminForbidden):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


# Every /admin/* route is declared on `admin` above, which carries the auth
# dependency. Included here — after the declarations, and still above the
# catch-all proxy route so /admin/* wins over the proxy path.
app.include_router(admin)


_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@app.get("/favicon.ico")
async def favicon():
    """Serve a real icon instead of letting the browser's automatic request fall
    through to the catch-all, where it was resolved as a *model* name: with no
    JSON body it took the passthrough path, got auth-gated into a 401, and showed
    up in the request feed as a failed request. Every browser asks for this on
    every page load, so it was pure noise in the one view meant to be readable."""
    return FileResponse(
        os.path.join(_STATIC_DIR, "favicon.ico"),
        media_type="image/vnd.microsoft.icon",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/robots.txt")
async def robots():
    """Same reasoning as the favicon: a crawler or browser probing this path is
    not a model request. Nothing here should be indexed."""
    return Response(content="User-agent: *\nDisallow: /\n", media_type="text/plain")


# Convenience redirects to the dashboard. The StaticFiles mount only serves
# `/ui/...`; a bare `/ui` — or someone guessing `/admin` — would otherwise fall
# through to the catch-all proxy and get a confusing 401. Send them to /ui/.
@app.get("/ui")
@app.get("/admin")
@app.get("/admin/")
async def _ui_redirect():
    return RedirectResponse(url="/ui/")


# Static web dashboard. Mounted before the catch-all so /ui/* wins over the proxy;
# html=True serves index.html at /ui/. Lives under app/static (already COPYd in).
app.mount(
    "/ui",
    StaticFiles(directory=_STATIC_DIR, html=True),
    name="ui",
)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def catch_all(request: Request, path: str):
    if request.url.query:
        path = f"{path}?{request.url.query}"
    return await proxy_request(request, path)
