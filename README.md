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
5. **Fallback** — first backend whose `enabled_models` serves it, else the first backend.

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
  `failover_statuses` (see above).

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
| `/admin/inflight` | `GET` | Snapshot of every request in flight (running + queued) with per-provider slot occupancy |
| `/admin/upstream-models` | `GET` | Probes every backend's raw `/v1/models` directly |
| `/admin/routing` | `GET` | Routing graph: providers (live slots/health), logical models + priorities, aliases |
| `/admin/routing/{model}` | `POST` | Rearrange a logical model's target priorities — applied live **and** persisted into the config file |
| `/*` | any | Catch-all proxy — routed from the request body's `model` |

All `/admin/*` endpoints are gated by the same bearer keys as `POST /logging` (the
log buffer can contain request/response bodies when `LOG_INPUT`/`LOG_OUTPUT` are on),
and never serialize provider `api_key`s.

### Web console (`/ui/`)

A built-in, dependency-free dashboard served by the proxy itself — open
`http://<host>:9999/ui/` and paste a proxy key (stored in your browser's
`localStorage`, sent as `Authorization: Bearer`). Four tabs:

- **Logging** — live log tail (level filter, pause, autoscroll) plus the
  `LOG_INPUT` / `LOG_OUTPUT` runtime toggles.
- **In-flight** — what the proxy is doing *right now*, polled every second: one row per
  request, **queued ones first**. A running row shows the backend it landed on, the native
  model id, and (for streams) a live chunk counter; a queued row shows the backends it is
  waiting on and how long it has waited. Each row carries age / queued-for / running-for
  timers, request body size, and who called (service from the `User-Agent`, plus IP and
  reverse-DNS name). The toolbar totals running vs. queued and shows each provider's
  `in_use/slots`, highlighted amber when full — so a growing queue can be read straight
  against the capacity that is causing it. See [Slots, priority & queueing](#slots-priority--queueing).
- **Models** — the aggregated catalog, plus a **Probe upstreams** button that queries
  each backend's real `/v1/models` so every endpoint's full list is visible at once
  (independent of `enabled_models`).
- **Routing** — each logical model drawn as connected boxes (model → prioritized
  targets) with live down/busy badges; rearrange priorities with `↑`/`↓` or by editing
  the number. Edits apply immediately **and are written back into the config file**.
  Auto-grouped models and aliases are shown read-only.

#### Runtime routing persistence

A priority change saved in the Routing tab is persisted into `config.yaml` with a
**surgical rewrite**: only the `priority: N` digits on the matching target lines
change — comments, alignment and everything else are preserved byte-for-byte, so a
git-tracked config shows a minimal, reviewable diff. Requirements & behavior:

- The config volume must be mounted **read-write** (the bundled compose does this).
  On a `:ro` mount the change still applies live, the response/UI report
  *"not persisted"*, and the file is untouched; the Routing tab shows a
  `config: read-only` warning badge.
- Targets must use the flow style used throughout this README:
  `- {provider: X, model: "Y", priority: N}` (one per line). Unrecognized layouts
  are refused cleanly (live-only), never rewritten.
- The rewritten text is parsed back and verified against the request **before**
  the file is written — a failed check aborts with the file untouched.

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
probe per backend; offline backends drop out. Gated backends are hidden from callers
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
- **PyYAML** — config
- **Python 3.11**

## License

MIT
