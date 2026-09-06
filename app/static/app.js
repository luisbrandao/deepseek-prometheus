"use strict";

/* LLM Proxy console — vanilla JS, no build step.
 * Talks to the proxy's own endpoints: GET/POST /logging, GET /v1/models,
 * and the auth-gated GET /admin/logs, GET /admin/upstream-models,
 * GET/POST /admin/routing. The bearer key (if any) is kept in localStorage and
 * sent as Authorization: Bearer <key> on every call. */

const KEY_STORE = "llmproxy.key";
const LOG_POLL_MS = 1500;
const FLIGHT_POLL_MS = 1000;
const MAX_LOG_LINES = 3000;

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ── Auth + fetch ──────────────────────────────────────────── */
const getKey = () => localStorage.getItem(KEY_STORE) || "";
const setKey = (k) => localStorage.setItem(KEY_STORE, k);

function authHeaders(extra = {}) {
  const k = getKey();
  return k ? { ...extra, Authorization: "Bearer " + k } : { ...extra };
}

function setConn(ok) {
  const dot = $("#conn-dot");
  dot.classList.toggle("ok", ok === true);
  dot.classList.toggle("bad", ok === false);
  dot.title = ok == null ? "unknown" : ok ? "connected" : "error / unauthorized";
}

async function api(path, opts = {}) {
  try {
    const res = await fetch(path, { ...opts, headers: authHeaders(opts.headers) });
    setConn(res.ok);
    return res;
  } catch (e) {
    setConn(false);
    throw e;
  }
}

let toastTimer = null;
function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = "toast"; }, 3000);
}

/* ── Tabs ──────────────────────────────────────────────────── */
function activateTab(name) {
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "logging") startLogPolling(); else stopLogPolling();
  if (name === "inflight") startFlightPolling(); else stopFlightPolling();
  if (name === "models") loadCatalog();
  if (name === "routing") loadRouting();
  if (name === "config") loadConfig();
}

/* ── Logging: flags ────────────────────────────────────────── */
async function loadLogFlags() {
  try {
    const res = await api("/logging");
    if (!res.ok) return;
    const d = await res.json();
    $("#log-input").checked = !!d.log_input;
    $("#log-output").checked = !!d.log_output;
  } catch { /* connection dot already reflects it */ }
}

async function setLogFlag(key, value) {
  const res = await api("/logging", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ [key]: value }),
  });
  if (res.status === 403) { toast("Unauthorized — set a valid bearer key", "bad"); await loadLogFlags(); return; }
  if (!res.ok) { toast("Failed to update " + key, "bad"); await loadLogFlags(); return; }
  const d = await res.json();
  $("#log-input").checked = !!d.log_input;
  $("#log-output").checked = !!d.log_output;
  toast(key + " = " + value);
}

/* ── Logging: live tail ────────────────────────────────────── */
let logTimer = null;
let lastSeq = 0;
let logLineCount = 0;
let logBlocked = false;

function startLogPolling() {
  if (logTimer) return;
  pollLogs();
  logTimer = setInterval(pollLogs, LOG_POLL_MS);
}
function stopLogPolling() {
  clearInterval(logTimer);
  logTimer = null;
}

async function pollLogs() {
  if ($("#log-pause").checked) return;
  const level = $("#log-level").value;
  let res;
  try { res = await api(`/admin/logs?since=${lastSeq}&level=${encodeURIComponent(level)}`); }
  catch { return; }
  if (res.status === 403) {
    if (!logBlocked) { renderLogNotice("Unauthorized — enter a valid bearer key above to view logs."); logBlocked = true; }
    return;
  }
  if (!res.ok) return;
  if (logBlocked) { logBlocked = false; $("#log-pane").innerHTML = ""; }
  const data = await res.json();
  lastSeq = data.last_seq;
  if (data.entries && data.entries.length) appendLogs(data.entries);
}

function renderLogNotice(text) {
  $("#log-pane").innerHTML = `<div class="notice">${escapeHtml(text)}</div>`;
}

const logQuery = () => ($("#log-search").value || "").toLowerCase().trim();

// Each line keeps its original (un-highlighted) text so the grep box can
// re-render highlights from scratch on every keystroke without losing data.
const logMeta = new WeakMap();

// Append `text` to `parent`, wrapping case-insensitive matches of `q` in <mark>.
function appendHighlighted(parent, text, q) {
  if (!q) { parent.appendChild(document.createTextNode(text)); return; }
  const lower = text.toLowerCase();
  let i = 0, idx;
  while ((idx = lower.indexOf(q, i)) !== -1) {
    if (idx > i) parent.appendChild(document.createTextNode(text.slice(i, idx)));
    parent.appendChild(el("mark", "hl", text.slice(idx, idx + q.length)));
    i = idx + q.length;
  }
  if (i < text.length) parent.appendChild(document.createTextNode(text.slice(i)));
}

function renderHighlighted(span, text, q) {
  span.textContent = "";
  appendHighlighted(span, text, q);
}

// Re-render a matching line's logger + message with the query highlighted (plain
// when q is empty). The full message is highlighted even while collapsed, so
// expanding a hit found in a request/response body reveals the mark.
function highlightLine(line, q) {
  const meta = logMeta.get(line);
  if (!meta) return;
  const lg = line.querySelector(".lg");
  if (lg) renderHighlighted(lg, meta.logger, q);
  const lm = line.querySelector(".lm");
  if (lm) renderHighlighted(lm, meta.msg, q);
  const prev = line.querySelector(".lm-preview");
  if (prev) {
    const more = prev.querySelector(".more");
    prev.textContent = "";
    appendHighlighted(prev, meta.firstLine, q);
    if (more) prev.appendChild(more);
  }
}

// Build one log line. Multi-line messages (the curl-style Request/Response dumps
// from LOG_INPUT/LOG_OUTPUT) become collapsible: a ▶/▼ toggle, a one-line preview
// when collapsed, the full pre-wrapped text when expanded. Single-line entries
// render as-is. The full text is stashed on dataset.search so the grep box can
// match even content hidden inside a collapsed entry.
function makeLogLine(e) {
  const level = e.level || "INFO";
  const msg = e.msg || "";
  const nl = msg.indexOf("\n");
  const multiline = nl !== -1;
  const line = el("div", "logline lvl-" + level.toLowerCase() + (multiline ? " multiline collapsed" : ""));
  // Grep matches logger + message (level has its own dropdown), so what you can
  // search is exactly what gets highlighted.
  line.dataset.search = ((e.logger || "") + " " + msg).toLowerCase();
  logMeta.set(line, { logger: e.logger || "", msg, firstLine: multiline ? msg.slice(0, nl) : msg });

  let tog = null;
  if (multiline) {
    tog = el("button", "ltog", "▶");
    tog.title = "expand / collapse";
    tog.addEventListener("click", () => {
      const collapsed = line.classList.toggle("collapsed");
      tog.textContent = collapsed ? "▶" : "▼";
    });
    line.appendChild(tog);
  }
  line.append(
    el("span", "lt", (e.ts || "").replace("T", " ")),
    el("span", "lv", level),
    el("span", "lg", e.logger || "")
  );
  if (multiline) {
    const preview = el("span", "lm-preview");
    preview.appendChild(document.createTextNode(msg.slice(0, nl) || "(multi-line)"));
    preview.appendChild(el("span", "more", `⋯ +${msg.split("\n").length - 1} lines`));
    preview.addEventListener("click", () => { line.classList.remove("collapsed"); if (tog) tog.textContent = "▼"; });
    line.appendChild(preview);
  }
  line.appendChild(el("span", "lm", msg));
  return line;
}

function appendLogs(entries) {
  const pane = $("#log-pane");
  const atBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 40;
  const q = logQuery();
  const frag = document.createDocumentFragment();
  for (const e of entries) {
    const line = makeLogLine(e);
    if (q) {
      if (line.dataset.search.includes(q)) highlightLine(line, q);
      else line.style.display = "none";
    }
    frag.appendChild(line);
  }
  pane.appendChild(frag);
  logLineCount += entries.length;
  while (pane.childElementCount > MAX_LOG_LINES) pane.removeChild(pane.firstChild);
  $("#log-count").textContent = logLineCount.toLocaleString() + " lines";
  if ($("#log-autoscroll").checked && atBottom) pane.scrollTop = pane.scrollHeight;
}

// Re-evaluate every buffered line against the grep box (skips the notice div).
function applyLogFilter() {
  const q = logQuery();
  for (const ln of $("#log-pane").children) {
    if (!ln.dataset || ln.dataset.search === undefined) continue;
    const match = !q || ln.dataset.search.includes(q);
    ln.style.display = match ? "" : "none";
    if (match) highlightLine(ln, q);
  }
}

function resetLogTail() {
  lastSeq = 0;
  logLineCount = 0;
  logBlocked = false;
  $("#log-pane").innerHTML = "";
  $("#log-count").textContent = "";
}

/* ── Models ────────────────────────────────────────────────── */
async function loadCatalog() {
  const wrap = $("#catalog");
  let res;
  try { res = await api("/v1/models"); }
  catch { wrap.innerHTML = '<div class="notice">Cannot reach the proxy.</div>'; return; }
  if (!res.ok) { wrap.innerHTML = '<div class="notice">Failed to load catalog.</div>'; return; }
  const data = await res.json();
  const items = (data.data || []).slice().sort((a, b) => a.id.localeCompare(b.id));
  $("#models-count").textContent = items.length + " models";
  wrap.innerHTML = "";
  if (!items.length) { wrap.innerHTML = '<div class="notice">No models listed.</div>'; return; }
  for (const m of items) {
    const row = el("div", "mrow");
    row.append(el("span", "mid", m.id), el("span", "mowner", m.owned_by || ""));
    wrap.appendChild(row);
  }
}

async function probeUpstreams() {
  const btn = $("#models-probe");
  const wrap = $("#upstreams");
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = "Probing…";
  try {
    const res = await api("/admin/upstream-models");
    if (res.status === 403) { wrap.innerHTML = '<div class="notice">Unauthorized — set a valid bearer key.</div>'; return; }
    if (!res.ok) { wrap.innerHTML = '<div class="notice">Probe failed.</div>'; return; }
    const data = await res.json();
    wrap.classList.remove("hint");
    wrap.innerHTML = "";
    for (const p of data.providers || []) {
      const card = el("div", "ucard" + (p.ok ? "" : " err"));
      const head = el("div", "uhead");
      head.appendChild(el("span", "uname", p.provider));
      head.appendChild(p.ok ? el("span", "ucount", p.ids.length + " ids") : el("span", "ubad", "unreachable"));
      card.appendChild(head);
      if (p.ok) {
        const list = el("div", "ulist");
        if (!p.ids.length) list.appendChild(el("span", "muted", "(empty)"));
        for (const id of p.ids) list.appendChild(el("span", "uid", id));
        card.appendChild(list);
      } else {
        card.appendChild(el("div", "uerr", p.error || "error"));
      }
      wrap.appendChild(card);
    }
  } catch {
    wrap.innerHTML = '<div class="notice">Probe failed.</div>';
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

/* ── In-flight ─────────────────────────────────────────────── */
/* Polls /admin/inflight and renders a rolling feed: live requests pinned on top
 * (newest first), then the finished history below. Rows are keyed by request id
 * and patched in place — a rebuild every second would fight text selection, and
 * with a few hundred history rows it would also be wasteful. A row only
 * re-renders when its data actually changed, so finished rows are drawn once and
 * then left alone; only the live ones tick. */
let flightTimer = null;
const flightRows = new Map(); // id -> row element
const killSent = new Set();
// Rows whose request/response pane is open. Kept by id so the pane survives the
// 1s re-render, and so reopening doesn't refetch what we already have.
const bodyOpen = new Map();   // id -> {loading, data, error}
// Built panes, cached by id: a live row re-renders every second and rebuilding an
// unchanged pane each time would throw away the reader's scroll position.
const bodyPanes = new Map();  // id -> {el, sig}
let flightDivider = null;

function startFlightPolling() {
  if (flightTimer) return;
  pollFlight();
  flightTimer = setInterval(pollFlight, FLIGHT_POLL_MS);
}
function stopFlightPolling() {
  clearInterval(flightTimer);
  flightTimer = null;
}

// Whole seconds under a minute, then m:ss — an in-flight request is short-lived,
// so precision matters more than the H:MM:SS the log lines use.
function dur(sec) {
  if (sec == null) return "—";
  if (sec < 60) return sec.toFixed(1) + "s";
  const m = Math.floor(sec / 60);
  return m + "m" + String(Math.floor(sec % 60)).padStart(2, "0") + "s";
}

function bytes(n) {
  if (n == null) return "—";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KiB";
  return (n / 1024 / 1024).toFixed(1) + " MiB";
}

async function pollFlight() {
  if (!$("#fl-live").checked && flightTimer) return;
  let res;
  try { res = await api("/admin/inflight"); }
  catch { return; }
  if (res.status === 403) {
    flightRows.clear();
    $("#fl-body").innerHTML = '<div class="notice">Unauthorized — set a valid bearer key to view in-flight requests.</div>';
    $("#fl-summary").textContent = "";
    $("#fl-slots").textContent = "";
    return;
  }
  if (!res.ok) return;
  const data = await res.json();
  renderFlight(data);
  refreshOpenBodies(data);
}

// A pane open on a *live* row is watching a reply still being written, so re-pull
// it (every other tick — 16 KiB per open pane per second would be wasteful).
let bodyTick = 0;
function refreshOpenBodies(data) {
  if (!bodyOpen.size || ++bodyTick % 2) return;
  const live = new Set((data.requests || []).filter((r) => r.live).map((r) => r.id));
  for (const id of bodyOpen.keys()) {
    const st = bodyOpen.get(id);
    if (!live.has(id) || st.loading) continue;
    api(`/admin/inflight/${id}/body`)
      .then((res) => (res.ok ? res.json() : null))
      .then((d) => { if (d && bodyOpen.has(id)) { bodyOpen.set(id, { data: d }); redrawRow(id); } })
      .catch(() => {});
  }
}

function renderFlight(data) {
  const running = data.running || 0;
  const queued = data.queued || 0;
  const sum = $("#fl-summary");
  sum.innerHTML = "";
  sum.append(
    el("span", "flstat run", running + " running"),
    el("span", "flstat queue" + (queued ? " hot" : ""), queued + " queued"),
    el("span", "flstat past", (data.history || 0) + " done"),
  );
  sum.lastChild.title = `rolling history, newest first — keeps the last ${data.history_limit} and is lost on restart`;

  // Per-provider occupancy: the capacity that decides whether the queue drains.
  const slotsWrap = $("#fl-slots");
  slotsWrap.innerHTML = "";
  for (const p of data.providers || []) {
    const full = p.slots != null && p.in_use >= p.slots;
    const chip = el("span", "flslot" + (full ? " full" : "") + (p.is_down ? " down" : ""));
    chip.append(el("b", null, p.name), el("span", null, `${p.in_use}/${p.slots == null ? "∞" : p.slots}`));
    if (p.resident) chip.appendChild(el("i", "flres", p.resident));
    chip.title = [
      p.is_down ? "marked down" : full ? "no free slot — new requests queue here" : "",
      p.resident ? `last ran ${p.resident} — queued requests for it skip ahead` : "",
    ].filter(Boolean).join(" · ");
    slotsWrap.appendChild(chip);
  }

  const body = $("#fl-body");
  const reqs = data.requests || [];
  if (!reqs.length) {
    flightRows.clear();
    killSent.clear();
    bodyOpen.clear();
    bodyPanes.clear();
    flightDivider = null;
    body.innerHTML = '<div class="notice">Nothing in flight yet — requests appear here as they arrive.</div>';
    return;
  }

  if (!body.querySelector(".fllist")) {
    body.innerHTML = "";
    body.appendChild(el("div", "fllist"));
  }
  const list = body.querySelector(".fllist");

  // The API already orders these: live newest-first, then the frozen history.
  const firstDone = reqs.findIndex((r) => !r.live);
  const items = [];
  reqs.forEach((r, i) => {
    // A divider marks where "happening now" ends, but only when both halves exist.
    if (i === firstDone && firstDone > 0) items.push(null);
    items.push(r);
  });

  const seen = new Set();
  items.forEach((r, i) => {
    let node;
    if (r === null) {
      if (!flightDivider) flightDivider = el("div", "fldiv", "recent");
      node = flightDivider;
    } else {
      seen.add(r.id);
      node = flightRows.get(r.id);
      if (!node) {
        node = el("div", "flrow");
        flightRows.set(r.id, node);
      }
      fillFlightRow(node, r);
    }
    // Keep DOM order in sync with the ordered list.
    if (list.children[i] !== node) list.insertBefore(node, list.children[i] || null);
  });
  for (const [id, row] of flightRows) {
    if (!seen.has(id)) {
      row.remove();
      flightRows.delete(id);
      killSent.delete(id);
      bodyOpen.delete(id);
      bodyPanes.delete(id);
    }
  }
  if (flightDivider && !items.includes(null)) { flightDivider.remove(); flightDivider = null; }
}

// Everything a row displays, in one string. Unchanged signature => skip the
// re-render, which is what keeps a long history cheap to poll over.
function flightSig(r) {
  return [
    r.state, r.status, r.age, r.queued_for, r.running_for, r.duration, r.chunks,
    r.provider, r.attempt, r.skipped, JSON.stringify(r.trimmed || null),
    r.in_tokens, r.out_tokens, r.estimated, r.tps,
    r.client_host, r.has_body, killSent.has(r.id),
    JSON.stringify(bodyOpen.get(r.id) || null),
  ].join("\u0001");
}

function fillFlightRow(row, r) {
  const sig = flightSig(r);
  if (row._sig === sig) { row._r = r; return; }
  row._sig = sig;
  // Stash the latest snapshot so the disarm timer can re-render this row without
  // waiting for (or forcing) another poll.
  row._r = r;

  row.className = "flrow st-" + r.state;
  row.innerHTML = "";

  const idCell = el("span", "flid", "#" + r.id);
  idCell.title = "request id · arrived " + r.arrived_at;
  row.appendChild(idCell);

  const state = el("span", "flstate", r.state);
  if (r.status != null) state.title = "HTTP " + r.status;
  row.appendChild(state);

  const main = el("div", "flmain");
  const line1 = el("div", "flline");
  line1.appendChild(el("span", "flmodel", r.model || r.path));
  if (r.stream) line1.appendChild(el("span", "fltag", "stream"));
  if (r.op) line1.appendChild(el("span", "fltag", r.op));
  if (r.attempt > 1) {
    const a = el("span", "fltag warn", "attempt " + r.attempt);
    a.title = "failed over from an earlier backend";
    line1.appendChild(a);
  }
  if (r.skipped) {
    const sk = el("span", "fltag warn", "passed over " + r.skipped + "\u00d7");
    sk.title = "the backend already had another request's model loaded, so that one "
      + "went first — this is capped, see routing.affinity_max_skips";
    line1.appendChild(sk);
  }
  if (r.trimmed) {
    // The context guardrail shrank this request (see trim: in the config).
    const t = r.trimmed;
    const label = t.dropped
      ? "trimmed " + t.dropped + " msg" + (t.dropped === 1 ? "" : "s")
      : "trimmed " + t.capped + " tool result" + (t.capped === 1 ? "" : "s");
    const tr = el("span", "fltag warn", label);
    tr.title = "conversation exceeded the num_ctx it declared: ~" + t.before
      + " tokens vs a budget of " + t.budget + " — dropped " + t.dropped
      + " oldest message(s), cut " + t.capped + " old tool result(s) to an excerpt, forwarded ~"
      + t.after + " tokens" + (t.after > t.budget ? " (still over: the newest turn alone does not fit)" : "");
    line1.appendChild(tr);
  }
  if (r.status != null && r.status >= 400) {
    line1.appendChild(el("span", "fltag bad", "HTTP " + r.status));
  }
  main.appendChild(line1);

  const line2 = el("div", "flline sub");
  if (r.state === "queued") {
    // What it is waiting on is the whole point of the queued view.
    line2.appendChild(el("span", "flwait", "waiting for: " + (r.candidates || []).join(", ")));
  } else if (!r.provider) {
    // Finished without ever being dispatched — killed in the queue, or rejected
    // before a backend was picked. Naming the backends it wanted is the useful bit.
    const c = (r.candidates || []).join(", ");
    line2.appendChild(el("span", "flwait", "never reached a backend" + (c ? " · wanted " + c : "")));
  } else {
    line2.appendChild(el("span", "flprov", r.provider));
    if (r.native_model && r.native_model !== r.model) {
      line2.appendChild(el("span", "flnative", r.native_model));
    }
  }
  const who = r.client_host || r.client_ip;
  line2.appendChild(el("span", "flwho", (r.svc ? r.svc + " · " : "") + who));
  main.appendChild(line2);
  row.appendChild(main);

  // Every row in a group gets the same cells — a stream-only column would make
  // the numbers jump left and right down the list.
  const times = el("div", "fltimes");
  // Live out-tokens are our own count of generation steps — the upstream only
  // reports usage in its final chunk — so they carry a ~ until the real number
  // lands. Input tokens simply aren't knowable before then.
  const tokIn = r.in_tokens == null ? (r.live ? "0" : "—") : String(r.in_tokens);
  const tokOut = r.out_tokens == null
    ? (r.live ? "0" : "—")
    : (r.estimated ? "~" + r.out_tokens : String(r.out_tokens));
  // Tokens per second over the upstream exchange — the same figure the request
  // log emits as speed_tps (in_tps for embeddings/rerankers, which generate
  // nothing). Queue wait is excluded; prompt processing is not, because the
  // clock starts when the request is sent, so a fresh stream reads 0.0 until the
  // first token lands. On a live row it is the average so far, over the
  // estimated count, hence the ~. Two decimals, like the log line, so the two
  // read as the same number instead of one that looks rounded.
  const tps = r.tps == null
    ? (r.state === "running" ? "0.00" : "—")
    : (r.estimated ? "~" : "") + r.tps.toFixed(2);
  const tpsCell = timeCell("tok/s", tps);
  tpsCell.title = (r.op ? "input" : "output") + " tokens per second of upstream time"
    + " — queue wait excluded" + (r.live ? "; average so far" : "")
    + " (the request log's " + (r.op ? "in_tps" : "speed_tps") + ")";
  if (r.live) {
    times.appendChild(timeCell("age", dur(r.age)));
    times.appendChild(timeCell("queued", dur(r.queued_for)));
    times.appendChild(timeCell("running", dur(r.running_for)));
    times.appendChild(timeCell("tok in", tokIn));
    times.appendChild(timeCell("tok out", tokOut));
    times.appendChild(tpsCell);
  } else {
    times.appendChild(timeCell("took", dur(r.duration)));
    times.appendChild(timeCell("queued", dur(r.queued_for)));
    times.appendChild(timeCell("tok in", tokIn));
    times.appendChild(timeCell("tok out", tokOut));
    times.appendChild(tpsCell);
  }
  times.appendChild(timeCell("chunks", r.stream ? String(r.chunks == null ? "—" : r.chunks) : "—"));
  times.appendChild(timeCell("body", bytes(r.req_bytes)));
  row.appendChild(times);

  if (r.has_body) {
    const open = bodyOpen.has(r.id);
    const peek = el("button", "btn peek" + (open ? " on" : ""), open ? "Hide" : "Body");
    peek.title = "show the prompt this request sent and the reply so far";
    peek.onclick = () => toggleBody(r.id);
    row.appendChild(peek);
  } else {
    row.appendChild(el("span", "flnopeek"));
  }

  // Only a live request can be killed; a finished row keeps the column aligned.
  if (!r.live) {
    row.appendChild(el("span", "flnokill"));
    if (bodyOpen.has(r.id)) row.appendChild(bodyPane(r.id));
    return;
  }

  const sent = killSent.has(r.id) || r.cancelled;
  const kill = el("button", "btn kill", sent ? "killing…" : "Kill");
  kill.disabled = sent;
  kill.title = sent
    ? "cancellation sent — the row turns into a 'cancelled' history entry once it unwinds"
    : "cancel this request — the client gets a 503";
  kill.onclick = () => killFlight(r.id);
  row.appendChild(kill);
  if (bodyOpen.has(r.id)) row.appendChild(bodyPane(r.id));
}

/* ── In-flight: the request/response pane ──────────────────── */
async function toggleBody(id) {
  if (bodyOpen.has(id)) {
    bodyOpen.delete(id);
    bodyPanes.delete(id);
    redrawRow(id);
    return;
  }
  bodyOpen.set(id, { loading: true });
  redrawRow(id);
  try {
    const res = await api(`/admin/inflight/${id}/body`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      bodyOpen.set(id, { error: err.error || `HTTP ${res.status}` });
    } else {
      bodyOpen.set(id, { data: await res.json() });
    }
  } catch {
    bodyOpen.set(id, { error: "cannot reach the proxy" });
  }
  redrawRow(id);
}

function redrawRow(id) {
  const row = flightRows.get(id);
  if (row && row._r) { row._sig = null; fillFlightRow(row, row._r); }
}

// Pretty-print when it's JSON, leave it alone when it isn't.
function maybeJson(text) {
  const t = (text || "").trim();
  if (!t.startsWith("{") && !t.startsWith("[")) return text;
  try { return JSON.stringify(JSON.parse(t), null, 2); } catch { return text; }
}

function bodyPane(id) {
  const st = bodyOpen.get(id) || {};
  const sig = JSON.stringify(st);
  const cached = bodyPanes.get(id);
  if (cached && cached.sig === sig) return cached.el;
  const pane = buildBodyPane(st);
  bodyPanes.set(id, { el: pane, sig });
  return pane;
}

function buildBodyPane(st) {
  const pane = el("div", "flbodypane");
  if (st.loading) { pane.appendChild(el("div", "muted", "loading…")); return pane; }
  if (st.error) { pane.appendChild(el("div", "uerr", st.error)); return pane; }
  const d = st.data || {};
  const section = (label, text, truncated) => {
    if (!text) return;
    const h = el("div", "flbodyhead", label);
    if (truncated) {
      const t = el("span", "fltag warn", "truncated at " + bytes(d.limit));
      t.title = "raise INFLIGHT_BODY_LIMIT to keep more";
      h.appendChild(t);
    }
    const pre = el("pre", "flbodytext", text);
    pane.append(h, pre);
  };
  section("request", maybeJson(d.request), d.request_truncated);
  section("reasoning", d.reasoning, d.reasoning_truncated);
  section("response", maybeJson(d.response), d.response_truncated);
  if (!d.request && !d.response && !d.reasoning) {
    pane.appendChild(el("div", "muted", "nothing captured for this request"));
  }
  return pane;
}

async function killFlight(id) {
  killSent.add(id);
  const row = flightRows.get(id);
  if (row && row._r) fillFlightRow(row, row._r);
  let res;
  try { res = await api(`/admin/inflight/${id}/cancel`, { method: "POST" }); }
  catch { killSent.delete(id); toast("Kill failed — cannot reach the proxy", "bad"); return; }
  if (res.status === 404) {
    // Benign race: it completed between the last poll and the click.
    killSent.delete(id);
    toast(`Request #${id} already finished`);
  } else if (!res.ok) {
    killSent.delete(id);
    const err = await res.json().catch(() => ({}));
    toast(err.error || `Kill failed (${res.status})`, "bad");
  } else {
    toast(`Killed request #${id}`);
  }
  pollFlight();
}

function timeCell(label, value) {
  const c = el("div", "flcell");
  c.append(el("span", "fllabel", label), el("span", "flvalue", value));
  return c;
}


/* ── Config: edit the config file from the browser ──────────── */
/* Every save is a PUT that rewrites config.yaml (comments preserved) and is
 * applied to the live process before the response returns — so the response
 * carries the fresh snapshot and we re-render from that, never from what we
 * hoped we wrote. Sections save independently: these are file writes, so they
 * are explicit rather than as-you-type. */
let cfg = null;                       // last snapshot from the server
const cfgCatalog = new Map();         // provider -> {loading, ids, error}
const cfgFilter = new Map();          // provider -> search text, survives re-renders
const cfgOpenHelp = new Set();        // section keys whose (?) panel is open
let cfgNewModelName = "";

/* Inline help. Kept on the page rather than in the README because the two
 * vocabularies (canonical vs native) are the thing everyone forgets, and the
 * moment you need reminding is while looking at a list of native ids. */
function code(text) { return el("code", null, text); }

function helpLine(...parts) {
  const line = el("p", "cfghelpline");
  for (const part of parts) line.append(typeof part === "string" ? document.createTextNode(part) : part);
  return line;
}

// A section heading with a (?) toggle. `build()` is only called when opened.
function cfgHead(title, key, build) {
  const wrap = el("div", "cfgheadwrap");
  const h = el("h2", null, title);
  const btn = el("button", "cfghelpbtn", "?");
  btn.title = "what is this?";
  btn.onclick = () => {
    cfgOpenHelp.has(key) ? cfgOpenHelp.delete(key) : cfgOpenHelp.add(key);
    renderConfig();
  };
  if (cfgOpenHelp.has(key)) btn.classList.add("on");
  h.appendChild(btn);
  wrap.appendChild(h);
  if (cfgOpenHelp.has(key)) {
    const panel = el("div", "cfghelp");
    for (const node of build()) panel.appendChild(node);
    wrap.appendChild(panel);
  }
  return wrap;
}

function helpNative() {
  const flow = el("div", "cfgflow");
  flow.append(
    el("div", "cfgflowrow"), // filled below
  );
  flow.innerHTML = "";
  const step = (label, value) => {
    const d = el("div", "cfgflowstep");
    d.append(el("span", "cfgflowlabel", label), el("code", null, value));
    return d;
  };
  flow.append(
    step("backend reports", "zai-org/glm-5.2"),
    el("span", "cfgflowarrow", "→"),
    step("model_map renames it", "glm-5.2"),
    el("span", "cfgflowarrow", "→"),
    step("client asks for", "glm-5.2"),
    el("span", "cfgflowarrow", "→"),
    step("proxy sends", "zai-org/glm-5.2"),
  );
  return [
    helpLine("Every model has two names, and this page shows both."),
    helpLine(
      "A ", el("b", null, "native id"), " is what one backend calls the model on the wire — ",
      code("zai-org/glm-5.2"), " on nanoGPT, ", code("z-ai/glm-5.2"), " on openRouter. ",
      "The checkboxes below are native ids, read straight from the backend's ",
      code("/v1/models"), ", which is why they carry vendor prefixes and look messy.",
    ),
    helpLine(
      "A ", el("b", null, "canonical name"), " is what your clients send — ", code("glm-5.2"),
      " — and stays the same whichever backend serves it.",
    ),
    flow,
    helpLine(
      el("b", null, "The part that trips everyone up:"), " if a native id has ",
      el("b", null, "no"), " ", code("model_map"), " entry and no group in front of it, there is ",
      "no translation — the raw id ", el("i", null, "is"), " the public name, and a client has to send ",
      code("moonshotai/kimi-k3"), " verbatim. Pinning a model does not give it a clean name.",
    ),
    helpLine(
      "Two ways to give one a clean name: a ", el("b", null, "group"), " (below, editable here) ",
      "that fronts it — which also hides the raw id from ", code("/v1/models"), " — or a ",
      code("model_map"), " entry in the config file, which renames it outright. Each pinned id ",
      "above is tagged with the name clients actually use, and the summary line says how many ",
      "are still exposed raw.",
    ),
  ];
}


async function loadConfig() {
  let res;
  try { res = await api("/admin/config"); }
  catch { $("#cfg-body").innerHTML = '<div class="notice">Cannot reach the proxy.</div>'; return; }
  if (res.status === 403) {
    $("#cfg-body").innerHTML = '<div class="notice">Unauthorized — set a valid bearer key to edit the config.</div>';
    $("#cfg-path").textContent = ""; $("#cfg-writable").textContent = "";
    return;
  }
  if (!res.ok) { $("#cfg-body").innerHTML = '<div class="notice">Failed to load the config.</div>'; return; }
  cfg = await res.json();
  renderConfig();
}

// Shared tail for every mutation: a 2xx carries the new snapshot, a 4xx carries
// {error}. Either way the UI ends up showing the server's truth.
async function cfgSave(path, method, body, what) {
  let res;
  try {
    res = await api(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch { toast("Save failed — cannot reach the proxy", "bad"); return false; }
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.persisted === false) {
    toast(data.error || `Save failed (${res.status})`, "bad");
    if (data.providers) { cfg = data; renderConfig(); }
    return false;
  }
  cfg = data;
  renderConfig();
  // Basename only: the full container path is long enough to wrap the toast.
  toast(what + " saved to " + (cfg.path || "config").split("/").pop());
  return true;
}

function renderConfig() {
  $("#cfg-path").textContent = cfg.path || "";
  const w = $("#cfg-writable");
  if (cfg.detached) {
    w.textContent = "file detached — saves would be lost on restart";
    w.style.color = "var(--red)";
  } else if (cfg.writable) {
    w.textContent = "file: writable";
    w.style.color = "";
  } else {
    w.textContent = "file: read-only — edits cannot be saved";
    w.style.color = "var(--amber)";
  }
  const body = $("#cfg-body");
  body.innerHTML = "";
  if (cfg.detached) body.appendChild(detachedBanner());
  body.appendChild(cfgUpstreamSection());
  body.appendChild(cfgGroupsSection());
  body.appendChild(cfgAliasSection());
}

const cfgDisabled = () => !cfg.writable || !!cfg.detached;

// The failure this exists for is silent by construction: the container keeps
// reading back its own writes, so everything looks saved right up until a restart
// throws the lot away. Loud, and every Save is disabled while it holds.
function detachedBanner() {
  const box = el("div", "cfgdetached");
  box.appendChild(el("div", "cfgdetachedhead", "⚠  This config file is detached — do not edit"));
  const p1 = el("p", "cfghelpline");
  p1.append(document.createTextNode(
    "The file was replaced on the host while this container was running — a "),
    code("git pull"), document.createTextNode(", "), code("git checkout"),
    document.createTextNode(", or an editor that saves by writing a new file and "
      + "renaming it over the old one (vim does this by default). The container is "
      + "still holding the old copy, so anything saved here would look fine and then "
      + "disappear the moment the container restarts."));
  const p2 = el("p", "cfghelpline");
  p2.append(document.createTextNode("Re-bind it on the host, then reload this page:"));
  const pre = el("pre", "flbodytext", "docker compose up -d --force-recreate llm-proxy");
  box.append(p1, p2, pre);
  return box;
}

/* ── Upstream models per provider ─────────────────────────── */
function cfgUpstreamSection() {
  const wrap = el("div", "cfgsec");
  wrap.appendChild(cfgHead("Upstream models", "upstream", () => [
    ...helpNative(),
    helpLine(
      el("b", null, "Allow all"), " means the proxy serves whatever the backend reports, ",
      "re-checked on a timer. Good for a box whose model list you change often.",
    ),
    helpLine(
      el("b", null, "Only these"), " pins an explicit allow-list. Anything not ticked is ",
      "invisible to clients and unroutable, even if the backend still serves it. ",
      "Good for a paid provider with hundreds of models where you only want a few.",
    ),
    helpLine(
      code("not in catalog"), " on a ticked id means it is pinned here but the backend no ",
      "longer reports it — usually a model that was retired upstream. It stays ticked so ",
      "saving cannot silently drop it; untick it to let it go.",
    ),
  ]));
  wrap.appendChild(el("div", "hint",
    "Which native model ids each backend is allowed to serve."));
  for (const p of cfg.providers) wrap.appendChild(cfgProviderCard(p));
  return wrap;
}

function cfgProviderCard(p) {
  const card = el("div", "mcard");
  const head = el("div", "mhead");
  head.appendChild(el("span", "mname", p.name));
  head.appendChild(el("span", "mtcount muted", p.base_url));
  if (p.require_permission) {
    const lock = el("span", "fltag", "🔒 key required");
    lock.title = "hidden from callers without a proxy key";
    head.appendChild(lock);
  }
  head.appendChild(el("span", "fltag", p.lists_all ? "allow all" : p.enabled_models.length + " pinned"));
  card.appendChild(head);

  const body = el("div", "cfgcard");
  let mode = p.lists_all ? "all" : "list";
  let chosen = new Set(p.enabled_models);

  const modes = el("div", "cfgmodes");
  const mk = (value, label, title) => {
    const b = el("button", "btn tiny" + (mode === value ? " primary" : ""), label);
    b.title = title;
    b.onclick = () => { mode = value; draw(); };
    return b;
  };
  const list = el("div", "cfglist");
  const actions = el("div", "cfgactions");

  function draw() {
    modes.innerHTML = "";
    modes.append(
      mk("all", "Allow all (live discovery)", "expose every model this backend reports"),
      mk("list", "Only these", "restrict to an explicit list of native ids"),
    );
    list.innerHTML = "";
    if (mode === "all") {
      list.appendChild(el("div", "hint",
        "Everything this backend reports from /v1/models is available. " +
        "Its catalog is refreshed every " + p.cache_ttl + "s."));
    } else {
      list.appendChild(exposureSummary(p, chosen));
      list.appendChild(catalogPicker(p, chosen, draw));
    }
    list.appendChild(nameMapBlock(p));
    actions.innerHTML = "";
    const save = el("button", "btn primary", "Save");
    save.disabled = cfgDisabled();
    save.onclick = () => cfgSave(
      `/admin/config/providers/${encodeURIComponent(p.name)}/enabled-models`,
      "PUT", { models: mode === "all" ? [] : Array.from(chosen) }, p.name);
    const reset = el("button", "btn", "Reset");
    reset.onclick = () => { mode = p.lists_all ? "all" : "list"; chosen = new Set(p.enabled_models); draw(); };
    actions.append(reset, save);
  }
  draw();
  body.append(modes, list, actions);
  card.appendChild(body);
  return card;
}

// What a client has to send to reach this native id. Three cases, and the
// difference between them is the thing the page was failing to explain:
//   fronted by a group  -> clients use the group name; the raw id is hidden
//   in model_map        -> clients use the mapped canonical name
//   neither             -> the native id IS the public name, verbatim
function clientName(p, native) {
  const group = (p.fronted || {})[native];
  if (group) return { name: group, via: "group" };
  const canon = (p.model_map || {})[native];
  if (canon && canon !== native) return { name: canon, via: "model_map" };
  return null;
}

function clientNameTag(p, native) {
  const cn = clientName(p, native);
  if (!cn) return null;
  const tag = el("span", "cfgmapped");
  tag.append(el("span", "cfgarrow", "→"), el("span", "cfgcanon", cn.name),
             el("span", "cfgvia", cn.via));
  tag.title = cn.via === "group"
    ? `clients send "${cn.name}" — the group ${cn.name} fronts this id, and it is hidden from /v1/models`
    : `clients send "${cn.name}" — this provider's model_map renames it`;
  return tag;
}

function catalogPicker(p, chosen, redraw) {
  const box = el("div", "cfgpick");
  const st = cfgCatalog.get(p.name);

  // Union of the probed catalog and whatever is already pinned: an id the backend
  // no longer reports must stay visible and checked, or saving would silently
  // drop it.
  const reported = new Set(st && st.ids ? st.ids : []);
  const all = Array.from(new Set([...reported, ...chosen])).sort();

  const head = el("div", "cfgpickhead");
  const count = el("span", "muted");
  const search = el("input");
  search.type = "search";
  search.className = "cfgsearch";
  search.placeholder = "filter " + (all.length || "") + " ids…";
  search.value = cfgFilter.get(p.name) || "";
  const probe = el("button", "btn tiny", st && st.ids ? "Re-probe" : "Probe backend");
  probe.title = "ask this backend for its full model list";
  probe.onclick = () => probeProvider(p.name, redraw);
  const pickShown = el("button", "btn tiny", "Tick shown");
  const clearShown = el("button", "btn tiny", "Untick shown");
  head.append(count, search, pickShown, clearShown, probe);
  box.appendChild(head);

  if (st && st.loading) { box.appendChild(el("div", "muted", "probing…")); return box; }
  if (st && st.error) box.appendChild(el("div", "uerr", st.error));

  if (!all.length) {
    box.appendChild(el("div", "hint", "Probe the backend to list what it serves, or type ids below."));
  }

  // Rows are built once and shown/hidden by the filter. Ticking updates the count
  // in place — a re-render on every keystroke or click would throw away the
  // reader's scroll position in a 300-model list, and the focus in the search box.
  const grid = el("div", "cfgcheckgrid");
  const rows = [];
  for (const id of all) {
    const row = el("label", "cfgcheck");
    const cb = el("input"); cb.type = "checkbox"; cb.checked = chosen.has(id);
    cb.onchange = () => { cb.checked ? chosen.add(id) : chosen.delete(id); refresh(); };
    row.append(cb, el("span", "cfgcheckid", id));
    const tag = clientNameTag(p, id);
    if (tag) row.appendChild(tag);
    if (!reported.has(id) && st && st.ids) {
      const warn = el("span", "fltag warn", "not in catalog");
      warn.title = "pinned here but the backend no longer reports it";
      row.appendChild(warn);
    }
    grid.appendChild(row);
    rows.push({ id, row, cb });
  }
  box.appendChild(grid);
  const empty = el("div", "hint", "Nothing matches that filter.");
  empty.style.display = "none";
  box.appendChild(empty);

  const shown = () => rows.filter((r) => r.row.style.display !== "none");

  function refresh() {
    const q = search.value.trim().toLowerCase();
    let visible = 0;
    for (const r of rows) {
      const match = !q || r.id.toLowerCase().includes(q);
      r.row.style.display = match ? "" : "none";
      if (match) visible += 1;
    }
    empty.style.display = all.length && !visible ? "" : "none";
    count.textContent = chosen.size + " of " + all.length + " selected"
      + (q ? "  ·  " + visible + " shown" : "");
    const none = !visible;
    pickShown.disabled = none;
    clearShown.disabled = none;
    pickShown.title = q ? "tick every id matching the filter" : "tick all";
    clearShown.title = q ? "untick every id matching the filter" : "untick all";
  }

  search.addEventListener("input", () => { cfgFilter.set(p.name, search.value); refresh(); });
  pickShown.onclick = () => {
    for (const r of shown()) { chosen.add(r.id); r.cb.checked = true; }
    refresh();
  };
  clearShown.onclick = () => {
    for (const r of shown()) { chosen.delete(r.id); r.cb.checked = false; }
    refresh();
  };
  refresh();

  // Manual entry, for a backend that can't be probed (or an id it hides).
  const add = el("div", "cfgadd");
  const input = el("input"); input.type = "text"; input.className = "cfginput";
  // A concrete example beats the abstract label: pick one of this backend's own
  // ids so the shape (vendor prefixes, colons, suffixes) is obvious.
  const sample = Object.keys(p.model_map || {})[0] || (st && st.ids && st.ids[0]) || all[0];
  input.placeholder = sample
    ? `add a native model id…    e.g. ${sample}`
    : "add a native model id…    e.g. vendor/model-name:variant";
  const btn = el("button", "btn tiny", "Add");
  const doAdd = () => {
    const v = input.value.trim();
    if (!v) return;
    // This field pins ONE native id. People reach for it to write a mapping
    // ("z-ai/glm-5.2 -> glm-5.2"), which used to be accepted verbatim as an id
    // that could never match anything. Say where mappings live instead.
    if (/->|=>|\s/.test(v)) {
      toast("One native id only — no spaces or arrows. To rename an id for clients, "
            + "use Name mapping below.", "bad");
      return;
    }
    chosen.add(v); input.value = ""; redraw();
  };
  btn.onclick = doAdd;
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); doAdd(); } });
  add.append(input, btn);
  box.appendChild(add);
  return box;
}

async function probeProvider(name, redraw) {
  cfgCatalog.set(name, { loading: true });
  redraw();
  try {
    const res = await api("/admin/upstream-models?provider=" + encodeURIComponent(name));
    const data = await res.json();
    const entry = (data.providers || [])[0];
    if (!entry) cfgCatalog.set(name, { error: "no response for " + name });
    else if (entry.ok) cfgCatalog.set(name, { ids: entry.ids });
    else cfgCatalog.set(name, { error: entry.error || "probe failed" });
  } catch {
    cfgCatalog.set(name, { error: "probe failed — cannot reach the proxy" });
  }
  redraw();
}

// One line telling you, in plain terms, how the ids you pinned are exposed —
// because "8 pinned" says nothing about what a client is supposed to type.
function exposureSummary(p, chosen) {
  const ids = Array.from(chosen);
  let viaGroup = 0, viaMap = 0, raw = [];
  for (const id of ids) {
    const cn = clientName(p, id);
    if (!cn) raw.push(id);
    else if (cn.via === "group") viaGroup += 1;
    else viaMap += 1;
  }
  const box = el("div", "cfgexpose");
  const parts = [];
  if (viaGroup) parts.push(viaGroup + " behind a group");
  if (viaMap) parts.push(viaMap + " renamed by model_map");
  if (raw.length) parts.push(raw.length + " exposed under the raw id");
  box.appendChild(el("div", "cfgexposehead",
    ids.length ? "Clients see: " + parts.join(" · ") : "Nothing pinned yet."));
  if (raw.length) {
    const d = el("div", "cfgexposeraw");
    d.append(document.createTextNode(
      "These have no group and no model_map entry, so a client must send the id exactly as written: "));
    raw.slice(0, 8).forEach((id, i) => {
      if (i) d.append(document.createTextNode(", "));
      d.appendChild(code(id));
    });
    if (raw.length > 8) d.append(document.createTextNode(` and ${raw.length - 8} more`));
    box.appendChild(d);
  }
  return box;
}

// model_map, editable. This is how a native id gets a clean client-facing name
// without a group — the question the "add a native model id" field kept being
// mistaken for.
function nameMapBlock(p) {
  let rows = Object.entries(p.model_map || {}).map(([native, canon]) => ({ native, canon }));
  const box = el("div", "cfgmapbox");
  const head = el("div", "cfgmaphead");
  const countTag = el("span", "fltag");
  head.append(
    el("span", null, "Name mapping"),
    countTag,
    el("span", "muted", "native id → what clients call it"),
  );
  box.appendChild(head);
  const grid = el("div", "cfgmapgrid");
  const foot = el("div", "cfgmapfoot");
  box.append(grid, foot);

  function draw() {
    countTag.textContent = rows.length ? rows.length + " entries" : "none";
    grid.innerHTML = "";
    rows.forEach((r, i) => {
      const row = el("div", "cfgmaprow");
      const native = el("input");
      native.type = "text"; native.className = "cfginput mono"; native.value = r.native;
      native.placeholder = "native id, e.g. z-ai/glm-5.2";
      native.onchange = () => { r.native = native.value.trim(); };
      const canon = el("input");
      canon.type = "text"; canon.className = "cfginput mono"; canon.value = r.canon;
      canon.placeholder = "name clients send, e.g. glm-5.2";
      canon.onchange = () => { r.canon = canon.value.trim(); };
      const del = el("button", "mini", "✕");
      del.title = "remove this mapping";
      del.onclick = () => { rows.splice(i, 1); draw(); };
      row.append(native, el("span", "cfgarrow", "→"), canon, del);
      grid.appendChild(row);
    });
    if (!rows.length) {
      grid.appendChild(el("div", "hint",
        "No mapping — every id this backend serves is exposed under its native name. "
        + "Add one to rename it for clients."));
    }
    foot.innerHTML = "";
    const add = el("button", "btn tiny", "+ Add mapping");
    add.onclick = () => { rows.push({ native: "", canon: "" }); draw(); };
    const reset = el("button", "btn tiny", "Reset");
    reset.onclick = () => {
      rows = Object.entries(p.model_map || {}).map(([native, canon]) => ({ native, canon }));
      draw();
    };
    const save = el("button", "btn tiny primary", "Save mapping");
    save.disabled = cfgDisabled();
    save.onclick = () => {
      const out = {};
      for (const r of rows) {
        if (!r.native || !r.canon) { toast("Every mapping needs both fields", "bad"); return; }
        if (out[r.native]) { toast("'" + r.native + "' is listed twice", "bad"); return; }
        out[r.native] = r.canon;
      }
      cfgSave(`/admin/config/providers/${encodeURIComponent(p.name)}/model-map`,
              "PUT", { model_map: out }, p.name + " mapping");
    };
    foot.append(add, el("span", "grow"), reset, save);
  }
  draw();
  return box;
}

/* ── Model groups (logical models) ────────────────────────── */
function cfgGroupsSection() {
  const wrap = el("div", "cfgsec");
  wrap.appendChild(cfgHead("Model groups", "groups", () => [
    helpLine(
      "A group is one ", el("b", null, "canonical name"), " your clients ask for, in front of ",
      "an ordered list of backends that can serve it. It gives you failover and ",
      "load-balancing behind a single stable name.",
    ),
    helpLine(
      el("b", null, "Priority"), " — lower wins. The proxy takes the lowest-priority backend ",
      "with a free slot; equal priorities share load round-robin. If they are all busy the ",
      "request queues, and if one errors the next one is tried.",
    ),
    helpLine(
      el("b", null, "Native id"), " — what to send that backend on the wire. Leave it ",
      el("b", null, "blank"), " to inherit it from that provider's ", code("model_map"),
      "; the greyed ", code("inherits …"), " placeholder shows what that resolves to today. ",
      "Fill it in only to override — e.g. pinning a specific quantization on one box.",
    ),
    helpLine(
      "A group hides the ids it fronts from ", code("/v1/models"), ", so clients use the ",
      "stable group name instead of a per-backend variant.",
    ),
  ]));
  for (const m of cfg.logical_models) wrap.appendChild(cfgGroupCard(m));

  const add = el("div", "cfgnew");
  const input = el("input"); input.type = "text"; input.className = "cfginput";
  input.placeholder = "new group name, e.g. local.my-model";
  input.value = cfgNewModelName;
  input.oninput = () => { cfgNewModelName = input.value; };
  const btn = el("button", "btn primary", "Create group");
  btn.disabled = cfgDisabled();
  btn.onclick = async () => {
    const name = input.value.trim();
    if (!name) { toast("Give the group a name", "bad"); return; }
    const first = cfg.providers[0];
    if (await cfgSave(`/admin/config/models/${encodeURIComponent(name)}`, "PUT",
                      { targets: [{ provider: first.name, priority: 1 }] }, name)) {
      cfgNewModelName = "";
    }
  };
  add.append(input, btn);
  wrap.appendChild(add);
  return wrap;
}

function cfgGroupCard(m) {
  const card = el("div", "mcard");
  let targets = m.targets.map((t) => ({ ...t }));

  const head = el("div", "mhead");
  head.appendChild(el("span", "mname", m.name));
  head.appendChild(el("span", "mtcount muted",
    targets.length + (targets.length === 1 ? " target" : " targets")));
  const actions = el("div", "mactions");
  head.appendChild(actions);
  card.appendChild(head);
  const list = el("div", "cfgcard");
  card.appendChild(list);

  function draw() {
    list.innerHTML = "";
    targets.forEach((t, i) => {
      const row = el("div", "cfgtrow");
      const sel = el("select"); sel.className = "cfgsel";
      for (const p of cfg.providers) {
        const o = el("option", null, p.name);
        o.value = p.name;
        if (p.name === t.provider) o.selected = true;
        sel.appendChild(o);
      }
      sel.onchange = () => { t.provider = sel.value; };

      const mid = el("input"); mid.type = "text"; mid.className = "cfginput mono";
      // Empty means "inherit from the backend's model_map" — the placeholder shows
      // what that currently resolves to, so leaving it blank is informed rather
      // than a guess.
      mid.value = t.model || "";
      mid.placeholder = t.resolved_model ? "inherits " + t.resolved_model : "native id (blank = inherit)";
      mid.title = "leave blank to inherit the native id from the backend's model_map";
      mid.onchange = () => { t.model = mid.value.trim(); };

      const prio = el("input"); prio.type = "number"; prio.min = "1"; prio.className = "cfgprio";
      prio.value = t.priority;
      prio.onchange = () => {
        const v = parseInt(prio.value, 10);
        t.priority = Number.isFinite(v) && v > 0 ? v : 1;
        prio.value = t.priority;
      };

      const del = el("button", "mini", "✕");
      del.title = "remove this target";
      del.onclick = () => { targets.splice(i, 1); draw(); };

      row.append(sel, mid, el("span", "muted", "priority"), prio, del);
      list.appendChild(row);
    });
    const addRow = el("div", "cfgtrow");
    const addBtn = el("button", "btn tiny", "+ Add target");
    addBtn.onclick = () => {
      targets.push({ provider: cfg.providers[0].name, model: "", resolved_model: "", priority: targets.length + 1 });
      draw();
    };
    addRow.appendChild(addBtn);
    list.appendChild(addRow);

    actions.innerHTML = "";
    const del = el("button", "btn", "Delete group");
    del.disabled = cfgDisabled();
    del.onclick = () => cfgSave(`/admin/config/models/${encodeURIComponent(m.name)}`,
                                "DELETE", undefined, m.name + " (deleted)");
    const reset = el("button", "btn", "Reset");
    reset.onclick = () => { targets = m.targets.map((t) => ({ ...t })); draw(); };
    const save = el("button", "btn primary", "Save");
    save.disabled = cfgDisabled();
    save.onclick = () => {
      if (!targets.length) { toast("A group needs at least one target", "bad"); return; }
      cfgSave(`/admin/config/models/${encodeURIComponent(m.name)}`, "PUT", {
        targets: targets.map((t) => ({
          provider: t.provider, priority: t.priority,
          ...(t.model ? { model: t.model } : {}),
        })),
      }, m.name);
    };
    actions.append(del, reset, save);
  }
  draw();
  return card;
}

/* ── Aliases ──────────────────────────────────────────────── */
function cfgAliasSection() {
  const wrap = el("div", "cfgsec");
  wrap.appendChild(cfgHead("Aliases", "aliases", () => [
    helpLine(
      "A one-line shortcut: one name resolves to another. ", code("chat"), " → ",
      code("deepseek-v4-pro"), " lets a client send ", code("chat"), " and get that model.",
    ),
    helpLine(
      "Unlike a group, an alias has no failover or priorities — it is pure renaming. ",
      "Point it at a canonical name, or at ", code("provider:model"),
      " to force one specific backend.",
    ),
    helpLine(
      el("b", null, "Aliases resolve first"), ", ahead of groups and everything else. So an ",
      "alias sharing a name with a group wins and the group becomes unreachable — which is ",
      "why that combination is refused here.",
    ),
  ]));
  let rows = Object.entries(cfg.aliases).map(([k, v]) => ({ k, v }));
  const list = el("div", "cfgcard");
  const actions = el("div", "cfgactions");

  function draw() {
    list.innerHTML = "";
    rows.forEach((r, i) => {
      const row = el("div", "cfgtrow");
      const k = el("input"); k.type = "text"; k.className = "cfginput mono"; k.value = r.k;
      k.placeholder = "alias"; k.onchange = () => { r.k = k.value.trim(); };
      const v = el("input"); v.type = "text"; v.className = "cfginput mono"; v.value = r.v;
      v.placeholder = "target model or provider:model";
      v.onchange = () => { r.v = v.value.trim(); };
      const del = el("button", "mini", "✕");
      del.onclick = () => { rows.splice(i, 1); draw(); };
      row.append(k, el("span", "aarrow", "→"), v, del);
      list.appendChild(row);
    });
    if (!rows.length) list.appendChild(el("div", "hint", "No aliases."));
    const addRow = el("div", "cfgtrow");
    const addBtn = el("button", "btn tiny", "+ Add alias");
    addBtn.onclick = () => { rows.push({ k: "", v: "" }); draw(); };
    addRow.appendChild(addBtn);
    list.appendChild(addRow);
  }
  draw();

  const save = el("button", "btn primary", "Save aliases");
  save.disabled = cfgDisabled();
  save.onclick = () => {
    const out = {};
    for (const r of rows) {
      if (!r.k || !r.v) { toast("Every alias needs a name and a target", "bad"); return; }
      if (out[r.k]) { toast("Duplicate alias '" + r.k + "'", "bad"); return; }
      out[r.k] = r.v;
    }
    cfgSave("/admin/config/aliases", "PUT", { aliases: out }, "Aliases");
  };
  actions.appendChild(save);
  wrap.append(list, actions);
  return wrap;
}

/* ── Routing ───────────────────────────────────────────────── */
async function loadRouting() {
  let res;
  try { res = await api("/admin/routing"); }
  catch { $("#logical").innerHTML = '<div class="notice">Cannot reach the proxy.</div>'; return; }
  if (res.status === 403) {
    $("#providers").innerHTML = "";
    $("#aliases").innerHTML = "";
    $("#logical").innerHTML = '<div class="notice">Unauthorized — set a valid bearer key to view routing.</div>';
    return;
  }
  if (!res.ok) { $("#logical").innerHTML = '<div class="notice">Failed to load routing.</div>'; return; }
  const data = await res.json();
  $("#routing-autogroup").textContent = "auto_group: " + data.auto_group;
  const cfg = $("#routing-config");
  if (data.config_writable) {
    cfg.textContent = "config: writable";
    cfg.style.color = "";
    cfg.title = "priority changes are written back to the config file";
  } else {
    cfg.textContent = "config: read-only — changes won't survive a restart";
    cfg.style.color = "var(--amber)";
    cfg.title = "the config file mount is read-only; drop :ro on the volume to persist changes";
  }
  const downSet = new Set((data.providers || []).filter((p) => p.is_down).map((p) => p.name));
  renderProviders(data.providers || []);
  renderLogical(data.logical_models || [], downSet);
  renderAliases(data.aliases || {});
}

function renderProviders(providers) {
  const wrap = $("#providers");
  wrap.innerHTML = "";
  wrap.appendChild(el("h2", null, "Providers"));
  const grid = el("div", "pgrid");
  for (const p of providers) {
    const chip = el("div", "pchip" + (p.is_down ? " down" : ""));
    chip.title = p.base_url || "";
    chip.appendChild(el("span", "pname", p.name));
    chip.appendChild(el("span", "pslot", p.slots == null ? "∞" : `${p.in_use}/${p.slots}`));
    if (p.require_permission) { const f = el("span", "pflag lock", "🔒"); f.title = "require_permission"; chip.appendChild(f); }
    if (p.lists_all) { const f = el("span", "pflag", "live"); f.title = "live-discovers all models"; chip.appendChild(f); }
    if (p.is_down) chip.appendChild(el("span", "pflag downflag", "down"));
    grid.appendChild(chip);
  }
  wrap.appendChild(grid);
}

function renderLogical(models, downSet) {
  const wrap = $("#logical");
  wrap.innerHTML = "";
  wrap.appendChild(el("h2", null, "Model routing"));
  if (!models.length) { wrap.appendChild(el("div", "hint", "No explicit logical models configured.")); return; }
  for (const m of models) wrap.appendChild(modelCard(m, downSet));
}

function modelCard(model, downSet) {
  const card = el("div", "mcard");
  let targets = model.targets.map((t) => ({ ...t })); // working copy

  const head = el("div", "mhead");
  head.appendChild(el("span", "mname", model.name));
  head.appendChild(el("span", "mtcount muted",
    targets.length + (targets.length === 1 ? " target" : " targets")));
  const actions = el("div", "mactions");
  const resetBtn = el("button", "btn", "Reset");
  const saveBtn = el("button", "btn primary", "Save");
  saveBtn.disabled = true;
  actions.append(resetBtn, saveBtn);
  head.appendChild(actions);
  card.appendChild(head);

  const list = el("div", "tlist");
  card.appendChild(list);

  const markDirty = () => { saveBtn.disabled = false; };

  // Display in priority order; JS sort is stable so equal priorities keep order.
  function refresh() {
    targets.sort((a, b) => a.priority - b.priority);
    renderRows();
  }

  // Up/down reorders array position and renumbers 1..N (a clean strict order).
  // Manual priority edits stay as typed, so ties remain expressible there.
  function move(i, dir) {
    const j = i + dir;
    if (j < 0 || j >= targets.length) return;
    const [item] = targets.splice(i, 1);
    targets.splice(j, 0, item);
    targets.forEach((t, idx) => { t.priority = idx + 1; });
    markDirty();
    renderRows();
  }

  function renderRows() {
    list.innerHTML = "";
    targets.forEach((t, i) => {
      const down = downSet.has(t.provider);
      const row = el("div", "trow" + (down ? " down" : ""));

      const ctrls = el("div", "tctrls");
      const up = el("button", "mini", "↑"); up.disabled = i === 0; up.onclick = () => move(i, -1);
      const dn = el("button", "mini", "↓"); dn.disabled = i === targets.length - 1; dn.onclick = () => move(i, 1);
      ctrls.append(up, dn);

      const info = el("div", "tinfo");
      info.append(el("span", "tprov", t.provider), el("span", "tmid", t.model));

      const badges = el("div", "tbadges");
      if (down) badges.appendChild(el("span", "bdown", "down"));
      if (t.known_provider === false) { const w = el("span", "bwarn", "unknown"); w.title = "provider not in config"; badges.appendChild(w); }

      const prioWrap = el("label", "tprio");
      prioWrap.appendChild(el("span", null, "priority"));
      const prio = el("input"); prio.type = "number"; prio.min = "1"; prio.value = t.priority;
      prio.addEventListener("change", () => {
        const v = parseInt(prio.value, 10);
        t.priority = Number.isFinite(v) && v > 0 ? v : 1;
        prio.value = t.priority;
        markDirty();
      });
      prioWrap.appendChild(prio);

      row.append(ctrls, info, badges, prioWrap);
      list.appendChild(row);
    });
  }

  resetBtn.onclick = () => {
    targets = model.targets.map((t) => ({ ...t }));
    saveBtn.disabled = true;
    refresh();
  };

  saveBtn.onclick = async () => {
    saveBtn.disabled = true;
    const label = saveBtn.textContent;
    saveBtn.textContent = "Saving…";
    try {
      const res = await api("/admin/routing/" + encodeURIComponent(model.name), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          targets: targets.map((t) => ({ provider: t.provider, model: t.model, priority: t.priority })),
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        toast(err.error || `Save failed (${res.status})`, "bad");
        saveBtn.disabled = false;
        saveBtn.textContent = label;
        return;
      }
      const updated = await res.json();
      model.targets = updated.targets.map((t) => ({ ...t })); // new baseline for Reset
      targets = updated.targets.map((t) => ({ ...t }));
      refresh();
      saveBtn.textContent = "Saved ✓";
      if (updated.persisted) {
        toast("Saved " + model.name + " (live + config)");
      } else {
        toast("Saved " + model.name + " live only — not persisted: " + (updated.persist_error || "unknown"), "bad");
      }
      setTimeout(() => { saveBtn.textContent = label; }, 1200);
    } catch {
      toast("Save error", "bad");
      saveBtn.disabled = false;
      saveBtn.textContent = label;
    }
  };

  refresh();
  return card;
}

function renderAliases(aliases) {
  const wrap = $("#aliases");
  wrap.innerHTML = "";
  const keys = Object.keys(aliases);
  if (!keys.length) { wrap.appendChild(el("span", "muted", "none")); return; }
  for (const k of keys) {
    const row = el("div", "arow");
    row.append(el("span", "aname", k), el("span", "aarrow", "→"), el("span", "atarget", aliases[k]));
    wrap.appendChild(row);
  }
}

/* ── Init ──────────────────────────────────────────────────── */
async function loadBuild() {
  // Which image is actually answering — the quickest way to notice a container
  // still running a stale tag after a deploy. /health needs no key, so this
  // works before signing in.
  const chip = $("#build");
  try {
    const res = await fetch("/health");
    if (!res.ok) return;
    const d = await res.json();
    if (!d.version) return;
    chip.textContent = d.version;
    chip.classList.toggle("release", !!d.release);
    chip.title = d.revision
      ? `build ${d.version} · commit ${d.revision}`
      : `build ${d.version} — not a CI build`;
    chip.hidden = false;
  } catch {
    /* console still works without it; leave the chip hidden */
  }
}

function initKey() {
  $("#api-key").value = getKey();
  const save = () => {
    setKey($("#api-key").value.trim());
    toast("Key saved");
    const active = ($(".tab.active") || {}).dataset?.tab;
    loadLogFlags();
    if (active === "logging") resetLogTail();
    if (active === "inflight") pollFlight();
    if (active === "models") { loadCatalog(); }
    if (active === "routing") loadRouting();
    if (active === "config") loadConfig();
  };
  $("#save-key").addEventListener("click", save);
  $("#api-key").addEventListener("keydown", (e) => { if (e.key === "Enter") save(); });
  // No key yet → put the cursor in the field so it's obvious where to start.
  if (!getKey()) $("#api-key").focus();
}

window.addEventListener("DOMContentLoaded", () => {
  initKey();
  $$(".tab").forEach((b) => b.addEventListener("click", () => activateTab(b.dataset.tab)));

  $("#log-input").addEventListener("change", (e) => setLogFlag("log_input", e.target.checked));
  $("#log-output").addEventListener("change", (e) => setLogFlag("log_output", e.target.checked));
  $("#log-level").addEventListener("change", resetLogTail);
  $("#log-search").addEventListener("input", applyLogFilter);
  $("#log-clear").addEventListener("click", () => { $("#log-pane").innerHTML = ""; logLineCount = 0; $("#log-count").textContent = ""; });

  $("#fl-refresh").addEventListener("click", pollFlight);
  $("#fl-live").addEventListener("change", (e) => {
    if (e.target.checked) startFlightPolling();
  });

  $("#models-refresh").addEventListener("click", loadCatalog);
  $("#models-probe").addEventListener("click", probeUpstreams);
  $("#routing-refresh").addEventListener("click", loadRouting);
  $("#cfg-refresh").addEventListener("click", loadConfig);

  loadBuild();
  loadLogFlags();
  activateTab("logging");
});
