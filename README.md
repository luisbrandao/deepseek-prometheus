# LLM Proxy

OpenAI-compatible reverse proxy for **multiple LLM backends**, with **priority-based
load balancing**, per-backend **concurrency limits (slots)**, automatic **failover**,
a bearer-key **permission gate**, **Prometheus metrics**, **token counting**, and
**structured logging**.

Drop it between your LLM clients (Open WebUI, OpenCode, LangChain, any OpenAI SDK, …)
and any number of upstream backends (DeepSeek, Ollama hosts, OpenRouter, …). Clients
request a **clean model name** — the proxy decides *which* backend actually serves it.

## Highlights

- **Transparent routing** — clients use a plain model name (`deepseek-v4-flash`,
  `local.qwen-medium:low`); no need to know which server runs it.
- **Priority + slots** — prefer your fast/cheap backend, cap concurrency per backend,
  and **queue** requests when every candidate is busy.
- **Swap-aware queueing** — when a local backend frees up, the queue prefers a request
  for the model it *already has loaded*, so it isn't made to swap out a model it is
  about to need again.
- **Failover** — if a backend errors, the request retries the next one; offline
  backends drop out of the model list and are skipped.
- **In-flight visibility** — the console's In-flight tab lists every request currently
  running or queued, what each one is waiting on, and how long it has waited.
- **Permission gate** — mark paid backends `require_permission: true`; only callers
  with a valid `Authorization: Bearer` key see or use them.
- **OpenRouter provider routing** — pin which upstream OpenRouter uses (e.g. force
  DeepSeek-official instead of whatever's cheapest).
- **Response decompression** — transparently decodes gzip/deflate/brotli upstream
  bodies that a client couldn't otherwise read.

## Architecture

```
Your App (OpenAI client)
       │  POST /v1/chat/completions   { "model": "deepseek-v4-flash" }
       ▼
┌──────────────────────────────────────────────┐
│                  llm-proxy                     │
│  resolve model → ordered targets               │
│  auth gate → slot acquire (priority+queue)     │
│  forward → failover on error → decompress      │
│  log tokens · export metrics                   │
└───┬─────────────┬──────────────┬───────────────┘
    ▼             ▼              ▼
 OpenRouter    DeepSeek     Ollama hosts        (any backend in config.yaml)
 (10 slots)    (10 slots)   (1 slot each, citrine preferred over gw)
```

## How routing works

A client sends a **canonical model name** — a clean, provider-agnostic id. Native
(provider-specific) ids never leak to clients. The proxy resolves the canonical name to
an ordered list of **targets** (a real native model on a real backend), then admits the
request to the highest-priority target that has a free slot.

Two layers turn native ids into canonical names: a backend's `model_map` (per-provider
native↔canonical dictionary) and the `models:` block (cross-provider routing table). They
compose — the logical model picks the backends and order; each backend's `model_map`
supplies the native id. See [Canonical names](#canonical-names).

**Resolution order:**
1. **Alias** — a global short name → `provider:canonical` (e.g. `chat` → `deepseek:deepseek-v4-pro`).
2. **Explicit `provider:model`** — forces one backend (the model part is canonical, rewritten to native).
3. **Logical model** — a `models:` entry mapping one canonical name to several prioritized targets.
4. **Auto-group** — the same canonical name served by multiple backends is load-balanced
   automatically, ordered by each backend's `priority` (no config needed).
5. **Allow-listed backend** — a backend whose `enabled_models` names it explicitly.

If none of those match, the request gets a **404 `model_not_found`** — the proxy never
picks a backend on the client's behalf:

```json
{"error": {"message": "The model 'x' does not exist or is not available on this proxy",
           "type": "invalid_request_error", "param": "model", "code": "model_not_found"}}
```

> **This used to be a last-resort guess** — an unresolved name was sent to whichever
> backend was listed *first* in the config. That looked like graceful degradation and was
> a trap. If the first backend is `require_permission`, a caller with no key asking for a
> **free local model** got `401 Model 'x' requires authentication` — an auth error about a
> backend they never chose and cannot see. It happened in production, intermittently,
> whenever live discovery blipped (see below), and was unexplainable from the client side.
> A model nothing is known to serve now says exactly that.

### Slots, priority & queueing

Each backend declares `slots` (max concurrent in-flight requests, shared across all its
models) and `priority` (lower = preferred). When a request arrives the proxy takes a
slot from the **highest-priority candidate that has one free**. When several candidates
**tie on priority**, it round-robins across the tied backends that have a free slot, so
equal-priority backends share load evenly. If all candidates are full it **waits**
(indefinitely by default, or up to `routing.queue_timeout`) for the first slot to free.

> Example: `glm-4.7-flash` runs on `ollamaCitrine` (fast, priority 1) and `ollamaGW`
> (slow, priority 2). Citrine is preferred until full, then gw; if both are full the
> request waits. Two backends at the *same* priority would instead alternate per request.

A released slot is handed **directly** to a chosen waiter, in the same step as the
release — it is never observable as free while somebody is queued for it, so a newly
arrived request cannot barge past the queue.

#### Model affinity: the queue reorders to avoid swaps

Local backends (llama-swap, ollama) hold one model resident and swap on demand, and a
swap costs seconds of load time — usually far more than the request itself. Strict FIFO
provokes the worst case. Take a single-slot `citrine` serving `modelA` and `modelB`:

| | queue order served | backend model loads |
|---|---|---|
| FIFO | `modelA` running → `modelB`, `modelA` | **3** — swap B in, swap A back |
| Affinity | `modelA` running → `modelA`, `modelB` | **2** — A stays, B swaps in once |

So when capacity frees, admission prefers a waiter whose model that backend **already has
resident** — running there now, or the one it just finished — over one that would force a
swap. Measured on a synthetic backend with a 3s swap and 1s generation, that exact
three-request scenario ran in **9.0s instead of 12.0s**, and the second `modelA` request
finished in 3.6s instead of 10.6s. The trade is real and worth stating: `modelB` waited
about a second longer.

This only reorders *waiting* requests. It never changes which backends a request may use,
and never holds a slot idle hoping for a better match — a lone non-matching waiter is
admitted immediately.

**Starvation is bounded.** A waiter passed over `routing.affinity_max_skips` times (default
`3`) is admitted next regardless of what is loaded, so a steady stream of `modelA` cannot
starve `modelB` indefinitely. Set `affinity_max_skips: 0` or `queue_affinity: false` for
strict FIFO.

Watch it work: `llm_proxy_queue_affinity_grants_total` counts reorderings and
`llm_proxy_queue_starvation_yields_total` counts times the cap had to intervene (a
consistently high ratio of yields means the cap is too tight for your traffic). The
In-flight tab marks a passed-over row `passed over N×` and shows each backend's resident
model on its chip.

**In Grafana:** import [`grafana-queue-dashboard.json`](grafana-queue-dashboard.json)
(Dashboards → New → Import → paste the JSON, then pick your Prometheus datasource). Or
query it directly:

```promql
# model loads avoided over the dashboard range
sum(increase(llm_proxy_queue_affinity_grants_total[$__range])) or vector(0)

# reorderings per minute, per backend
sum by (provider) (rate(llm_proxy_queue_affinity_grants_total[$__rate_interval])) * 60

# how often the starvation cap has to step in
sum(increase(llm_proxy_queue_starvation_yields_total[$__range])) or vector(0)
```

The `or vector(0)` is not decoration: a labelled Prometheus counter does not exist until
its first increment, so a panel for a cap that has never fired reads **No data** rather
than zero — which looks like a broken dashboard. Note also that a counter's first ever
sample cannot be an `increase()`, so a freshly created series shows 0 until it moves
again.

### Failover

If a chosen backend errors the proxy releases the slot, marks the backend down for
`routing.down_backoff` seconds, and retries the next-priority target. "Errors" means both
a connection-level failure (connection refused, timeout, …) **and** an upstream HTTP
response whose status is in `routing.failover_statuses` (default `429, 500, 502, 503,
504`) — a backend that answers `503 Service Unavailable` is failed over just like one
that's unreachable. Deliberate 4xx (bad request, auth) are relayed to the client as-is,
since every backend would reject them identically. Once **all** candidates fail, the
client gets the last upstream error verbatim (its real status and body), not a synthetic
502. Streaming requests can fail over up to the first byte (after that the HTTP status is
already committed).

## Configuration

All routing config lives in `config.yaml` (see `config.example.yaml`). Provider API keys
go inline, or optionally via `${ENV_VAR}` interpolation. Proxy auth keys and runtime
flags come from the environment.

Edits to `config.yaml` are **hot-reloaded** — the file is checked every
`CONFIG_RELOAD_INTERVAL` seconds (default 3, `0` disables) and applied live, no restart
needed. Requests already in flight finish under the settings they started with. A broken
edit (YAML typo) is logged and ignored; the proxy keeps running on the previous config
until the file parses again.

> **Bind-mount gotcha:** with the default single-file mount
> (`./config.yaml:/app/config.yaml`), edit the file **in place** on the host. Editors
> that save by *rename-and-replace* (vim's default, `sed -i`) swap the host inode, and
> the container keeps watching the old file — the edit never becomes visible inside.
> `nano`, a `>` redirect, or `:set backupcopy=yes` in vim write in place. Mounting the
> containing directory instead of the single file avoids the issue entirely.

```yaml
aliases:                          # optional short names -> provider:canonical
  chat: deepseek:deepseek-v4-pro

providers:
  - name: deepseek
    api_key: "sk-..."             # or "${DEEPSEEK_API_KEY}"
    require_permission: true      # paid -> only authenticated callers
    base_url: "https://api.deepseek.com"
    enabled_models: [deepseek-chat, deepseek-reasoner]   # native ids
    slots: 10
    cache_ttl: 3600
    model_map:                    # native -> canonical
      deepseek-chat: deepseek-v4-pro
      deepseek-reasoner: deepseek-v4-flash

  - name: ollamaCitrine
    api_key: ""                   # empty -> no auth header (Ollama)
    base_url: "http://citrine.brandao:11434"
    enabled_models: []            # empty -> expose ALL models (live-discovered)
    slots: 1                      # single GPU box
    priority: 1                   # faster -> preferred over gw

  - name: ollamaGW
    base_url: "http://gw.brandao:11434"
    enabled_models: []
    slots: 1
    priority: 2

  - name: openRouter
    api_key: "sk-or-..."
    require_permission: true
    base_url: "https://openrouter.ai/api"
    enabled_models: [z-ai/glm-5.2]            # native ids
    slots: 10
    model_map:                                # native -> canonical
      z-ai/glm-5.2: glm-5.2
      deepseek/deepseek-v4-flash: deepseek-v4-flash
    provider_routing:                         # pin OpenRouter's upstream (native key)
      deepseek/deepseek-v4-flash: [deepseek]

# One canonical name backed by several prioritized targets. Omit `model:` to inherit
# the native id from each provider's model_map; set it to pin a specific native id.
models:
  deepseek-v4-flash:
    targets:
      - {provider: openRouter, priority: 1}   # native via model_map
      - {provider: deepseek,   priority: 2}

# Global routing behavior.
routing:
  queue_timeout: 0    # seconds to wait for a free slot; 0 = wait forever
  failover: true      # retry the next target on backend error
  auto_group: true    # identical canonical names across backends load-balance
  down_backoff: 15    # seconds a failed backend is skipped before retry
  failover_statuses: [429, 500, 502, 503, 504]  # upstream statuses that fail over
```

### Provider fields

| Field | Description |
|---|---|
| `name` | Backend key, also the `provider:` routing prefix |
| `base_url` | Upstream base URL |
| `api_key` | Sent as `Authorization: Bearer` upstream. Empty → no auth header (e.g. Ollama) |
| `enabled_models` | Allow-list in **native** ids. **Empty** = expose all (live-queried). **Non-empty** = exactly these (no live call) |
| `slots` | Max concurrent in-flight requests (shared across the backend's models). Omit = unlimited |
| `priority` | Preference when a model has several backends; lower wins. Default = config order. Ties round-robin across backends with a free slot |
| `require_permission` | `true` → gated behind a proxy auth key (default `false`) |
| `strip_path_prefix` | Path segment removed before appending to `base_url`. For OpenAI-compatible backends whose root isn't `/v1` — e.g. Google Gemini (`v1` → its `/v1beta/openai/...`) |
| `strip_fields` | Top-level request-body keys to drop before forwarding. For strict backends that 400 on unknown fields (e.g. Google rejects the `num_ctx` some clients inject) |
| `model_map` | Per-provider **native → canonical** dictionary. Drives `/v1/models` display (native→canonical) and request rewrite (canonical→native). Must be a bijection |
| `provider_routing` | OpenRouter only — per-model upstream pinning, keyed by **native** id (list = strict order, dict = verbatim `provider` field) |
| `headers` | Extra headers sent upstream, applied as **defaults** (a header the client already sent wins). For backend attribution the client can't set itself — e.g. OpenRouter app identity (`HTTP-Referer` / `X-Title`). Values support `${ENV}` |
| `cache_ttl` | Seconds the live `/models` result is cached for this backend |

### Canonical names

> Short version, since this is the part everyone forgets: a **canonical name** is what
> clients send (`glm-5.2`) and a **native id** is what one backend calls that same model
> on the wire (`zai-org/glm-5.2` on nanoGPT, `z-ai/glm-5.2` on openRouter). `model_map`
> translates between them, per provider. The console's Config tab has the same
> explanation behind a (?) next to each section.

Clients only ever see and send **canonical** names; native (provider-specific) ids stay
internal. Two layers mint canonical names:

- **`model_map`** (per provider) — the native↔canonical dictionary for *one* backend. A
  pure rename/translation; no routing. Keyed by the native id.
- **`models:`** (global) — the cross-provider routing table in canonical space (priority,
  failover, load-balancing). A target's native id is its explicit `model:`, or — when
  omitted — inherited from that provider's `model_map`.

They compose without overlap: `models:` decides *which backends in what order*; `model_map`
decides *what each backend calls the thing*. A canonical name reachable both as a logical
model and via auto-group resolves as the logical model (it's earlier in the order).

### `models:` and `routing:`

- **`models:`** — declare logical models in canonical space (the flash example). Use an
  explicit `model:` only to pin a specific native id (e.g. a per-box quant); otherwise it's
  inherited from `model_map`. Backends sharing the *same* canonical name auto-group without
  an entry.
- **`routing:`** — `queue_timeout`, `failover`, `auto_group`, `down_backoff`,
  `failover_statuses`, `queue_affinity`, `affinity_max_skips` (see above).

### Environment variables

Set in `docker-compose.yml` — **not** in `config.yaml`:

| Env var | Default | Description |
|---|---|---|
| `PROXY_API_KEYS` | _(empty)_ | Comma-separated proxy auth keys for gated backends. Empty = gate disabled |
| `LOG_INPUT` | `false` | Log the full proxied request (curl-style, auth masked). Toggleable at runtime via `/logging` |
| `LOG_OUTPUT` | `false` | Log the upstream response (pretty JSON; streaming reassembled). Toggleable at runtime via `/logging` |
| `PORT` | `8000` | Port the proxy binds to inside the container |
| `CONFIG_PATH` | `config.yaml` | Path to the YAML config |
| `CONFIG_RELOAD_INTERVAL` | `3` | Seconds between config-file change checks (hot reload). `0` disables |
| `TZ` | `America/Sao_Paulo` | Timezone for log timestamps (image ships tzdata; timestamps are ISO-8601 with offset) |
| `RESOLVE_CLIENT_HOST` | `true` | Reverse-DNS the caller IP for the request log (cached, off-loop, time-bounded) |
| `CLIENT_DNS_TIMEOUT` | `1.0` | Seconds to wait for a reverse-DNS lookup before logging IP-only |
| `TRUST_PROXY_HEADERS` | `true` | Trust `X-Forwarded-For` / `X-Real-IP` for the caller IP (set `false` behind no proxy) |
| `INFLIGHT_HISTORY` | `200` | Finished requests the In-flight tab keeps below the live ones (in-memory, lost on restart) |
| `INFLIGHT_BODIES` | `true` | Attach each request's prompt + reply to its In-flight row. **Holds prompt text in memory** (bounded, never written to disk or stdout, admin-gated); `false` keeps the feed metadata-only |
| `INFLIGHT_BODY_LIMIT` | `16384` | Bytes kept per side (request / reasoning / response) before truncating |

> **Single worker required.** Slot/queue accounting is in-process, so run **one**
> uvicorn worker (the default). Multiple workers would split the accounting and break
> the concurrency caps.

## Authentication & permissions

Backends marked `require_permission: true` are gated. A request is **authorized** when it
carries `Authorization: Bearer <key>` with a key listed in `PROXY_API_KEYS`.

- **With a valid key** → sees and uses every backend.
- **Without a key** → gated backends are hidden from `/v1/models`, and requests to them
  (including explicit `provider:model` pins) return **401**. Open backends work for everyone.
- **No keys configured** → the gate is inert; everything is open.

> Use case: share your Ollama hosts with a friend (no key, open) while keeping your paid
> DeepSeek/OpenRouter backends private (key required).

## Quick Start

```bash
cp config.example.yaml config.yaml   # edit backends, slots, priorities, keys
# set PROXY_API_KEYS and other env vars in docker-compose.yml (see table above)
docker compose up -d
```

Point any OpenAI client at `http://<host>:8000/v1/chat/completions` and request a clean
model name (e.g. `deepseek-v4-flash`). Pass `Authorization: Bearer <key>` for gated backends.

## Endpoints

| Path | Method | Description |
|---|---|---|
| `/health` | `GET` | `{"status": "ok"}` health check |
| `/metrics` | `GET` | Prometheus-format metrics |
| `/models`, `/v1/models` | `GET` | Aggregated model list (clean names; honors the auth gate) |
| `/logging` | `GET` | Current `log_input` / `log_output` state |
| `/logging` | `POST` | Toggle request/response logging at runtime (honors the auth gate) |
| `/ui/` | `GET` | Web console (Logging / In-flight / Models / Routing). Static, served by the proxy |
| `/admin/logs` | `GET` | Recent log lines from an in-memory ring buffer (`?since=<seq>&level=<min>`) |
| `/admin/inflight` | `GET` | The request feed: live (running + queued) plus recent finished ones, with per-provider slot occupancy |
| `/admin/inflight/{id}/cancel` | `POST` | Kill one in-flight request. `404` if it already finished |
| `/admin/inflight/{id}/body` | `GET` | The prompt and reply captured for one row (see `INFLIGHT_BODIES`) |
| `/favicon.ico`, `/robots.txt` | `GET` | Served directly, so browser noise never reaches the proxy path |
| `/admin/upstream-models` | `GET` | Probes every backend's raw `/v1/models` directly (`?provider=name` for just one) |
| `/admin/routing` | `GET` | Routing graph: providers (live slots/health), logical models + priorities, aliases |
| `/admin/routing/{model}` | `POST` | Rearrange a logical model's target priorities — applied live **and** persisted into the config file |
| `/admin/config` | `GET` | The editable config: providers (no secrets), logical models, aliases, routing |
| `/admin/config/providers/{name}/enabled-models` | `PUT` | Set a backend's upstream allow-list (`[]` = allow all, live discovery) |
| `/admin/config/aliases` | `PUT` | Replace the alias map |
| `/admin/config/models/{name}` | `PUT` / `DELETE` | Create, replace or drop a logical model (group) |
| `/*` | any | Catch-all proxy — routed from the request body's `model` |

All `/admin/*` endpoints are gated by the same bearer keys as `POST /logging` (the
log buffer can contain request/response bodies when `LOG_INPUT`/`LOG_OUTPUT` are on),
and never serialize provider `api_key`s.

### Web console (`/ui/`)

A built-in, dependency-free dashboard served by the proxy itself — open
`http://<host>:9999/ui/` and paste a proxy key (stored in your browser's
`localStorage`, sent as `Authorization: Bearer`). Five tabs:

- **Logging** — live log tail (level filter, pause, autoscroll) plus the
  `LOG_INPUT` / `LOG_OUTPUT` runtime toggles.
- **In-flight** — what the proxy is doing *right now*, polled every second, as a rolling
  feed: **live requests pinned on top** (newest first), then the recently finished ones
  below a divider. Every row is numbered with the request id.
  - A **running** row shows the backend it landed on, the native model id, live token
    counts, and — for streams — a chunk counter that ticks as tokens arrive. A **queued**
    row shows which backends it is waiting on and for how long. Both carry age /
    queued-for / running-for.
  - **Live token counts** come with a caveat worth knowing: an upstream reports `usage`
    only in its *final* streaming chunk (verified against llama.cpp — 1 chunk in 33), so
    while a request runs, `tok out` is the proxy's own count of generation steps, shown as
    `~40`, and is replaced by the authoritative number the instant usage arrives. `tok in`
    is genuinely unknowable before then and reads `0`. A buffered (non-streaming) request
    reveals nothing until it returns, so both read `0` while it runs.
  - A **finished** row is frozen at completion with its status, total time, queue wait and
    token counts (`done` / `cancelled` / `failed`, colour-coded). History is capped by
    `INFLIGHT_HISTORY` and is lost on restart by design — the durable per-request record is
    the `event=request` log line.
  - Every row shows who called (service from the `User-Agent`, plus IP and reverse-DNS name)
    and the request body size.
  - **Body** expands the row to show the prompt this request sent and the reply that came
    back — pretty-printed, with a thinking model's reasoning kept separate from its answer.
    On a running request the reply fills in as it streams. This is the reason it exists:
    reading one request end-to-end here beats grepping it out of a log stream that also
    carries every `/metrics` scrape. Bodies are fetched per row on demand, never in the
    1s poll.
  - **Kill** cancels a live request, on a single click. Works on a queued request *and* on
    one wedged in an upstream read that will never return; the slot is freed immediately
    and the caller gets a `503`. See [Killing a request](#killing-a-request).
  - The toolbar totals running / queued / done and shows each provider's `in_use/slots`,
    amber when full — so a growing queue reads straight against the capacity causing it.
    See [Slots, priority & queueing](#slots-priority--queueing).
- **Config** — edit `config.yaml` from the browser; every save rewrites the file
  (comments preserved) and takes effect immediately, no restart.
  - **Upstream models** — per backend, either *allow all* (use whatever it live-reports)
    or an explicit list. **Probe backend** fetches its real catalog as checkboxes; an id
    that is pinned but no longer reported stays visible and checked, marked
    *not in catalog*, so saving cannot silently drop it. A paid provider can report
    hundreds of ids, so the list has a **filter** plus **Tick shown** / **Untick shown**
    for bulk selection of whatever the filter matches — typing and ticking never re-render
    the list, so focus and scroll position survive.
  - **Model groups** — add, edit and delete logical models: pick each target's backend,
    native id (blank inherits from `model_map`) and priority.
  - **Aliases** — add, edit and delete short names. An alias pointing at an unknown
    provider is refused rather than written, since an alias resolves ahead of everything
    else and a broken one silently breaks a model name.
  - Each section has a **(?)** panel explaining the concepts in place — in particular
    canonical vs. native names, which is the thing worth having on screen while you are
    staring at a list of native ids.
  - `api_key` values are never sent to the browser; a provider shows only whether one is
    set. Editing secrets and adding/removing whole providers stay file-side for now.

#### Killing a request

The **Kill** button cancels the asyncio task serving that request, which unwinds the
ordinary cleanup path: the slot is released, the upstream connection closed, and the
request moves into the history as `cancelled`. Cancellation rather than a flag, because
the requests worth killing are blocked inside an `await` — a queued one inside the slot
Condition, a running one inside an upstream read that may never return.

What the caller sees depends on how far the request got:

- **Queued, or a buffered (non-streaming) response** — a clean `503` with
  `{"error": {"type": "cancelled"}}`, the same shape as a queue timeout.
- **Mid-stream** — the SSE stream simply ends. The `200` was committed with the first
  chunk, so there is no status code left to send; the client sees a truncated completion
  and a normal end of body.

Each kill is logged at `WARNING` with the request id, model, backend and elapsed time.
- **Models** — the aggregated catalog, plus a **Probe upstreams** button that queries
  each backend's real `/v1/models` so every endpoint's full list is visible at once
  (independent of `enabled_models`).
- **Routing** — each logical model drawn as connected boxes (model → prioritized
  targets) with live down/busy badges; rearrange priorities with `↑`/`↓` or by editing
  the number. Edits apply immediately **and are written back into the config file**.
  Auto-grouped models and aliases are shown read-only.

#### How console edits reach the file

Every console edit rewrites `config.yaml` through a **ruamel.yaml round-trip**, so
comments, key order, quoting and blank lines survive. Measured on this project's own
production config (189 lines, 31 comments): a round-trip keeps every comment and
produces a semantically identical document. The one irreducible change is
hand-alignment *padding inside flow maps* —
`{provider: nanoGPT,␣␣␣␣␣␣priority: 2}` becomes `{provider: nanoGPT,␣priority: 2}`,
because ruamel does not track intra-flow spacing. That normalization happens **once**,
on the first console write.

Requirements & behavior:

- The config volume must be mounted **read-write** (the bundled compose does this).
  On a `:ro` mount the change is refused with *"not persisted"* and the file is
  untouched; the Config and Routing tabs show a read-only warning.
- **Abort-don't-corrupt.** Every edit is serialized, re-parsed with plain PyYAML, and
  checked to contain exactly the requested change *before* the file is opened for
  writing. A mutation may never add or remove a provider — that guard is explicit.
  Any surprise aborts with a reason and leaves the file byte-identical.
- The write is **in-place** (`r+` + truncate), never write-temp-then-rename:
  `/app/config.yaml` is a single-file bind mount, so a rename would detach from the
  host inode.
- The edit is applied to the live process before the response returns, so the console
  re-renders from the file rather than from what it hoped it wrote.
- `api_key` values are read and written back verbatim and **never** leave the process.
  A provider reports only `has_api_key`.

**Inherited vs. explicit native ids.** A target's `model:` may be omitted, meaning
"inherit the native id from this provider's `model_map`". The editor keeps that
distinction: an inherited target shows an empty field with the resolved id as a
greyed `inherits …` placeholder, and saving it unchanged does **not** write an
explicit pin. Otherwise opening a group and pressing Save would silently harden every
inherited id — changing what the config means without changing what it says.

### `/models` aggregation

Lists, deduplicated, in **canonical** names: aliases, logical models, and each remaining
canonical model once (native ids translated through `model_map`). A model served by several
backends appears a **single** time (the proxy load-balances behind it). Backend-prefixed
`provider:model` ids are **not** listed — they still work for pinning a specific backend, but
advertising them would just duplicate the clean names. Likewise, anything a logical model
fronts is **hidden** — both its canonical name and the concrete native ids of its targets —
so clients use the stable logical name instead, and per-backend variants (e.g. quantizations)
don't flap in and out of the list as backends come and go. Live-discovered backends are queried (`GET {base_url}/v1/models`),
cached for `cache_ttl`, coalesced via single-flight so a burst of cold requests triggers one
probe per backend; offline backends drop out.

**A failed probe does not blank a backend.** A local backend busy loading a model can miss
its `/v1/models` window, and treating that as "this box has no models" made every model on
it unresolvable for a full `cache_ttl`. Instead the last catalog that *did* succeed stands
in for up to 5 minutes (`registry.STALE_GRACE`), re-probing every 15s while it does, and is
logged each time. Past that window a backend that is still failing really is gone and its
models drop out of the listing. Gated backends are hidden from callers
without a valid key.

## Prometheus Metrics

Scrape `http://<host>:8000/metrics`:

| Metric | Type | Labels | Description |
|---|---|---|---|
| `llm_proxy_requests_total` | Counter | `provider`, `model` | Completed requests |
| `llm_proxy_tokens_input_total` | Counter | `provider`, `model` | Cumulative input tokens |
| `llm_proxy_tokens_output_total` | Counter | `provider`, `model` | Cumulative output tokens |
| `llm_proxy_request_duration_seconds` | Histogram | `provider`, `model` | Request latency (0.1–600s) |
| `llm_proxy_errors_total` | Counter | `provider`, `model`, `status_code` | Failed requests |
| `llm_proxy_slots_in_use` | Gauge | `provider` | In-flight requests holding a slot |
| `llm_proxy_queue_waiting` | Gauge | — | Requests currently waiting for a slot |
| `llm_proxy_failovers_total` | Counter | `provider` | Failovers away from a backend |
| `llm_proxy_queue_affinity_grants_total` | Counter | `provider` | Times the queue admitted a request ahead of an earlier one to avoid a model swap |
| `llm_proxy_queue_starvation_yields_total` | Counter | `provider` | Times `affinity_max_skips` forced FIFO to unblock a passed-over request |

> **Metric prefix:** as of the multi-backend rework these use `llm_proxy_` (was
> `deepseek_proxy_`). Update existing Grafana/alert queries accordingly.

### Persistence across restarts (optional)

In-memory counters reset to zero on restart, which Prometheus sees as a counter reset and
loses the delta around the reboot. Enable **`METRICS_PERSIST=true`** to snapshot the
cumulative counters to `METRICS_PERSIST_PATH` (lazily, every `METRICS_FLUSH_INTERVAL`
seconds and on graceful shutdown) and re-seed them on boot, keeping totals continuous.

Only counters are persisted; live gauges (`slots_in_use`, `queue_waiting`) and the latency
histogram intentionally reset. **The path must be on a volume that outlives the
container** (the bundled `docker-compose.yml` mounts `./data`), otherwise the file is
recreated empty each restart and persistence is a no-op.

| Env var | Default | Description |
|---|---|---|
| `METRICS_PERSIST` | `false` | Enable counter persistence |
| `METRICS_PERSIST_PATH` | `metrics_state.json` | Where the snapshot is written (use a mounted volume) |
| `METRICS_FLUSH_INTERVAL` | `30` | Seconds between lazy snapshots |

## Request Logging

Every completed request — success **or** error — emits exactly one structured
[logfmt](https://brandur.org/logfmt) line on stdout:

```
ts=2026-06-17T02:48:13-03:00 level=info event=request provider=openRouter model=z-ai/glm-5.2 status=200 stream=true in=5524 out=890 dur=0:00:26 speed_tps=34.91 client_ip=192.168.1.50 client_host=workstation.lan svc=OpenWebUI ua="OpenWebUI/0.5"
```

| Field | Meaning |
|---|---|
| `ts` | ISO-8601 timestamp **with offset** (local time per `TZ`; unambiguous regardless of reader) |
| `provider`, `model` | Backend chosen and the upstream model id sent to it |
| `status` | Upstream HTTP status relayed to the client |
| `stream` | Whether the response was streamed |
| `in`, `out`, `dur` | Prompt/completion tokens and wall-clock duration as `H:MM:SS` (rounded up to the second, so a fast request reads `0:00:01` not `0:00:00`) |
| `speed_tps` | Output tokens/s. **Omitted for embeddings & rerankers** — they return no completion tokens, so it would always be a misleading `0.00` |
| `op`, `in_tps` | Present only on no-output ops (**`embedding`**, **`rerank`**): the op kind plus input tokens/s — the throughput that matters when nothing is generated |
| `client_ip` | Caller address (`X-Forwarded-For`/`X-Real-IP` honored when `TRUST_PROXY_HEADERS`) |
| `client_host` | Reverse-DNS of `client_ip` (omitted if unresolved or `RESOLVE_CLIENT_HOST=false`) |
| `svc`, `ua` | Service guessed from the User-Agent's leading token, and the full User-Agent |
| `err` | Short error category (`invalid_request`, `unauthorized`, `rate_limited`, `upstream_error`…); absent on success |

An **embedding or rerank** request returns no completion tokens (`out=0`), so it's tagged
`op=embedding` / `op=rerank` and reports `in_tps` (input tokens/s) in place of `speed_tps` —
an output-tokens/s figure would just be a constant `0.00` that skews throughput panels.
Detection is by request path (`/v1/embeddings`, `/v1/rerank`, …) **and** model id (e.g. a
`*-Reranker-*` / `*-Embedding-*` name), so it works even on a nonstandard path:

```
ts=2026-06-30T12:24:12-03:00 level=info event=request provider=ollamaGW model=Qwen3-Reranker-8B op=rerank status=200 stream=false in=9515 out=0 dur=0:00:12 in_tps=792.92 client_ip=172.19.0.19 client_host=open-webui.main svc=python-requests ua=python-requests/2.34.2
```

A request whose body carries no resolvable `model` (a non-chat / multipart passthrough —
forwarded untouched to the first provider) is logged instead as **`event=passthrough`**,
keyed by `method` + `path` rather than a model, e.g.:

```
ts=2026-06-17T12:10:23-03:00 level=info event=passthrough provider=deepseek method=POST path=/models status=405 stream=false client_ip=192.168.0.79 client_host=luis.brandao svc=PostmanRuntime ua="PostmanRuntime/7.52.0" err=invalid_request
```

So `model=` never appears as `unknown`, and these requests stay out of per-model dashboards
(they're also excluded from the `llm_proxy_*` model metrics). Split them in Loki with
`| logfmt | event="request"` vs `event="passthrough"`.

Because it's logfmt, Loki/Grafana parse it with no regex:

```logql
{container="llm-proxy"} | logfmt | status>=`400`            # all failed requests
{container="llm-proxy"} | logfmt | provider=`openRouter`    # one backend
sum by (client_host) (count_over_time({container="llm-proxy"} | logfmt [1h]))  # calls per caller machine
```

The line is emitted **after** the response is delivered (background task / stream
end), so reverse-DNS never adds latency to the caller.

`LOG_INPUT` logs the full proxied request curl-style (auth masked); `LOG_OUTPUT` logs the
upstream response pretty-printed (streaming reassembled into one JSON with
`_assembled_content` / `_reasoning_content`). These verbose dumps and uvicorn's access/error
logs use the human-readable `<ts> LEVEL <msg>` format (same ISO-8601 timestamp).

Both flags can be flipped at runtime — no restart required:

```bash
curl http://<host>:8000/logging                          # current state
curl -X POST http://<host>:8000/logging \
  -H "Authorization: Bearer <key>" \
  -d '{"log_input": true, "log_output": true}'           # either key optional
```

The `POST` requires a valid proxy key when `PROXY_API_KEYS` is set (otherwise open). The
env vars still set the state at boot; runtime changes are not persisted across restarts.

### Error logging & relay

Upstream errors are **always logged with their response body** (WARNING level, pretty-printed,
truncated at 4 KB) regardless of `LOG_OUTPUT` — the body is where the backend says *why* it
rejected the request:

```
2026-06-11T23:42:22-03:00 WARNING Upstream error 400 from 'google' (model: gemini-2.5-pro):
{
  "error": { "code": 400, "message": "Unknown name 'num_ctx': Cannot find field.", ... }
}
```

The error body and status are also relayed to the client verbatim — including on **streaming**
requests, where the upstream's 4xx/5xx is returned as a plain JSON response instead of being
wrapped in a bogus `200` SSE stream. The one-line `event=request … status=400 err=invalid_request`
summary above is emitted in addition, so you can both alert on it and read the full reason.

> **Streaming token counts:** upstreams only emit a `usage` block in a streamed response
> when the request sets `stream_options: {"include_usage": true}`. The proxy **injects
> this automatically** on streamed requests (unless the client explicitly set it), so
> token metrics/logs are reliable for streams too. As a result the client receives a final
> usage chunk (standard OpenAI streaming behavior). Non-streaming always reports usage.

## CI / CD

Pushes to `master` (ignoring `.md`-only changes) build the Docker image and push to GitHub
Container Registry, tagged `latest` and `master-{run_number}`, using `GITHUB_TOKEN`.

## Tech Stack

- **FastAPI** + **uvicorn** — async ASGI
- **httpx** — async client with streaming
- **prometheus-client** — native metrics
- **PyYAML** — config parsing
- **ruamel.yaml** — comment-preserving config *writes* from the console
- **Python 3.11**

## License

MIT
