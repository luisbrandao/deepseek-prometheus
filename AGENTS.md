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
| `app/router.py` | `resolve(model) -> [Target]` (async). Resolution order: alias → `provider:model` → `models:` logical → auto-group. `_allow_listed` is the fallback for `auto_group: false` only — with auto-grouping on it can match nothing `_auto_group` doesn't. Returns `[]` when nothing serves the model — **never** a guessed backend. |
| `app/slots.py` | Per-provider concurrency. `acquire(targets, timeout, on_skip)` / `release(provider, model)` / `poke()`. Priority admission (round-robin within a tie tier), then an **explicit** queue of `_Waiter` futures with model-affinity reordering. Also `in_use`/`resident_model`/`queue_depth` for introspection. |
| `app/inflight.py` | The request feed backing the console's In-flight tab: `begin()` per request, `Entry.wait/run/chunk/token/record`, `Entry.tps()` (tokens/s over the handler's measured upstream time — the log's `speed_tps`, never a second clock), `Entry.finish()` (freezes into a bounded `_recent` ring buffer, `INFLIGHT_HISTORY`), `snapshot()`, `Entry.cancel()` for the Kill button, and a separate bounded `_bodies` store (`set_request`/`add_response`/`bodies()`) read only by `/admin/inflight/{id}/body`. Observation only apart from `cancel` — nothing in the *request path* reads it. |
| `app/registry.py` | `/v1/models` listing, live model discovery (cached, single-flight), and backend health (`mark_down`/`is_down`/`clear_down`). |
| `app/auth.py` | Bearer-key gate: `is_authorized(request)`, `restricted(provider)`. |
| `app/proxy.py` | Request lifecycle: parse → resolve → gate → `_dispatch` (acquire slot, build body, forward, failover) → `_handle_non_stream` / `_handle_stream`. Also decompression, `_error` (the one OpenAI-shaped error envelope) and `_relay_headers`. |
| `app/trim.py` | The context guardrail: `trim_request(payload, body_str, asked)` returns a shrunk copy of a chat body that declares `num_ctx` and is estimated to exceed it, or `None` when nothing needs to change. Excerpts old oversized tool results first, then drops the oldest turns at tool-call block boundaries; system messages always survive. Returns a `Trimmed` (payload + dropped/capped/before/after/budget) that `_route` writes to the entry (`Entry.mark_trimmed`) so the row badge and the log's `trimmed=`/`trim_capped=` fields carry the same numbers. Configured by the `trim:` section (`conf.TRIM`). Called once per request in `proxy._route`, before `_dispatch`. |
| `app/version.py` | Build identity — `VERSION` (the image tag, e.g. `master-52`), `REVISION` (git sha), `summary()`, `as_dict()`. Read from `APP_VERSION`/`APP_REVISION` at import; baked in by the Dockerfile from CI build-args. **Not** hot-reloaded — it is build metadata, not config. |
| `app/upstream.py` | The two shared `httpx.AsyncClient`s and their timeouts. Everything outbound goes through here so connections are pooled; `FORWARD_TIMEOUT` is long-read/short-connect on purpose. Closed by `main`'s lifespan. |
| `app/metrics.py` | Prometheus counters/gauges (`llm_proxy_` prefix). Never persisted — see the metrics note below. |
| `app/logbuffer.py` | In-memory ring buffer (`logging.Handler`) of recent log lines, seq-stamped, for the `/admin/logs` tail. Process-local like the slot/health state. |
| `app/configwrite.py` | Persists console edits into `CONFIG_PATH` via a **ruamel.yaml round-trip** (comments/key order/quoting preserved). `persist_model_priorities`, `set_enabled_models`, `set_aliases`, `set_logical_model`, `delete_logical_model` — each returns `(ok, reason)`. Abort-don't-corrupt; in-place write (bind-mount inode). |
| `app/main.py` | FastAPI app, routes, logging unification, lifespan (config hot-reload watcher + upstream pool shutdown). The `/admin/*` API lives on the `admin` `APIRouter`, which carries the auth dependency, plus the `/ui` static mount. `_provider_view` / `_serialize_targets` are the single serializers all three admin views share. |
| `tests/` | pytest + pytest-asyncio. `pip install -r requirements-dev.txt && pytest`. See **Testing** below for what each file guards. |
| `app/static/` | The web console (`index.html` + `app.css` + `app.js`). Vanilla, no build step; served via `StaticFiles` at `/ui/`. |

## Request lifecycle (`proxy.proxy_request`)

1. Parse body; extract `model`, `stream`. No model → passthrough to first provider (auth-gated, no slots).
2. `authorized = auth.is_authorized(request)`.
3. `router.resolve(model)` → ordered targets.
4. Gate: if unauthorized, drop `require_permission` targets; none left → **401**.
5. Drop currently-down targets (keep as last resort).
6. `trim.trim_request` — the context guardrail. Only touches a body with `messages` + an
   integer `num_ctx` that is estimated to exceed it; anything else passes through untouched.
   Runs once, before any target is chosen, so every failover attempt forwards the same body.
7. `_dispatch`: loop — `slots.acquire` → `_build_body` (rewrite model id, inject `provider_routing`) → forward. Fails over on two conditions: an `httpx.RequestError` (connection failure) **or** an upstream response whose status is in `ROUTING.failover_statuses` (`_should_failover`, default 429/5xx). Either one → release slot, `mark_down`, drop this target, try next. Exhausted: a connection failure → `_backend_error`; a relayed upstream error → that last response **verbatim** (real status + body). `clear_down` runs only on a `< 400` response.

## Layering

Strict topological order — nothing imports upward, nothing imports sideways
within a layer, so there are no cycles:

```
main.py                        ASGI entry: routes, /admin router, lifespan
  └─ proxy.py                  request lifecycle, failover
       └─ router.py            name -> ordered targets
            └─ registry.py     discovery + health
  registry · slots · inflight · auth · clientinfo · configwrite · trim     services
       └─ config.py            the leaf everything reads
  config.py · metrics.py · logbuffer.py · upstream.py · version.py   leaves
```

`config.py` is the only module everything depends on, and it is read **per
operation** (`conf.X`) rather than snapshotted at import. That is the whole
mechanism behind hot reload: `reload_if_changed()` rebinds the module globals,
and because there is no `await` between the rebinds, a request sees either the
old config or the new one, never a mix.

## Where state lives

Every piece of state that gates admission is a plain dict in one process's
memory. That is not an oversight — it is what makes the slot handoff correct
without a lock — but it is why the single uvicorn worker is load-bearing, and why
a restart is a real reset rather than an inconvenience. Three tiers, and the
difference between them is deliberate:

| Tier | What | Why |
|---|---|---|
| **Dropped on every config reload** | `registry._cache`, `registry._last_good` | A changed `base_url` points somewhere else entirely, so a catalog from the previous endpoint is worse than no catalog. |
| **Survives a reload, lost on restart** | `slots._in_use` / `_running` / `_last_model` / `_waiters`, `registry._down_until`, `inflight._active` / `_recent` / `_bodies`, `logbuffer._buf`, the Prometheus counters | Keyed by provider *name*, so it survives the rebind on purpose: a request holding a slot still holds it after the edit. |
| **Durable, outside the process** | `config.yaml` (ruamel round-trip, in place), Loki (one `event=request` line per request), Prometheus (scraped counters) | The durable copy of every request is its log line, which is exactly why the in-memory history is allowed to be lossy. |

Most of the invariants below are consequences of that table rather than
independent rules. If you add process-local state, add it to the reset list in
`tests/conftest.py` too, or it leaks between tests.

## Invariants — do not break these

- **Single worker.** Slot/queue/health state is in-process. Never add `--workers > 1`
  without moving that state to a shared store.
- **Config is hot-reloaded — always read it as `conf.X` at use time.** Never
  `from app.config import PROVIDERS` (a snapshot that goes stale after a reload; the
  dataclasses/helpers like `Provider`/`Target`/`strip_prefix` are fine to import) and
  never cache config values across requests. `reload_if_changed()` rebinds the module
  globals with no `await` in between, so a request sees either the old or the new
  config, never a mix. After a reload the watcher drops `registry._cache` and pokes
  queued slot waiters; derived state keyed by provider *name* (`slots._in_use`,
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
- **Decompression reads raw bytes, and handles exactly what it advertises.**
  `_handle_non_stream` uses `aiter_raw()` + manual `_decompress` so we control decoding.
  `_build_headers` caps the forwarded `Accept-Encoding` at `gzip, deflate` and
  `_decompress` implements exactly those two — **keep them aligned**. The `br`/`zstd`
  branches that used to be there could never fire (nothing requested those encodings)
  and would have raised `ImportError` if they had, since neither library is a
  dependency; they were removed. To add one you need all three: the library in
  `requirements.txt`, the branch, *and* the encoding advertised in `_build_headers`.
- **Auth gate consistency.** Any new model-listing or routing path must apply the same
  `require_permission` filtering as `registry.list_models` and `proxy_request`.
- **Never substitute a backend the client didn't ask for.** `router.resolve` returning `[]`
  means "nothing serves this model" and must become a 404 (`proxy._model_not_found`), not a
  fallback to `PROVIDERS[0]`. That fallback existed and caused a production bug: an
  unauthenticated caller requesting a free local model, during a live-discovery blip, was
  routed to the first-listed backend — a `require_permission` one — and received a 401
  about a model they never asked for and cannot see. The same rule applies to the
  model-less passthrough path, which must pick the first backend the *caller is permitted
  to use*, not the first configured one. When resolution is uncertain, fail loudly; a wrong
  backend produces an error that cannot be diagnosed from the client side.
- **Discovery failure ≠ empty catalog.** `registry._cached_live` keeps the last successful
  catalog for `STALE_GRACE` when a probe fails (re-probing every `STALE_RETRY`), because a
  backend mid-model-swap misses its probe window routinely. Caching `[]` on failure made
  every model on that backend unresolvable for a full `cache_ttl` — which is what triggered
  the bug above. `clear_cache()` drops the last-known catalogs too, since a config reload
  may have repointed `base_url`.
- **Admin surface (`/admin/*`, `/ui`).** The gate is **structural**: every `/admin/*`
  route is declared on the `admin` `APIRouter`, which carries
  `Depends(require_admin)`, so a new endpoint is protected by construction. Declare
  new admin routes on that router, never on `app` directly — the old per-handler
  prologue failed silently, since an endpoint that forgot it was simply open. The
  403 body is `{"error": "unauthorized"}` via `_AdminForbidden`; keep that shape,
  the console and any scripts read it. Provider serialization must **never** include
  `api_key` — secrets stay in-process. `POST /admin/routing/{model}` mutates
  `LOGICAL_MODELS[*].targets` priorities in place and must re-`sort` the target list
  afterwards, or the priority-tier `groupby` in `slots._pick_free` breaks. Routes +
  the `/ui` mount live **above** the catch-all in `main.py` so they win over the
  proxy path. `/favicon.ico` and `/robots.txt` are there for the same reason and are
  not cosmetic: without them a browser's automatic favicon request falls into the
  catch-all, is treated as a model-less passthrough, 401s, and lands in the request
  feed as a failed request on every page load. Any other well-known browser path
  belongs there too.
- **Body capture is bounded and never leaves the process.** `_bodies` is capped per
  side (`INFLIGHT_BODY_LIMIT`) and evicted with the history, because an agentic
  client's prompt is routinely hundreds of KiB. It must stay out of `snapshot()` —
  the console polls that every second — and out of stdout, which is what separates it
  from `LOG_INPUT`/`LOG_OUTPUT`: those go to the log shipper, this does not. It is
  auth-gated like every other `/admin/*` route because it holds prompt text.
- **Config write-back (`configwrite.py`) round-trips through ruamel, never PyYAML.**
  The config is a hand-annotated, git-tracked file on the deploy host, so a write
  must preserve comments — `yaml.dump` would erase all 31 of them. Use `_yaml()`;
  its `indent(mapping=2, sequence=4, offset=2)` reproduces this project's list
  indentation (ruamel's default collapses it and inflates the diff five-fold) and
  `preserve_quotes`/`width=4096` stop quote and line-wrap churn. Intra-flow
  alignment padding is the one thing ruamel cannot keep; that normalizes once.
  The write must be **in-place** (`r+` + truncate): `/app/config.yaml` is a
  single-file bind mount, so replace-by-rename detaches from the host inode.
  **Mutate the loaded document in place; never rebuild a node.** ruamel remembers
  each scalar's quote style and each collection's flow/block style, and replacing a
  `CommentedMap`/`CommentedSeq` discards all of it — which made a save that changed
  nothing rewrite `"a": "b"` as `a: b` and flatten block lists into flow. Every
  editor here keeps surviving entries at their original position untouched and only
  writes genuine additions, removals and value changes. There is a regression test
  for exactly this: two consecutive identical saves must leave the file
  byte-identical.
  Abort-don't-corrupt: every mutation is serialized, re-parsed with plain PyYAML,
  and self-checked to contain exactly the requested change before the file is
  opened — plus a hard guard that the provider list is unchanged, since adding or
  dropping a backend is the blast radius that takes routing down. Any surprise
  returns `(False, reason)` with the file untouched. Persist failures are warnings,
  never 500s.
- **A target's `model` is inherited when omitted — preserve that through an edit.**
  `Target.model is None` means "resolve via the provider's `model_map`", and
  **`config.native_for` is the single place that resolution lives**. It used to be
  three: inline in `router._from_logical`, plus near-identical copies in `main` and
  `configwrite` — and this AGENTS.md claimed there was one. Call it, don't re-derive
  it. Both sides of a priority write need it: callers hand over live `Target`s where
  an inherited id is still `None`, while the file must be matched on the id it
  resolves to. The config API likewise reports `model` raw and `resolved_model`
  separately (`_serialize_targets(..., raw=True)`) — collapsing them would make the
  editor write explicit pins for every inherited target on the first save, changing
  what the config means without changing what it says.
- **`api_key` never leaves the process, including through the config editor.**
  `_config_snapshot` emits `has_api_key`, never the value; `configwrite` reads and
  writes key lines back verbatim without ever returning one. Any new config endpoint
  must keep that property.
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
- **Outbound calls use the shared clients in `app/upstream.py`; never build one
  per request.** Both were `httpx.AsyncClient(...)` constructed and closed inside the
  handler, which threw the connection pool away after a single call — every request to
  a remote backend paid a fresh TCP handshake plus a full TLS negotiation. The clients
  are process-wide and closed by the lifespan, so **never `aclose()` one from a request
  path**; a stream returns its connection by exiting `stream_cm`, nothing more. A stale
  pooled connection surfaces as `httpx.RemoteProtocolError`, a `RequestError` subclass,
  so the existing failover handles it.
- **The forwarding timeout is long-read, short-connect, and both halves matter.**
  `FORWARD_TIMEOUT = httpx.Timeout(600.0, connect=5.0)`. The 600s read is deliberate:
  a large model loading, or a backend that buffers the whole completion, routinely
  spends minutes before the first byte. Do not shorten it. The short connect is equally
  deliberate: a bare `Timeout(600.0)` sets *connect* to 600s too, and since a handshake
  completes in the kernel before the request is sent, no model-loading time lands
  there — the only way to wait on connect is a host that black-holes SYN, which will
  never answer. That request holds its slot for the whole wait, so on a single-slot
  local backend one bad request was a ten-minute outage, with `down_backoff` never
  engaging because nothing had failed yet.
- **The request log carries both model names.** `model=` is the **native** id on the
  wire and must stay that way — existing dashboards and recording rules key off it.
  `asked=` is the canonical name the client sent, added because a logical model
  spanning three backends resolves to three different native ids, so `model=` alone
  splits one model's traffic across three series and a failover moves a request between
  them mid-flight. `asked=` is the field to group by. It is omitted when the two are
  equal, so an unmapped model logs exactly as before.
- **The version string must stay identical to the image tag.** CI passes
  `APP_VERSION=master-${{ github.run_number }}` as a build-arg, which is the same value
  the metadata step turns into the `master-N` image tag; `app/version.py` reads it and
  `/health`, `/metrics` (`llm_proxy_build_info`) and the console header report it. That
  equality is the whole point — the compose file pins a tag, and this is what lets you
  confirm the process *answering requests* is that build rather than a container that
  never got recreated. If you change how the tag is derived, change the build-arg in the
  same commit. The `ARG` lines sit **after** `pip install` in the Dockerfile on purpose:
  `APP_VERSION` changes every build, and an `ARG` above the install would invalidate that
  layer every time. An undefined `ARG` expands to `""`, not to nothing, which is why
  `version.py` treats blank as absent and reports `dev`.
- **The context guardrail must be a no-op for anything it was not built for.** `trim`
  acts only on a JSON chat body with a `messages` list *and* an integer `num_ctx`, and
  only when the estimate exceeds the budget; every other body — no `num_ctx`, a fitting
  chat, embeddings, a malformed `messages` — must come back as `None` so `_route`
  forwards the client's bytes unchanged. A client that manages its own context is never
  second-guessed. When it does act: system messages are never dropped, an assistant
  `tool_calls` message and its `tool` results are kept or dropped together (a dangling
  call is a 400 at most backends), and the newest block is always sent even when it alone
  is over budget. It reads `num_ctx` *before* `_build_body` applies `strip_fields`, so a
  Google target that strips the field still gets the trimmed conversation. Read it as
  `conf.TRIM` at call time — it hot-reloads like everything else.
- **Metric names use the `llm_proxy_` prefix** (renamed from `deepseek_proxy_`).
- **Never persist or re-seed the counters.** A restart resetting them to zero is a real
  counter reset, and `rate()`/`increase()` handle it correctly. A snapshot restored from
  disk does not: it always lags the last scrape, so the counter returns *lower* than what
  Prometheus already read, and Prometheus credits the whole pre-drop value as new increase.
  The phantom equals the entire counter, however small the dip. `METRICS_PERSIST` did this
  and was removed; don't reintroduce it. Long-range totals belong in a recording rule
  (`sum_over_time(<counter>:increase5m[range])`), not in the process.

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

pytest + pytest-asyncio, no network, sub-second:

```bash
pip install -r requirements-dev.txt
pytest
```

The suite exists because most of the section above is invariants — properties that
are invisible in review and expensive in production. Each file guards a specific
one:

| File | Guards |
|---|---|
| `tests/test_slots.py` | Priority admission, round-robin within a tie tier, queue-when-full, the synchronous handoff (no barging), affinity reordering, the `affinity_max_skips` starvation bound, and **no slot stranded** by timeout or cancellation. |
| `tests/test_configwrite.py` | Formatting fidelity — comments, key order, quote style, block-vs-flow lists — and the headline case: **two consecutive identical saves must leave the file byte-identical.** Plus abort-don't-corrupt on every refusal path. |
| `tests/test_router.py` | Resolution order, inherited vs pinned native ids, and that an unknown model resolves to `[]` and **never** to `PROVIDERS[0]`. Also that `_auto_group` subsumes `_allow_listed`, which is what lets the latter live behind `auto_group: false`. |
| `tests/test_proxy.py` | The full lifecycle through `httpx.MockTransport`: failover on both triggers, error relay, streaming, decompression, the request log. Every test asserts slot occupancy returns to zero. |
| `tests/test_version.py` | The build-identity chain: blank args mean `dev`, a CI build reports its tag and sha, `/health` carries them, and `llm_proxy_build_info` is exposed. |
| `tests/test_gate.py` | Catalog visibility with and without a key, the 401/404 distinction, admin gating, and that `api_key` never appears in a response. |
| `tests/test_admin_contract.py` | The exact fields `app/static/app.js` dereferences from each admin view. The console is untyped with no build step, so a dropped field shows up as a blank cell rather than an error. |
| `tests/test_trim.py` | The context guardrail is a no-op unless `num_ctx` is present and exceeded (a fitting body returns `None`, not a copy); system messages survive; the oldest turns go first; a tool call and its results are never separated at the cut (swept across budgets); old oversized tool results are excerpted before any turn is dropped while recent ones stay whole; the newest turn is always sent; images are counted flat; the `trim:` section parses and hot-reloads. `test_proxy.py` covers the wire: a trimmed body reaches the backend, a fitting one is forwarded verbatim, and a failover re-sends the same trimmed body. |
| `tests/test_inflight.py` | The feed's derived numbers, driven on `Entry` directly: tokens/s divides over upstream time (queue wait excluded), takes the handler's recorded duration over the clock, is reset by a failover, keeps the `~` estimate when a backend never reports usage, and rounds exactly as the log formats. `test_proxy.py` closes the loop by matching a history row to its `speed_tps` line. |

Conventions that matter when adding tests:

- `tests/conftest.py` sets `CONFIG_PATH` **before** anything imports `app.*`, because
  `config.py` reads it at import time.
- `reset_state` is autouse and clears the process-local dicts between tests. Add new
  process-local state to it, or tests leak into each other.
- Use `load_config(text)` to install a config and `providers(...)` to install
  `Provider` objects directly; both go through the real code paths.
- Mock upstream with `httpx.MockTransport`, and build responses with
  `test_proxy.mock_response` — a plain `httpx.Response(json=...)` arrives with its
  content already consumed and `aiter_raw()` raises `StreamConsumed`.
- `test_proxy.py` deliberately sets `queue_timeout: 3` rather than the production
  default of 0. Both backends have one slot, so a leaked slot under "wait forever"
  hangs the suite instead of failing it; the wait-forever path is covered directly in
  `test_slots.py`.

A quick smoke check without the suite:

```bash
CONFIG_PATH=config.example.yaml python -c "from app import main; print('imports OK')"
```

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
