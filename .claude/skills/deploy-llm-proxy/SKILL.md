---
name: deploy-llm-proxy
description: "Ship llm-proxy to production on gw.brandao. Use when: deploying/releasing llm-proxy, 'push this live', bumping the llm-proxy image tag, rolling out a code change to the proxy, restarting the production llm-proxy container, or rolling back a bad llm-proxy build."
---

# Deploy llm-proxy

Four hops: **push code → GHA builds an image → bump the tag in the deploy repo →
restart the container on gw.** Two separate git repos on two separate forges are
involved, and the image tag is a build counter you must *read*, never guess.

## The moving parts

| What | Where | Forge / notes |
|---|---|---|
| App source (this repo) | `/dados/techmago/git/luis/llm-proxy` | GitHub — `luisbrandao/llm-proxy` |
| Deploy repo (local clone) | `/dados/techmago/git/luis/techsytes-docker` | Gitea — `git.techsytes.com:techmago/techsytes-docker` |
| Production compose | `<deploy repo>/monitoring/docker-compose.yml` | service `llm-proxy` |
| Production proxy config | `<deploy repo>/monitoring/llmproxy.yaml` | bind-mounted **rw** at `/app/config.yaml` |
| Production host | `gw.brandao` — `ssh techmago@gw.brandao` | deploy repo lives at `~/docker`, has git credentials |
| Image | `ghcr.io/luisbrandao/llm-proxy` | tags: `latest` **and** `master-<run_number>` |

> **This repo was renamed** (`deepseek-prometheus` → `llm-proxy`). The local
> `origin` URL still says the old name and works only via GitHub's redirect. The
> image name comes from `github.repository`, which resolves to the **new** name —
> so the image is `ghcr.io/luisbrandao/llm-proxy`, not `.../deepseek-prometheus`.
> `gh` follows the rename transparently; don't "fix" the remote as part of a deploy.

> **Sandbox note:** run every `ssh` and `docker` step **outside the sandbox**. The
> sandbox hides `~/.ssh` (breaks SSH to gw) and strips supplementary groups
> (breaks the local docker socket).

## 0. Preflight

- Is there anything to deploy? `git -C <repo> status` and `git log origin/master..HEAD`.
- **A docs-only change builds nothing.** `.github/workflows/docker-build.yml` has
  `paths-ignore: ['**.md']`, so a push touching only `.md` files produces no run,
  no new tag, and nothing to deploy. Say so and stop rather than hunting for a
  tag that will never exist.
- Deploying only *config* (`monitoring/llmproxy.yaml`)? Skip steps 1–2 entirely.
  The proxy polls its config file and hot-reloads it (`CONFIG_RELOAD_INTERVAL`),
  so a config-only change needs a commit+push and the file in place — **no image
  bump and no container restart**.

## 1. Commit and push the app

```bash
cd /dados/techmago/git/luis/llm-proxy
git add -A && git commit -m "<what changed>"
git push
```

Pushing to `master` is what triggers the build — there is no manual dispatch.

## 2. Wait for the build, then read the real tag

The tag is `master-<run_number>`, where `run_number` is GitHub's per-workflow
counter. **Resolve it from the completed run. Never derive it by adding 1 to
whatever the compose file currently says** — those two numbers drift:

- `run_number` counts *runs*, not commits. Runs that never produced an image
  (a failed build) still consumed a number.
- A **re-run** of an existing run does *not* increment it.
- Docs-only pushes produce no run at all.
- Production is frequently a build or more behind, because a previous deploy
  stopped after the build. (This has already happened: the compose pinned
  `master-35` while `master-36` was built and sitting in the registry.)

```bash
# newest run for this workflow — grab databaseId + number
gh run list --workflow "Docker Build" --limit 1 \
  --json databaseId,number,status,conclusion,headSha

# block until it finishes, non-zero exit if it failed
gh run watch <databaseId> --exit-status
```

A run appears a few seconds after the push and takes **~20–35s** to finish. If
`gh run list` still shows the previous run, wait and re-poll — do not assume it
was skipped. Confirm `headSha` matches the commit you just pushed.

The tag to deploy is `master-<number>` from that run. Verify it really landed in
the registry before editing anything (the package is private, so this needs a
token):

```bash
T=$(curl -s -u "luisbrandao:$(gh auth token)" \
  "https://ghcr.io/token?scope=repository:luisbrandao/llm-proxy:pull&service=ghcr.io" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
curl -s -H "Authorization: Bearer $T" \
  "https://ghcr.io/v2/luisbrandao/llm-proxy/tags/list?n=1000" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['tags'][-6:])"
```

Unlike the llama-swap fleet (thousands of tags, needs paging), this package has a
few dozen tags and one un-paged request returns all of them.

Deploy the **explicit `master-N` tag, not `latest`.** The compose file is the
record of what production runs; `latest` makes that unreadable and makes
rollback guesswork.

## 3. Bump the tag in the deploy repo

```bash
cd /dados/techmago/git/luis/techsytes-docker
# one line, in the `llm-proxy:` service block of monitoring/docker-compose.yml
#   image: ghcr.io/luisbrandao/llm-proxy:master-<N>
git commit -am "llm-proxy: master-<N>"
git push
```

Push this **before** touching gw — step 4 pulls it.

## 4. Deploy on gw

```bash
ssh techmago@gw.brandao
cd ~/docker
git commit -am "Server Bump" ; git pull ; git push
cd ~/docker/monitoring ; docker compose up -d
```

> **Order matters: `commit` → `pull` → `push`.** Do not reorder. gw's checkout is
> reliably dirty, and for a specific reason: `monitoring/llmproxy.yaml` is
> bind-mounted read-write, and the proxy's own web console **writes routing
> priority edits back into it** (`app/configwrite.py`). Any priority reordered
> from `/ui/` shows up as an uncommitted change on gw. Committing first preserves
> those live edits instead of letting `git pull` trip over them.

`docker compose up -d` recreates only the services whose definition changed, so
the rest of the monitoring stack (Prometheus, Loki, dcgm-exporter, …) is left
alone.

**If `git pull` conflicts on `llmproxy.yaml`:** you edited the config locally
*and* someone reordered priorities in the production console. gw's version is
the live truth — prefer it (`git checkout --ours`/keep gw's hunks), re-apply your
intended edit on top, and push the merge.

## 5. Verify

```bash
ssh techmago@gw.brandao \
  "docker ps --filter name=llm-proxy --format '{{.Image}}\t{{.Status}}' && \
   curl -s localhost:8000/health"
```

Expect the new `master-N` in the image column, a `Up …` status, and a health body
that **reports the same tag**:

```json
{"status":"ok","version":"master-52","revision":"1e5837a3…","release":true}
```

Those two must agree. `docker ps` shows what the compose file *asked* for;
`version` is what the process actually running is built from (baked in at build
time — see `app/version.py`). They diverge when a container wasn't really
recreated, which is otherwise invisible. The one-liner worth running:

```bash
ssh techmago@gw.brandao \
  'docker ps --filter name=llm-proxy --format "{{.Image}}" && curl -s localhost:8000/health'
```

Then load `http://gw.brandao:8000/ui/` and check the console tabs render — the
header carries the same build chip, so a stale image is obvious at a glance.

Note that metric counters reset on restart, along with slot/queue/health state
and the log ring buffer — all of it is in-process. That reset is fine:
Prometheus compensates for it in `rate()`/`increase()`, and long-range totals
come from the `:increase5m` recording rules. In-flight requests are dropped on
recreate — a restart mid-generation cuts those clients off.

## Rollback

Every previous image is still in the registry, and the compose file records
exactly what was running:

```bash
cd /dados/techmago/git/luis/techsytes-docker
git log -p --follow -- monitoring/docker-compose.yml | grep -m5 'llm-proxy:master'
# set the image back to the last known-good master-N, then:
git commit -am "llm-proxy: rollback to master-<N>"; git push
ssh techmago@gw.brandao 'cd ~/docker && git commit -am "Server Bump"; git pull; git push; cd monitoring && docker compose up -d'
```

## Checklist

- [ ] App committed and pushed to `master` (and the change isn't docs-only)
- [ ] Read `master-N` from the **completed** GHA run — not guessed, not `latest`
- [ ] `headSha` of that run matches the pushed commit
- [ ] Tag confirmed present in GHCR
- [ ] `image:` bumped in `monitoring/docker-compose.yml`, committed **and pushed**
- [ ] On gw: `commit` → `pull` → `push`, in that order
- [ ] `docker compose up -d` from `~/docker/monitoring`
- [ ] `docker ps` shows the new tag; `/health` and `/ui/` respond
- [ ] `/health`'s `version` **equals** the tag in `docker ps` — otherwise the
      container is still running an older build
