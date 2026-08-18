# AGENTS.md

Guidance for AI agents working in this repo. Read this before changing routing,
concurrency, or auth code. User-facing docs live in `README.md`.

## What this is

An OpenAI-compatible reverse proxy (FastAPI + httpx) in front of multiple LLM
backends. Clients send a clean model name; the proxy resolves it to one or more
prioritized backends, gates by permission, load-balances across per-backend
concurrency slots (queueing when full), forwards, fails over on error, and
decompresses the response. Single process, async, one uvicorn worker.

## Module map

| File | Responsibility |
|---|---|
| `app/config.py` | Loads `config.yaml` + env. Dataclasses `Provider`, `Target`, `LogicalModel`, `Routing`. Exposes `PROVIDERS`, `PROVIDERS_BY_NAME`, `ALIASES`, `LOGICAL_MODELS`, `ROUTING`, `AUTH_KEYS`. Hot reload: `reload_if_changed()` re-reads the file and rebinds those globals (polled from `main._config_reload_loop`). |
| `app/router.py` | `resolve(model) -> [Target]` (async). Resolution order: alias → `provider:model` → `models:` logical → auto-group → fallback. |
| `app/slots.py` | Per-provider concurrency. `acquire(targets, timeout, on_skip)` / `release(provider, model)` / `poke()`. Priority admission (round-robin within a tie tier), then an **explicit** queue of `_Waiter` futures with model-affinity reordering. Also `in_use`/`resident_model`/`queue_depth` for introspection. |
| `app/inflight.py` | The request feed backing the console's In-flight tab: `begin()` per request, `Entry.wait/run/chunk/record`, `Entry.finish()` (freezes into a bounded `_recent` ring buffer, `INFLIGHT_HISTORY`), `snapshot()`, and `Entry.cancel()` for the Kill button. Observation only apart from `cancel` — nothing in the *request path* reads it. |
| `app/registry.py` | `/v1/models` listing, live model discovery (cached, single-flight), and backend health (`mark_down`/`is_down`/`clear_down`). |
| `app/auth.py` | Bearer-key gate: `is_authorized(request)`, `restricted(provider)`. |
| `app/proxy.py` | Request lifecycle: parse → resolve → gate → `_dispatch` (acquire slot, build body, forward, failover) → `_handle_non_stream` / `_handle_stream`. Also decompression + upstream error mapping. |
| `app/metrics.py` | Prometheus counters/gauges (`llm_proxy_` prefix) + `PERSISTABLE_COUNTERS`. |
| `app/persistence.py` | Optional: snapshot/restore cumulative counters to disk (`load`/`dump`/`flush_loop`). |
| `app/logbuffer.py` | In-memory ring buffer (`logging.Handler`) of recent log lines, seq-stamped, for the `/admin/logs` tail. Process-local like the slot/health state. |
| `app/configwrite.py` | Persists runtime routing edits into `CONFIG_PATH` via a surgical, priority-digits-only text rewrite (comments/format preserved). Abort-don't-corrupt; in-place write (bind-mount inode). |
| `app/main.py` | FastAPI app, routes, logging unification, lifespan (metrics load/flush/dump + the config hot-reload watcher). Also the `/admin/*` API + `/ui` static mount that back the web console. |
| `app/static/` | The web console (`index.html` + `app.css` + `app.js`). Vanilla, no build step; served via `StaticFiles` at `/ui/`. |

## Request lifecycle (`proxy.proxy_request`)

1. Parse body; extract `model`, `stream`. No model → passthrough to first provider (auth-gated, no slots).
2. `authorized = auth.is_authorized(request)`.
3. `router.resolve(model)` → ordered targets.
4. Gate: if unauthorized, drop `require_permission` targets; none left → **401**.
5. Drop currently-down targets (keep as last resort).
6. `_dispatch`: loop — `slots.acquire` → `_build_body` (rewrite model id, inject `provider_routing`) → forward. Fails over on two conditions: an `httpx.RequestError` (connection failure) **or** an upstream response whose status is in `ROUTING.failover_statuses` (`_should_failover`, default 429/5xx). Either one → release slot, `mark_down`, drop this target, try next. Exhausted: a connection failure → `_backend_error`; a relayed upstream error → that last response **verbatim** (real status + body). `clear_down` runs only on a `< 400` response.

## Invariants — do not break these

- **Single worker.** Slot/queue/health state is in-process. Never add `--workers > 1`
  without moving that state to a shared store.
- **Config is hot-reloaded — always read it as `conf.X` at use time.** Never
  `from app.config import PROVIDERS` (a snapshot that goes stale after a reload; the
  dataclasses/helpers like `Provider`/`Target`/`strip_prefix` are fine to import) and
  never cache config values across requests. `reload_if_changed()` rebinds the module
  globals with no `await` in between, so a request sees either the old or the new
  config, never a mix. After a reload the watcher drops `registry._cache` and pokes
  the slot Condition; derived state keyed by provider *name* (`slots._in_use`,
  `registry._down_until`) intentionally survives.
- **Every acquired slot must be released exactly once.** Non-stream: released in
  `_dispatch` after the call. Stream: released in the generator's `finally` via the
  `on_complete` callback (the handler returns before streaming finishes). If you add a
  code path, guarantee release on every exit including errors and client disconnect.
- **Async primitives are lazily created** (`registry._lock_for`, and each
  `slots._Waiter.future`) so they bind to uvicorn's running loop, not the import-time
  loop. Do not move them to module-level construction — it breaks under some
  Python/loop setups.
- **Slot admission is a synchronous handoff, and needs no lock.** `slots` has no
  Condition any more: a release decrements, then `_drain()` picks the best waiter and
  resolves its future, all in one synchronous step with no `await` between. That is what
  makes it correct without a lock and what stops a newly arrived request from barging
  past the queue — so **never introduce an `await` inside `_drain`/`_release`/`_choose`
  or the accounting becomes observable mid-update.** Two consequences to preserve:
  `acquire`'s `finally` must hand back a slot granted in the instant it gave up
  (`w.granted and not w.taken` — the timeout/cancel race), and `release` must be passed
  the **native model** that finished, or affinity has nothing to work from and silently
  degrades to FIFO.
- **Affinity only reorders waiting requests.** It must never change which targets a
  request is eligible for, never hold a slot idle waiting for a better match, and never
  be consulted on the fast path. `Routing.affinity_max_skips` is the starvation bound and
  is checked *before* affinity in `_choose` — keep that order, it is the only thing
  stopping a hot model from starving a cold one.
- **Streaming fails over only pre-first-byte.** `_handle_stream` pre-flights the
  connection (`stream_cm.__aenter__`) and re-raises `RequestError` so `_dispatch` can try
  the next target *before* a `StreamingResponse` commits its 200 status. Don't move the
  error handling inside the body generator.
- **Handlers raise, the dispatcher decides.** `_handle_non_stream` / `_handle_stream`
  must let `httpx.RequestError` propagate (for connection-level failover) and return a
  buffered `Response` carrying the upstream status for HTTP errors. `_dispatch` owns *both*
  failover triggers — the `RequestError` except-branch and the `_should_failover(status)`
  check on the returned response — and is the only place that converts errors to terminal
  client responses (`_backend_error`, 401, 503) or relays an upstream error verbatim.
- **Decompression reads raw bytes.** `_handle_non_stream` uses `aiter_raw()` + manual
  `_decompress` because httpx can't decode brotli without the lib. The forwarded
  `Accept-Encoding` is capped to `gzip, deflate` in `_build_headers`. Keep these aligned.
- **Auth gate consistency.** Any new model-listing or routing path must apply the same
  `require_permission` filtering as `registry.list_models` and `proxy_request`.
- **Admin surface (`/admin/*`, `/ui`).** Every `/admin/*` endpoint is gated by
  `auth.is_authorized` (the log buffer can hold request/response bodies once
  `LOG_INPUT`/`LOG_OUTPUT` are on). Provider serialization must **never** include
  `api_key` — secrets stay in-process. `POST /admin/routing/{model}` mutates
  `LOGICAL_MODELS[*].targets` priorities in place and must re-`sort` the target list
  afterwards, or the priority-tier `groupby` in `slots._pick_free` breaks. Routes +
  the `/ui` mount live **above** the catch-all in `main.py` so they win over the
  proxy path.
- **Config write-back (`configwrite.py`).** Persisting a routing edit rewrites ONLY
  the `priority: N` digits of the matched flow-style target lines; never re-emit the
  file through a YAML dumper (comments/format must survive — the config is
  git-tracked on the deploy host). The write must be **in-place** (`r+` + truncate):
  `/app/config.yaml` is a single-file bind mount, so replace-by-rename detaches from
  the host inode. Abort-don't-corrupt: any surprise (unmatched target, no
  `priority:` field, failed parse-back self-check) returns `(False, reason)` with
  the file untouched; the live change stands and the endpoint reports
  `persisted: false`. Persist failures are warnings, never 500s.
- **In-flight registry (`inflight.py`) has exactly one closer per entry.** `proxy_request`
  opens the entry and closes it for every response *except* a `StreamingResponse` — that
  one is still in flight when the handler returns, so `_handle_stream`'s generator closes
  it in its `finally`, in the leading sync block before any `await` (a raising await must
  not leave a phantom live row). `record()` before `finish()`, or the history row loses its
  status and token counts. `finish()` is idempotent — removal from `_active` is the guard,
  so a request can never appear twice in the feed. Mirror the slot-release rule: if you add
  a code path, guarantee the entry is closed on every exit including errors, cancellation
  and client disconnect. Keep it write-only from the request path — never let admission,
  routing or failover read it, or an observability bug becomes a routing bug.
- **Cancellation must not strand a slot.** The Kill button cancels the task serving a
  request, so `CancelledError` can now surface at *any* `await` in the request path —
  including ones that previously only ever saw `httpx.RequestError`. `_dispatch` therefore
  releases the slot in an `except asyncio.CancelledError` branch (the non-stream path has
  no `finally`, and for a stream the generator that owns the release never ran), and the
  stream pre-flight closes its client on `BaseException`. Any new await that holds a slot
  needs the same treatment. Cancellation prefers `Entry.stream_task` (bound inside the SSE
  generator) over the request task: Starlette runs a streaming body in a child task while
  the request task waits on client disconnect, so cancelling the latter mid-stream unwinds
  through Starlette's disconnect listener and uvicorn logs a spurious "Exception in ASGI
  application" traceback for a deliberate action.
- **Metric names use the `llm_proxy_` prefix** (renamed from `deepseek_proxy_`). Only
  cumulative counters are persisted (`metrics.PERSISTABLE_COUNTERS`); never persist gauges
  or the histogram. Persistence must never crash startup or a request — failures are logged
  and swallowed.

## Conventions

- Match existing style: small module-level functions, `_private` helpers, docstrings that
  explain *why*. No new deps without reason (stdlib first).
- New per-backend behavior usually means a `Provider` field in `config.py` + parsing in
  `_load` + documenting it in `README.md`, `config.example.yaml`, and here.
- Secrets: provider keys inline in `config.yaml` (or `${ENV}`); proxy auth keys via
  `PROXY_API_KEYS`. Never hard-code keys in `app/`. `config.yaml` and `.env` are not
  committed; keep `config.example.yaml` / `.env.example` in sync.
- Logging: human-readable lines (incl. uvicorn) use `<iso-ts> LEVEL <msg>` via the
  `llm-proxy` logger; timestamps are local-time ISO-8601 with offset (set `TZ`, image
  ships tzdata). One structured logfmt line per request (`event=request …`) goes to the
  prefix-free `llm-proxy.event` logger so Loki parses it with `| logfmt`. Caller
  identification lives in `app/clientinfo.py`. All handlers (ours + uvicorn's, the
  latter re-pointed in `_unify_logging`) write to **stdout**, not stderr, so
  `docker logs | grep` works without `2>&1`.

## Testing

No formal suite yet. Validate changes by importing with a config and exercising the
async functions directly, e.g.:

```bash
CONFIG_PATH=config.example.yaml python -c "from app import main; print('imports OK')"
```

For routing/slots/auth, drive the functions with mock backends (local `http.server`)
and mock `registry.provider_model_ids` / `registry._cached_live` for discovery. Cover:
priority admission, queue-when-full, cancellation (no slot leak), failover, the 401 gate,
and streaming slot release. Prefer adding a real `tests/` suite (pytest + pytest-asyncio)
if you extend behavior substantially.

## Canonical naming model

Clients only ever see/send **canonical** names; native (provider-specific) ids never leak.

- **`provider.model_map`** is a per-provider native↔canonical dictionary keyed by the
  **native** id (`{native: canonical}`). `to_canonical(native)` drives `/v1/models` display;
  `to_native(canonical)` rewrites the wire id when that provider is chosen. Must be a bijection
  per provider (the reverse lookup is built once in `__post_init__`).
- **`enabled_models`** is the allow-list in **native** ids (empty = live-discover all).
- **`models:` logical targets** live in canonical space. A target's `model` is the native id;
  omit it to inherit `provider.to_native(logical_name)`, set it to pin a specific native id
  (the per-quant case). `Target.model` is `None` when omitted — resolution fills it in.
- `router.resolve` returns Targets whose `model` is already the **native** wire id; `_build_body`
  sends it verbatim. `provider_routing` is keyed by that native id.
- `registry.list_models` hides anything a logical model fronts — by canonical name *and* by the
  concrete `(provider, native)` of each target (so explicit per-quant ids stay hidden too).

## Gotchas

- Model ids can contain `:` (e.g. `local.qwen-medium:low`). `provider:model` splits on the
  **first** `:` only, and only treats the prefix as explicit if it matches a known provider.
  Canonical names are colon-free by convention, so a native id like `zai-org/glm-5.2:thinking`
  maps cleanly to a canonical `glm-5.2`.
- `priority` lower = preferred; defaults to config order. Within a tie tier, admission
  round-robins across the tied backends that currently have a free slot (`slots._pick_free`).
- `queue_timeout: 0` means wait forever (the current default).
- Live discovery is cached per backend for `cache_ttl`; a down backend caches an empty list
  for the full ttl (no hammering) and silently rejoins on recovery.
- `/v1/models` hides ids that are targets of a logical model (clients use the stable logical
  name). A logical model is always listed regardless of backend liveness, so the catalog
  stays stable as backends come and go.
