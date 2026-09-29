"use strict";
/* DAToolkit frontend. The AI never reaches a terminal from here except through the
   technician clicking Run/Insert on a queue item. */

// ------------------------------------------------------------------ token & helpers

const TOKEN = (() => {
  const fromUrl = new URLSearchParams(location.search).get("t");
  try {
    if (fromUrl) sessionStorage.setItem("dat-token", fromUrl);
    const t = fromUrl || sessionStorage.getItem("dat-token");
    history.replaceState(null, "", "/");
    return t;
  } catch { return fromUrl; }
})();

const S = {
  state: null,
  terms: {},          // sid -> {term, fit, host, ws, markers: Map(num -> IMarker)}
  activeSid: null,
  streaming: null,    // {text, reasoning, model, tier}
  promptModals: {},   // prompt id -> modal
  rows: new Map(),    // queue num -> row element
};

const $ = (sel, root = document) => root.querySelector(sel);

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { "X-Token": TOKEN, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) throw new Error(data.error || data.detail || `${res.status} ${res.statusText}`);
  return data;
}

function toast(text, kind = "info", ms = 6000) {
  const el = h("div", { class: `toast ${kind}` }, text);
  $("#toasts").append(el);
  setTimeout(() => el.remove(), ms);
}

async function guarded(fn) {
  try { return await fn(); } catch (e) { toast(e.message, "error"); }
}

function md(text) {
  return DOMPurify.sanitize(marked.parse(text || "", { breaks: true }));
}

const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* ignore */ } },
};

async function clipWrite(text) {
  try {
    if (window.pywebview?.api?.clipboard_set) return await window.pywebview.api.clipboard_set(text);
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = h("textarea", { value: text });
    document.body.append(ta); ta.select(); document.execCommand("copy"); ta.remove();
  }
}

async function clipRead() {
  if (window.pywebview?.api?.clipboard_get) return (await window.pywebview.api.clipboard_get()) || "";
  return await navigator.clipboard.readText();
}

// ------------------------------------------------------------------ modals

function modal({ title, body, buttons = [], wide = false, onClose, dismissable = true }) {
  const root = $("#modal-root");
  const box = h("div", { class: `modal${wide ? " wide" : ""}` });
  const overlay = h("div", { class: "overlay" }, box);
  const close = () => { overlay.remove(); onClose?.(); };
  const content = h("div", { class: "content" }, body);
  const btnBar = h("div", { class: "buttons" });
  for (const b of buttons) {
    const btn = h("button", { class: b.kind || "", type: "button" }, b.label);
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        const keep = await b.onClick?.();
        if (keep !== true) close();
      } catch (e) {
        toast(e.message, "error");
      } finally {
        btn.disabled = false;
      }
    });
    btnBar.append(btn);
  }
  box.append(h("h2", {}, title), content);
  if (buttons.length) box.append(btnBar);
  if (dismissable) {
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) close(); });
    overlay.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  }
  root.append(overlay);
  setTimeout(() => box.querySelector("input:not([type=checkbox]):not([type=radio]), textarea, select")?.focus(), 30);
  return { close, box };
}

function confirmModal(title, message, okLabel = "OK", kind = "primary") {
  return new Promise((resolve) => {
    let result = false;
    modal({
      title, body: h("div", {}, message),
      buttons: [
        { label: "Cancel" },
        { label: okLabel, kind, onClick: () => { result = true; } },
      ],
      onClose: () => resolve(result),
    });
  });
}

// ------------------------------------------------------------------ events socket

function connectEvents() {
  const ws = new WebSocket(`ws://${location.host}/ws/events?t=${encodeURIComponent(TOKEN)}`);
  ws.onmessage = (m) => handleEvent(JSON.parse(m.data));
  ws.onclose = () => setTimeout(connectEvents, 1500);
}

function handleEvent(ev) {
  switch (ev.type) {
    case "state":
      S.state = ev.state;
      renderAll();
      break;
    case "sessions":
      S.state.sessions = ev.sessions;
      renderSessions();
      renderQueue();
      break;
    case "queue":
      S.state.queue = ev.queue;
      renderQueue();
      break;
    case "chat":
      S.state.chat.push(ev.entry);
      renderChat(true);
      break;
    case "turn_start":
      S.state.busy = true;
      S.streaming = { text: "", reasoning: "", model: ev.model, tier: ev.tier };
      renderBusy();
      renderChat(true);
      break;
    case "delta":
      if (!S.streaming) break;
      S.streaming[ev.kind === "text" ? "text" : "reasoning"] += ev.text;
      scheduleStreamRender();
      break;
    case "turn_end":
      S.state.busy = false;
      S.streaming = null;
      S.state.chat = ev.chat;
      renderBusy();
      renderChat(true);
      if (ev.error && ev.error !== "stopped") toast(`AI request failed: ${ev.error}`, "error", 12000);
      break;
    case "toast":
      toast(ev.text, ev.level || "info", 10000);
      break;
    case "prompt":
      showCredentialPrompt(ev.prompt);
      break;
    case "prompt_done":
      S.promptModals[ev.id]?.close();
      delete S.promptModals[ev.id];
      break;
  }
}

// ------------------------------------------------------------------ top bar

function renderTop() {
  const st = S.state;
  $("#case-btn").textContent = st.case ? st.case.name : "No case";
  const sb = $("#sens-badge");
  sb.textContent = st.case ? st.case.sensitivity : "";
  sb.className = `badge ${st.case?.sensitivity || ""}`;
  $("#model-name").textContent = st.config.active_model || "Choose model";
  const tb = $("#tier-badge");
  tb.textContent = st.active_tier || "";
  tb.className = `badge ${st.active_tier || ""}`;
  const ab = $("#attest-btn");
  const att = st.attestation && st.attestation.model === st.config.active_model ? st.attestation : null;
  ab.classList.toggle("hidden", !att);
  if (att) {
    ab.className = `ghost small ${att.status === "verified" ? "ok" : att.status === "failed" ? "bad" : ""}`;
    ab.textContent = { verified: att.latest_release ? "🔐 attested (older release)" : "🔐 attested",
      checking: "Attesting enclave…", failed: "⚠ attestation failed" }[att.status];
    ab.title = att.status === "verified"
      ? `End-to-end encrypted to an attested enclave.\n${att.summary}\nKey sha256: ${att.hpke_key_sha256.slice(0, 16)}…\nClick to re-check.`
      : att.status === "failed" ? `${att.error}\nNothing is sent until attestation succeeds. Click to retry.` : "Verifying the enclave…";
  }
  const banner = $("#keyring-banner");
  banner.textContent = st.keyring_error ? `⚠ ${st.keyring_error} API keys and passwords cannot be stored.` : "";
  banner.classList.toggle("hidden", !st.keyring_error);
}

function renderAll() {
  renderTop();
  renderChat(true);
  renderSessions();
  renderQueue();
  renderBusy();
  for (const p of S.state.prompts || []) if (!S.promptModals[p.id]) showCredentialPrompt(p);
  if (!S.state.case && !document.querySelector(".case-modal")) openCaseModal(true);
}

// ------------------------------------------------------------------ chat

function nearBottom(el) { return el.scrollHeight - el.scrollTop - el.clientHeight < 80; }

function renderEntry(e) {
  if (e.kind === "note") return h("div", { class: "msg note" }, e.text);
  if (e.kind === "user") {
    const box = h("div", { class: "msg user" }, h("div", { class: "who" }, "You"));
    if (e.text) box.append(h("div", { class: "body", html: md(e.text) }));
    for (const r of e.results || []) {
      const label = `#${r.num} ${r.status.toUpperCase()} · ${r.session_id} · ${r.command}`;
      box.append(h("details", {}, h("summary", {}, label),
        r.note ? h("div", { class: "small" }, `Note: ${r.note}`) : null,
        r.text ? h("pre", {}, r.text) : null));
    }
    for (const s of e.snippets || []) {
      box.append(h("details", {}, h("summary", {}, `Terminal excerpt · ${s.session_id}`), h("pre", {}, s.text)));
    }
    return box;
  }
  // assistant (final or streaming)
  const who = h("div", { class: "who" }, "AI", e.model ? ` · ${e.model}` : "",
    e.tier ? h("span", { class: `badge ${e.tier}` }, e.tier) : null,
    e.sealed ? h("span", { class: "sealed", title: e.sealed }, "🔐 end-to-end encrypted") : null);
  const box = h("div", { class: "msg assistant" }, who);
  if (e.reasoning) {
    box.append(h("details", { open: e.streaming && !e.text ? true : false },
      h("summary", {}, "Reasoning"), h("div", { class: "reasoning" }, e.reasoning)));
  }
  box.append(h("div", { class: "body", html: md(e.text) }));
  if (e.proposals?.length) {
    box.append(h("div", { class: "chips" }, e.proposals.map((n) =>
      h("span", { class: "chip", title: "Show in queue", onclick: () => flashQueueItem(n) }, `#${n}`))));
  }
  return box;
}

function renderChat(scroll) {
  const log = $("#chat-log");
  const stick = scroll || nearBottom(log);
  log.replaceChildren(...(S.state.chat || []).map(renderEntry));
  if (S.streaming) log.append(streamingBubble());
  if (!S.state.chat?.length && !S.streaming) {
    log.append(h("div", { class: "empty" },
      "Describe the problem. The AI will propose commands into the queue below; nothing runs until you click Run."));
  }
  if (stick) log.scrollTop = log.scrollHeight;
}

function streamingBubble() {
  const el = renderEntry({ ...S.streaming, streaming: true });
  el.id = "streaming";
  return el;
}

let streamPending = false;
function scheduleStreamRender() {
  if (streamPending) return;
  streamPending = true;
  requestAnimationFrame(() => {
    streamPending = false;
    const log = $("#chat-log");
    const stick = nearBottom(log);
    const old = $("#streaming");
    if (old && S.streaming) old.replaceWith(streamingBubble());
    else if (S.streaming) log.append(streamingBubble());
    if (stick) log.scrollTop = log.scrollHeight;
  });
}

function renderBusy() {
  const busy = !!S.state.busy;
  $("#chat-status").classList.toggle("hidden", !busy);
  $("#stop-btn").classList.toggle("hidden", !busy);
  $("#send-btn").disabled = busy;
  $("#send-results-btn").disabled = busy || readyItems().length === 0;
}

async function sendChat(ev) {
  ev?.preventDefault();
  const input = $("#chat-input");
  const message = input.value.trim();
  if (!message) return;
  await guarded(async () => {
    await api("POST", "/api/send", { message });
    input.value = "";
  });
}

// ------------------------------------------------------------------ terminals

function ensureTerm(sess) {
  if (S.terms[sess.id]) return S.terms[sess.id];
  const cfg = S.state.config.settings;
  const host = h("div", { class: "term-host" });
  $("#terms").append(host);
  const term = new Terminal({
    fontFamily: '"JetBrains Mono", "Fira Code", "DejaVu Sans Mono", "Ubuntu Mono", monospace',
    fontSize: cfg.font_size || 13,
    scrollback: cfg.scrollback || 10000,
    cursorBlink: true,
    allowProposedApi: true,
    theme: { background: "#0b0d10", foreground: "#d8dee6", selectionBackground: "#2f6fc080" },
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(host);
  const t = { id: sess.id, term, fit, host, ws: null, markers: new Map() };
  term.onData((d) => termSend(t, { type: "input", data: d }));
  term.onResize(({ cols, rows }) => termSend(t, { type: "resize", cols, rows }));
  term.attachCustomKeyEventHandler((e) => {
    if (e.type !== "keydown" || !e.ctrlKey || !e.shiftKey) return true;
    if (e.code === "KeyC") { clipWrite(term.getSelection()); return false; }
    if (e.code === "KeyV") { clipRead().then((txt) => txt && term.paste(txt)).catch(() => toast("Clipboard not available", "error")); return false; }
    return true;
  });
  const ws = new WebSocket(`ws://${location.host}/ws/term/${encodeURIComponent(sess.id)}?t=${encodeURIComponent(TOKEN)}`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => { t.ws = ws; fitTerm(t, true); };
  ws.onmessage = (m) => term.write(new Uint8Array(m.data));
  ws.onclose = () => { t.ws = null; };
  S.terms[sess.id] = t;
  return t;
}

function termSend(t, msg) {
  if (t.ws && t.ws.readyState === WebSocket.OPEN) t.ws.send(JSON.stringify(msg));
}

function fitTerm(t, force) {
  if (!t || t.host.style.display === "none") return;
  try { t.fit.fit(); } catch { /* not visible yet */ }
  if (force) termSend(t, { type: "resize", cols: t.term.cols, rows: t.term.rows });
}

function activateTab(sid, focus = true) {
  S.activeSid = sid;
  for (const [id, t] of Object.entries(S.terms)) t.host.style.display = id === sid ? "" : "none";
  for (const tab of document.querySelectorAll(".tab")) tab.classList.toggle("active", tab.dataset.sid === sid);
  const t = S.terms[sid];
  if (t) { fitTerm(t, true); if (focus) t.term.focus(); }
}

function renderSessions() {
  const sessions = S.state.sessions || [];
  const ids = new Set(sessions.map((s) => s.id));
  for (const [id, t] of Object.entries(S.terms)) {
    if (!ids.has(id)) { t.ws?.close(); t.term.dispose(); t.host.remove(); delete S.terms[id]; }
  }
  const list = $("#tab-list");
  list.replaceChildren();
  for (const s of sessions) {
    ensureTerm(s);
    const tab = h("div", { class: `tab${s.exited ? " exited" : ""}`, "data-sid": s.id, title: `${s.target} ${s.os_hint || ""}`,
      onclick: () => activateTab(s.id) },
      h("span", { class: "dot" }), h("span", { class: "name" }, s.id), h("span", { class: "kind" }, s.kind),
      h("button", { class: "close", title: "Close session", onclick: (e) => { e.stopPropagation(); closeSession(s); } }, "×"));
    list.append(tab);
  }
  $("#terms-empty").classList.toggle("hidden", sessions.length > 0);
  if (!ids.has(S.activeSid)) S.activeSid = sessions.length ? sessions[sessions.length - 1].id : null;
  activateTab(S.activeSid, false);
}

async function closeSession(s) {
  if (!s.exited && !(await confirmModal("Close session", `Close ${s.id} (${s.target})? The process will be terminated.`, "Close", "danger"))) return;
  await guarded(() => api("DELETE", `/api/sessions/${encodeURIComponent(s.id)}`));
}

async function openSession(kind, host) {
  hideMenus();
  const r = await guarded(() => api("POST", "/api/sessions", { kind, host }));
  if (r) setTimeout(() => activateTab(r.id), 50);
}

function renderSessionMenu() {
  const menu = $("#session-menu");
  const hosts = S.state.config.hosts || [];
  menu.replaceChildren(
    h("button", { onclick: () => openSession("local") }, "Local shell"),
    h("div", { class: "sep" }),
    h("div", { class: "label" }, "Saved hosts"),
    ...(hosts.length ? hosts.map((x) => h("button", { onclick: () => openSession(x.kind, x.name) },
      `${x.name}  `, h("span", { class: "muted small" }, `${x.kind.toUpperCase()} ${x.user ? x.user + "@" : ""}${x.host}`)))
      : [h("div", { class: "label" }, "none yet")]),
    h("div", { class: "sep" }),
    h("button", { onclick: () => { hideMenus(); openSettings("hosts"); } }, "Manage hosts…"),
  );
}

// Text of the terminal buffer from a marker to the next marker (or the cursor).
function captureFor(item) {
  const t = S.terms[item.session_id];
  if (!t) return { text: "", error: "Session is no longer open; paste the output manually." };
  const m = t.markers.get(item.num);
  if (!m) return { text: "", error: "Not run from this window (or the page was reloaded); paste the output manually." };
  if (m.isDisposed || m.line < 0) return { text: "", error: "Output has scrolled out of the terminal buffer; paste it manually." };
  const buf = t.term.buffer.normal;
  let end = buf.baseY + buf.cursorY;
  for (const [num, other] of t.markers) {
    if (num !== item.num && !other.isDisposed && other.line > m.line && other.line - 1 < end) end = other.line - 1;
  }
  const lines = [];
  for (let i = m.line; i <= end; i++) {
    const line = buf.getLine(i);
    if (!line) continue;
    const s = line.translateToString(true);
    if (line.isWrapped && lines.length) lines[lines.length - 1] += s;
    else lines.push(s);
  }
  while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
  return { text: lines.join("\n") };
}

// ------------------------------------------------------------------ queue

const readyItems = () => (S.state?.queue || []).filter((i) => ["ran", "inserted", "skipped"].includes(i.status));

function sessionOptions(item) {
  const sessions = S.state.sessions || [];
  const opts = sessions.map((s) => h("option", { value: s.id, selected: s.id === item.session_id }, s.exited ? `${s.id} (closed)` : s.id));
  if (!sessions.some((s) => s.id === item.session_id)) {
    opts.unshift(h("option", { value: item.session_id, selected: true }, `${item.session_id || "?"} (not open)`));
  }
  return opts;
}

function renderQueue() {
  if (!S.state) return;
  const showDone = $("#show-done").checked;
  const items = S.state.queue.filter((i) => showDone || i.status !== "sent");
  const list = $("#queue-list");
  const keep = new Set(items.map((i) => i.num));
  for (const [num, row] of S.rows) if (!keep.has(num)) { row.remove(); S.rows.delete(num); }
  items.forEach((item, idx) => {
    let row = S.rows.get(item.num);
    if (!row) { row = buildRow(item); S.rows.set(item.num, row); }
    updateRow(row, item);
    if (list.children[idx] !== row) list.insertBefore(row, list.children[idx] || null);
  });
  if (!items.length) {
    if (!$("#queue-empty")) list.append(h("div", { id: "queue-empty", class: "empty" }, "No commands queued."));
  } else $("#queue-empty")?.remove();
  const pending = S.state.queue.filter((i) => i.status === "pending").length;
  const ready = readyItems().length;
  $("#queue-count").textContent = `${pending} pending · ${ready} ready to send`;
  $("#send-results-btn").textContent = ready ? `Send results (${ready})` : "Send results";
  $("#send-results-btn").disabled = !!S.state.busy || ready === 0;
}

function autosize(ta) { ta.style.height = "auto"; ta.style.height = `${ta.scrollHeight + 2}px`; }

function buildRow(item) {
  const cmd = h("textarea", { class: "cmd", rows: 1, spellcheck: "false" });
  cmd.addEventListener("input", () => autosize(cmd));
  cmd.addEventListener("change", () => saveCommand(item.num, cmd.value));
  const sel = h("select", { class: "sess", onchange: (e) => guarded(() => api("POST", `/api/queue/${item.num}`, { session_id: e.target.value })) });
  const row = h("div", { class: "qitem", "data-num": item.num },
    h("div", { class: "num" }, `#${item.num}`),
    sel,
    h("div", {}, cmd, h("div", { class: "purpose" }), h("div", { class: "note" })),
    h("div", { class: "meta" }, h("span", { class: "badge risk" }), h("span", { class: "status" })),
    h("div", { class: "actions" }));
  return row;
}

async function saveCommand(num, value) {
  const item = S.state.queue.find((i) => i.num === num);
  if (!item || item.command === value.trim()) return;
  await guarded(() => api("POST", `/api/queue/${num}`, { command: value }));
}

function updateRow(row, item) {
  const pending = item.status === "pending";
  row.className = `qitem risk-${item.risk}${item.status === "sent" ? " done" : ""}`;
  const cmd = $(".cmd", row);
  if (document.activeElement !== cmd) cmd.value = item.command;
  cmd.readOnly = !pending;
  requestAnimationFrame(() => autosize(cmd));
  const sel = $(".sess", row);
  sel.replaceChildren(...sessionOptions(item));
  sel.disabled = !pending;
  $(".purpose", row).textContent = item.purpose + (item.edited ? "  (edited)" : "");
  $(".note", row).textContent = item.note ? `Note: ${item.note}` : "";
  const badge = $(".risk", row);
  badge.className = `badge risk ${item.risk}`;
  badge.textContent = item.risk.replace("_", " ");
  badge.title = [`Model said: ${item.model_risk}`, ...item.risk_reasons.map((r) => `Local rule: ${r}`)].join("\n");
  const status = $(".status", row);
  status.className = `status ${item.status}`;
  status.textContent = { pending: "pending", ran: "ran", inserted: "inserted", skipped: "skipped", sent: "sent to AI" }[item.status];

  const actions = $(".actions", row);
  const btn = (label, fn, kind = "", title = "") => h("button", { class: `small ${kind}`, title, onclick: () => guarded(fn) }, label);
  const set = (status, extra = {}) => api("POST", `/api/queue/${item.num}`, { status, ...extra });
  const sessOpen = (S.state.sessions || []).some((s) => s.id === item.session_id && !s.exited);
  const btns = [];
  if (pending) {
    const run = btn("Run", () => runItem(item.num, "run"), item.risk === "disruptive" ? "danger" : "primary", "Type into the terminal and press Enter");
    const ins = btn("Insert", () => runItem(item.num, "insert"), "", "Type into the terminal without pressing Enter");
    run.disabled = ins.disabled = !sessOpen;
    btns.push(run, ins,
      btn("Skip", () => skipItem(item)),
      btn("↑", () => api("POST", `/api/queue/${item.num}/move`, { delta: -1 }), "ghost", "Move up"),
      btn("↓", () => api("POST", `/api/queue/${item.num}/move`, { delta: 1 }), "ghost", "Move down"));
  } else if (item.status === "ran" || item.status === "inserted") {
    btns.push(btn("Preview", () => previewCapture(item)), btn("Reset", () => set("pending"), "ghost", "Back to pending"));
  } else if (item.status === "skipped") {
    btns.push(btn("Unskip", () => set("pending", { note: "" }), "ghost"));
  }
  actions.replaceChildren(...btns);
}

function flashQueueItem(num) {
  if (!$("#show-done").checked && S.state.queue.find((i) => i.num === num)?.status === "sent") {
    $("#show-done").checked = true;
    renderQueue();
  }
  const row = S.rows.get(num);
  if (!row) return;
  row.scrollIntoView({ block: "nearest", behavior: "smooth" });
  row.classList.remove("flash");
  void row.offsetWidth;
  row.classList.add("flash");
}

async function runItem(num, mode) {
  const row = S.rows.get(num);
  const cmdEl = row && $(".cmd", row);
  if (cmdEl) await saveCommand(num, cmdEl.value);
  const item = S.state.queue.find((i) => i.num === num);
  const sess = (S.state.sessions || []).find((s) => s.id === item.session_id);
  if (!sess || sess.exited) throw new Error(`Session ${item.session_id} is not open. Pick another target.`);
  if (item.risk === "disruptive") {
    const reasons = item.risk_reasons.length ? ` (${item.risk_reasons.join(", ")})` : "";
    const ok = await confirmModal("Disruptive command",
      h("div", {}, h("p", {}, `This command is flagged DISRUPTIVE${reasons}. It may interrupt service, lose data or cut off access.`),
        h("pre", { class: "prompt-text" }, item.command), h("p", {}, `Target: ${sess.id} (${sess.target})`)),
      mode === "run" ? "Run it" : "Insert it", "danger");
    if (!ok) return;
  }
  activateTab(sess.id);
  const t = S.terms[sess.id];
  if (!t?.ws) throw new Error("Terminal is not connected.");
  t.markers.get(num)?.dispose();
  t.markers.set(num, t.term.registerMarker(0));
  t.term.paste(item.command);
  if (mode === "run") termSend(t, { type: "input", data: "\r" });
  t.term.focus();
  await api("POST", `/api/queue/${num}`, { status: mode === "run" ? "ran" : "inserted" });
}

function skipItem(item) {
  const note = h("textarea", { rows: 3, placeholder: "Why? (optional, sent to the AI) e.g. 'not allowed on production', 'already checked'" });
  modal({
    title: `Skip #${item.num}`,
    body: h("div", { class: "field" }, h("pre", { class: "prompt-text" }, item.command), note),
    buttons: [{ label: "Cancel" },
      { label: "Skip", kind: "primary", onClick: () => api("POST", `/api/queue/${item.num}`, { status: "skipped", note: note.value.trim() }) }],
  });
}

async function previewCapture(item) {
  const cap = captureFor(item);
  modal({
    title: `Captured output for #${item.num}`, wide: true,
    body: h("div", {}, cap.error ? h("div", { class: "warnbox" }, cap.error) : null,
      h("pre", { class: "prompt-text", style: "max-height:60vh;overflow:auto" }, cap.text || "(empty)")),
    buttons: [{ label: "Close" }],
  });
}

async function openSendResults() {
  const items = readyItems();
  if (!items.length) return toast("Nothing ready to send. Run or skip queue items first.");
  const caps = items.map((i) => (i.status === "skipped" ? { text: "" } : captureFor(i)));
  const prev = (await api("POST", "/api/preview", { texts: caps.map((c) => c.text) })).items;
  const blocks = items.map((item, idx) => {
    const include = h("input", { type: "checkbox", checked: true });
    const skipped = item.status === "skipped";
    const ta = skipped ? null : h("textarea", { spellcheck: "false", value: prev[idx].text });
    const note = h("input", { type: "text", placeholder: "Note to the AI (optional)", value: item.note || "" });
    const info = [];
    if (prev[idx].redactions) info.push(`${prev[idx].redactions} redaction(s) applied`);
    if (prev[idx].truncated) info.push("truncated");
    const block = h("div", { class: "result-block" },
      h("label", { class: "head" }, include, h("b", {}, `#${item.num}`),
        h("span", { class: `status ${item.status}` }, item.status.toUpperCase()),
        h("span", { class: "muted" }, item.session_id), h("code", { class: "mono" }, item.command)),
      caps[idx].error ? h("div", { class: "warnbox" }, caps[idx].error) : null,
      ta, info.length ? h("div", { class: "muted small" }, info.join(" · ")) : null, note);
    include.addEventListener("change", () => block.classList.toggle("excluded", !include.checked));
    if (ta) setTimeout(() => autosize(ta), 30);
    return { item, include, ta, note };
  });
  const message = h("textarea", { rows: 2, placeholder: "Add a message for the AI (optional)" });
  modal({
    title: "Review results before sending", wide: true,
    body: h("div", { style: "display:flex;flex-direction:column;gap:10px" },
      h("div", { class: "muted small" }, "This exact text is what the AI receives. Edit or untick anything that shouldn't be shared."),
      ...blocks.map((b) => b.include.closest(".result-block")), h("div", { class: "field" }, h("span", {}, "Message"), message)),
    buttons: [{ label: "Cancel" }, {
      label: "Send to AI", kind: "primary", onClick: async () => {
        const results = blocks.filter((b) => b.include.checked)
          .map((b) => ({ num: b.item.num, text: b.ta ? b.ta.value : "", note: b.note.value.trim() }));
        if (!results.length && !message.value.trim()) throw new Error("Nothing selected.");
        await api("POST", "/api/send", { message: message.value.trim(), results });
      },
    }],
  });
}

async function sendSelection() {
  const t = S.terms[S.activeSid];
  const sel = t?.term.getSelection() || "";
  if (!sel.trim()) return toast("Select some text in the terminal first.");
  const prev = (await api("POST", "/api/preview", { texts: [sel] })).items[0];
  const ta = h("textarea", { spellcheck: "false", class: "mono", rows: 12, value: prev.text });
  const message = h("textarea", { rows: 2, placeholder: "Add a message for the AI (optional)" });
  modal({
    title: `Send terminal excerpt from ${S.activeSid}`, wide: true,
    body: h("div", { style: "display:flex;flex-direction:column;gap:8px" },
      prev.redactions || prev.truncated ? h("div", { class: "muted small" },
        [prev.redactions ? `${prev.redactions} redaction(s) applied` : "", prev.truncated ? "truncated" : ""].filter(Boolean).join(" · ")) : null,
      ta, h("div", { class: "field" }, h("span", {}, "Message"), message)),
    buttons: [{ label: "Cancel" }, {
      label: "Send to AI", kind: "primary",
      onClick: () => api("POST", "/api/send", { message: message.value.trim(), snippets: [{ session_id: S.activeSid, text: ta.value }] }),
    }],
  });
}

// ------------------------------------------------------------------ credential prompts

function showCredentialPrompt(p) {
  const yesNo = /\(yes\/no/i.test(p.text);
  const input = yesNo ? null : h("input", { type: p.secret ? "password" : "text", autocomplete: "off" });
  const save = p.can_save ? h("input", { type: "checkbox" }) : null;
  let answered = false;
  const answer = (value, doSave = false) => {
    answered = true;
    return api("POST", `/api/prompts/${p.id}`, { answer: value, save: doSave });
  };
  const m = modal({
    title: `Session ${p.session_id} is asking`,
    dismissable: false,
    body: h("div", { style: "display:flex;flex-direction:column;gap:8px" },
      h("div", { class: "prompt-text" }, p.text),
      yesNo ? h("div", { class: "muted small" }, "Verify the fingerprint with the device owner or console before accepting.") : null,
      input, save ? h("label", { class: "check" }, save, "Save to keyring for this host") : null),
    buttons: yesNo
      ? [{ label: "Reject", onClick: () => answer("no") }, { label: "Accept", kind: "primary", onClick: () => answer("yes") }]
      : [{ label: "Cancel", onClick: () => answer(null) },
        { label: "OK", kind: "primary", onClick: () => answer(input.value, save?.checked) }],
    onClose: () => { if (!answered) api("POST", `/api/prompts/${p.id}`, { answer: null }).catch(() => {}); },
  });
  input?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); m.box.querySelector(".buttons button.primary").click(); }
  });
  S.promptModals[p.id] = m;
}

// ------------------------------------------------------------------ case

function openCaseModal(first) {
  const st = S.state;
  const name = h("input", { type: "text", placeholder: "e.g. TKT-1042 Acme file server slow" });
  const notes = h("textarea", { rows: 3, placeholder: "Site/client notes for the AI (optional): environment, known quirks, what's been tried…" });
  const opt = (value, title, desc, checked) => h("label", { class: "radio-card" },
    h("input", { type: "radio", name: "sens", value, checked }),
    h("div", {}, h("div", {}, h("span", { class: `badge ${value}` }, value), " ", title), h("div", { class: "desc" }, desc)));
  const body = h("div", { style: "display:flex;flex-direction:column;gap:10px", class: "case-modal" },
    h("label", { class: "field" }, h("span", {}, "Case name / ticket"), name),
    h("div", { class: "field" }, h("span", {}, "Sensitivity"),
      opt("open", "Trial / non-confidential", "Any model, including Standard-tier cloud models (e.g. Claude Opus) and TEE models whose prompts pass the provider's gateway in the clear.", true),
      opt("confidential", "Client data involved", "Only end-to-end encrypted models (sealed to an attested enclave, e.g. private/glm-5-3) or local models.", false),
      opt("sovereign", "Data must not leave this network", "Only local models.", false)),
    h("label", { class: "field" }, h("span", {}, "Notes"), notes),
    st.case && st.chat.length ? h("div", { class: "muted small" }, "The current case's log stays on disk. Open sessions carry over; the conversation and queue start fresh.") : null);
  modal({
    title: first ? "Start a case" : "New case",
    dismissable: !first,
    body,
    buttons: [first ? null : { label: "Cancel" }, {
      label: "Start case", kind: "primary", onClick: async () => {
        const sensitivity = body.querySelector("input[name=sens]:checked").value;
        await api("POST", "/api/case", { name: name.value.trim(), sensitivity, notes: notes.value.trim() });
      },
    }].filter(Boolean),
  });
}

// ------------------------------------------------------------------ model picker

async function openModelPicker() {
  const st = S.state;
  const providers = st.config.providers;
  if (!providers.length) {
    toast("Add an AI provider first.");
    return openSettings("providers");
  }
  let prov = st.config.active_provider || providers[0].name;
  let models = [];
  const search = h("input", { type: "text", placeholder: "Filter models… (e.g. opus, glm, tee)" });
  const provSel = h("select", {}, providers.map((p) => h("option", { value: p.name, selected: p.name === prov }, p.name)));
  const listEl = h("div", { class: "model-list" }, h("div", { class: "muted" }, "Loading…"));
  const sens = st.case?.sensitivity || "open";
  const legend = {
    open: "Open case: all tiers allowed.",
    confidential: "Confidential case: only E2EE (private/…) and Local models are allowed.",
    sovereign: "Sovereign case: only Local models are allowed.",
  }[sens];
  let m;
  const render = () => {
    const q = search.value.trim().toLowerCase();
    const recent = st.config.recent_models.filter((r) => r.startsWith(prov + "|")).map((r) => r.split("|")[1]);
    const shown = models.filter((x) => !q || x.id.toLowerCase().includes(q) || x.tier.includes(q));
    shown.sort((a, b) => (b.allowed - a.allowed) || ((recent.indexOf(a.id) + 1 || 99) - (recent.indexOf(b.id) + 1 || 99)) || a.id.localeCompare(b.id));
    listEl.replaceChildren(...shown.slice(0, 400).map((x) => h("div", {
      class: `model-row${x.allowed ? "" : " blocked"}${x.id === st.config.active_model && prov === st.config.active_provider ? " current" : ""}`,
      title: x.allowed ? x.id : `Not permitted for a ${sens} case`,
      onclick: async () => {
        if (!x.allowed) return;
        await guarded(async () => { await api("POST", "/api/model", { provider: prov, model: x.id }); m.close(); });
      },
    }, h("span", { class: "id" }, x.id), recent.includes(x.id) ? h("span", { class: "muted small" }, "recent") : null,
    h("span", { class: `badge ${x.tier}` }, x.tier))));
    if (!shown.length) listEl.append(h("div", { class: "muted" }, "No matching models."));
  };
  const load = async (refresh) => {
    listEl.replaceChildren(h("div", { class: "muted" }, "Loading…"));
    try {
      models = (await api("GET", `/api/models?provider=${encodeURIComponent(prov)}&refresh=${refresh ? "true" : "false"}`)).models;
      render();
    } catch (e) {
      listEl.replaceChildren(h("div", { class: "warnbox" }, e.message));
    }
  };
  search.addEventListener("input", render);
  provSel.addEventListener("change", () => { prov = provSel.value; load(false); });
  m = modal({
    title: "Choose model", wide: true,
    body: h("div", { style: "display:flex;flex-direction:column;gap:8px" },
      h("div", { class: "muted small" }, legend, " Tier is detected from the model id; override it in Settings → Providers."),
      h("div", { class: "row" }, providers.length > 1 ? provSel : null, search,
        h("button", { type: "button", onclick: () => load(true) }, "Refresh")),
      listEl),
    buttons: [{ label: "Close" }],
  });
  load(false);
}

// ------------------------------------------------------------------ settings

function openSettings(tab = "providers") {
  const tabs = h("div", { class: "tabs-inline" });
  const pane = h("div", { style: "display:flex;flex-direction:column;gap:10px" });
  const show = (name) => {
    for (const b of tabs.children) b.classList.toggle("active", b.dataset.tab === name);
    pane.replaceChildren(({ providers: providersPane, hosts: hostsPane, general: generalPane })[name]());
  };
  for (const [key, label] of [["providers", "AI providers"], ["hosts", "Hosts"], ["general", "General"]]) {
    tabs.append(h("button", { type: "button", "data-tab": key, onclick: () => show(key) }, label));
  }
  const m = modal({ title: "Settings", wide: true, body: h("div", {}, pane), buttons: [{ label: "Close" }] });
  m.box.insertBefore(tabs, m.box.querySelector(".content"));
  show(tab);

  function providersPane() {
    const provs = S.state.config.providers;
    const list = h("div", { class: "list" }, provs.length ? provs.map((p) => h("div", { class: "list-item" },
      h("div", { class: "grow" }, h("b", {}, p.name), "  ", h("span", { class: "muted small" }, p.base_url)),
      h("span", { class: "small " + (p.has_key ? "" : "muted") }, p.has_key ? "🔑 key in keyring" : "no key"),
      h("button", { class: "small", onclick: () => pane.replaceChildren(providerForm(p)) }, "Edit"),
      h("button", { class: "small danger", onclick: async () => {
        if (await confirmModal("Delete provider", `Delete ${p.name} and its stored API key?`, "Delete", "danger")) {
          await guarded(() => api("DELETE", `/api/providers/${encodeURIComponent(p.name)}`));
          show("providers");
        }
      } }, "Delete"))) : h("div", { class: "muted" }, "No providers yet."));
    return h("div", { style: "display:flex;flex-direction:column;gap:10px" }, list,
      h("div", { class: "row" },
        h("button", { class: "primary", onclick: () => pane.replaceChildren(providerForm({ name: "NanoGPT", base_url: "https://nano-gpt.com/api/v1", _new: true })) }, "Add NanoGPT"),
        h("button", { onclick: () => pane.replaceChildren(providerForm({ name: "Local", base_url: "http://localhost:11434/v1", _new: true })) }, "Add local (Ollama / LM Studio / vLLM)"),
        h("button", { onclick: () => pane.replaceChildren(providerForm({ name: "", base_url: "", _new: true })) }, "Add other")));
  }

  function providerForm(p) {
    const name = h("input", { type: "text", value: p.name });
    const url = h("input", { type: "text", value: p.base_url });
    const key = h("input", { type: "password", autocomplete: "off",
      placeholder: p.has_key ? "Stored in keyring — leave blank to keep" : "Paste API key (stored in the OS keyring)" });
    const def = h("input", { type: "text", value: p.default_model || "", placeholder: "Filled automatically on Test (Claude Opus 5.5 if available)" });
    const overrides = h("textarea", { rows: 3, class: "mono",
      value: Object.entries(p.tier_overrides || {}).map(([k, v]) => `${k} = ${v}`).join("\n"),
      placeholder: "One per line: model-id = standard | tee | local" });
    const status = h("div", { class: "muted small" });
    const payload = () => {
      const tier_overrides = {};
      for (const line of overrides.value.split("\n")) {
        const [k, v] = line.split("=").map((s) => (s || "").trim());
        if (k && v) tier_overrides[k] = v.toLowerCase();
      }
      return { provider: { name: name.value.trim(), base_url: url.value.trim(), default_model: def.value.trim(), tier_overrides },
        api_key: key.value.trim() || null, original_name: p._new ? null : p.name };
    };
    const save = async () => {
      const body = payload();
      await api("POST", "/api/providers", body);
      p = { ...body.provider, has_key: p.has_key || !!body.api_key };
      key.value = "";
      return body.provider.name;
    };
    return h("div", { style: "display:flex;flex-direction:column;gap:10px" },
      h("div", { class: "row" }, h("label", { class: "field" }, h("span", {}, "Name"), name),
        h("label", { class: "field", style: "flex:2" }, h("span", {}, "Base URL (OpenAI-compatible)"), url)),
      h("label", { class: "field" }, h("span", {}, "API key"), key),
      h("label", { class: "field" }, h("span", {}, "Default model"), def),
      h("label", { class: "field" }, h("span", {}, "Tier overrides"), overrides),
      h("div", { class: "muted small" }, "Tiers: STANDARD = normal cloud; TEE = runs in an enclave but the prompt passes the provider's gateway in the clear (TEE/, phala/); E2EE = sealed on this machine to an attested enclave (NanoGPT private/… models, attested with Tinfoil's verifier); LOCAL = your own hardware (localhost/private IP URLs)."),
      status,
      h("div", { class: "row" },
        h("button", { onclick: () => show("providers") }, "Back"),
        h("span", { class: "spacer" }),
        h("button", { onclick: () => guarded(async () => {
          const n = await save();
          status.textContent = "Testing…";
          try {
            const models = (await api("GET", `/api/models?provider=${encodeURIComponent(n)}&refresh=true`)).models;
            const fresh = S.state.config.providers.find((x) => x.name === n);
            if (fresh?.default_model && !def.value) def.value = fresh.default_model;
            status.textContent = `✓ Connected: ${models.length} models available.` + (def.value ? ` Default: ${def.value}` : "");
            p._new = false;
          } catch (e) { status.textContent = `✗ ${e.message}`; }
        }) }, "Save & test"),
        h("button", { class: "primary", onclick: () => guarded(async () => {
          await save();
          toast("Provider saved.", "ok");
          show("providers");
        }) }, "Save")));
  }

  function hostsPane() {
    const hosts = S.state.config.hosts;
    const list = h("div", { class: "list" }, hosts.length ? hosts.map((x) => h("div", { class: "list-item" },
      h("div", { class: "grow" }, h("b", {}, x.name), "  ",
        h("span", { class: "muted small" }, `${x.kind.toUpperCase()} ${x.user ? x.user + "@" : ""}${x.host}${x.port ? ":" + x.port : ""} · ${x.auth}${x.os_hint ? " · " + x.os_hint : ""}`)),
      x.has_password ? h("span", { class: "small" }, "🔑") : null,
      h("button", { class: "small", onclick: () => pane.replaceChildren(hostForm(x)) }, "Edit"),
      h("button", { class: "small danger", onclick: async () => {
        if (await confirmModal("Delete host", `Delete ${x.name} and any stored password?`, "Delete", "danger")) {
          await guarded(() => api("DELETE", `/api/hosts/${encodeURIComponent(x.name)}`));
          show("hosts");
        }
      } }, "Delete"))) : h("div", { class: "muted" }, "No saved hosts yet."));
    return h("div", { style: "display:flex;flex-direction:column;gap:10px" }, list,
      h("div", { class: "row" },
        h("button", { class: "primary", onclick: () => pane.replaceChildren(hostForm({ kind: "ssh", auth: "agent", _new: true })) }, "Add SSH host"),
        h("button", { onclick: () => pane.replaceChildren(hostForm({ kind: "winrm", auth: "ntlm", winrm_ssl: true, winrm_cert_validation: true, _new: true })) }, "Add WinRM host")));
  }

  function hostForm(x) {
    const ssh = x.kind === "ssh";
    const f = {
      name: h("input", { type: "text", value: x.name || "", placeholder: "e.g. acme-fs01" }),
      host: h("input", { type: "text", value: x.host || "", placeholder: "hostname or IP" }),
      port: h("input", { type: "number", value: x.port || "", placeholder: ssh ? "22" : (x.winrm_ssl === false ? "5985" : "5986") }),
      user: h("input", { type: "text", value: x.user || "", placeholder: ssh ? "username" : "DOMAIN\\user or user@domain" }),
      auth: h("select", {}, (ssh ? [["agent", "SSH agent / default keys"], ["key", "Key file"], ["password", "Password"]]
        : [["ntlm", "NTLM"], ["kerberos", "Kerberos"], ["negotiate", "Negotiate"], ["basic", "Basic (HTTPS only!)"]])
        .map(([v, l]) => h("option", { value: v, selected: v === x.auth }, l))),
      password: h("input", { type: "password", autocomplete: "off",
        placeholder: x.has_password ? "Stored in keyring — leave blank to keep" : "Optional; stored in the OS keyring. Leave blank to be asked each time." }),
      key_file: h("input", { type: "text", value: x.key_file || "", placeholder: "~/.ssh/id_ed25519" }),
      jump: h("input", { type: "text", value: x.jump || "", placeholder: "user@bastion (optional)" }),
      ssh_options: h("textarea", { rows: 2, class: "mono", value: (x.ssh_options || []).join("\n"),
        placeholder: "One -o option per line, e.g. KexAlgorithms=+diffie-hellman-group14-sha1" }),
      winrm_ssl: h("input", { type: "checkbox", checked: x.winrm_ssl !== false }),
      winrm_cert_validation: h("input", { type: "checkbox", checked: x.winrm_cert_validation !== false }),
      os_hint: h("input", { type: "text", value: x.os_hint || "", placeholder: ssh ? "e.g. Ubuntu 24.04 / Cisco IOS-XE / RouterOS 7" : "e.g. Windows Server 2022" }),
    };
    const field = (label, el, style) => h("label", { class: "field", style }, h("span", {}, label), el);
    return h("div", { style: "display:flex;flex-direction:column;gap:10px" },
      h("div", { class: "row" }, field("Name", f.name), field("Host", f.host, "flex:2"), field("Port", f.port)),
      h("div", { class: "row" }, field("User", f.user), field(ssh ? "Authentication" : "Auth method", f.auth)),
      field("Password", f.password),
      ssh ? h("div", { class: "row" }, field("Key file", f.key_file), field("Jump host", f.jump)) : null,
      ssh ? field("Extra SSH options", f.ssh_options) : null,
      ssh ? null : h("div", { class: "row" },
        h("label", { class: "check" }, f.winrm_ssl, "HTTPS (5986)"),
        h("label", { class: "check" }, f.winrm_cert_validation, "Validate certificate")),
      field("OS / device hint for the AI", f.os_hint),
      x.has_password ? h("button", { class: "small", style: "align-self:flex-start", onclick: () => guarded(async () => {
        await api("POST", `/api/hosts/${encodeURIComponent(x.name)}/forget-password`);
        toast("Password removed from keyring.", "ok");
      }) }, "Forget stored password") : null,
      h("div", { class: "row" },
        h("button", { onclick: () => show("hosts") }, "Back"), h("span", { class: "spacer" }),
        h("button", { class: "primary", onclick: () => guarded(async () => {
          await api("POST", "/api/hosts", {
            host: {
              name: f.name.value.trim(), kind: x.kind, host: f.host.value.trim(), port: f.port.value ? Number(f.port.value) : null,
              user: f.user.value.trim(), auth: f.auth.value, key_file: f.key_file.value.trim(), jump: f.jump.value.trim(),
              ssh_options: f.ssh_options.value.split("\n"), winrm_ssl: f.winrm_ssl.checked,
              winrm_cert_validation: f.winrm_cert_validation.checked, os_hint: f.os_hint.value.trim(),
            },
            password: f.password.value || null, original_name: x._new ? null : x.name,
          });
          toast("Host saved.", "ok");
          show("hosts");
        }) }, "Save")));
  }

  function generalPane() {
    const s = S.state.config.settings;
    const num = (v) => h("input", { type: "number", value: v });
    const f = { capture_max_lines: num(s.capture_max_lines), capture_max_chars: num(s.capture_max_chars),
      scrollback: num(s.scrollback), font_size: num(s.font_size) };
    return h("div", { style: "display:flex;flex-direction:column;gap:10px" },
      h("div", { class: "row" },
        h("label", { class: "field" }, h("span", {}, "Max lines per result"), f.capture_max_lines),
        h("label", { class: "field" }, h("span", {}, "Max characters per result"), f.capture_max_chars)),
      h("div", { class: "row" },
        h("label", { class: "field" }, h("span", {}, "Terminal scrollback (new sessions)"), f.scrollback),
        h("label", { class: "field" }, h("span", {}, "Terminal font size (new sessions)"), f.font_size)),
      h("div", { class: "muted small" }, `Case logs are stored under ${S.state.case ? S.state.case.dir.replace(/\/[^/]+$/, "") : "~/.local/share/datoolkit/cases"}.`),
      h("div", { class: "row" }, h("span", { class: "spacer" }), h("button", { class: "primary", onclick: () => guarded(async () => {
        await api("POST", "/api/settings", Object.fromEntries(Object.entries(f).map(([k, el]) => [k, Number(el.value)])));
        toast("Settings saved.", "ok");
      }) }, "Save")));
  }
}

// ------------------------------------------------------------------ export

async function doExport(act) {
  hideMenus();
  if (act === "markdown") {
    const r = await guarded(() => api("POST", "/api/export/markdown"));
    if (r) toast(`Transcript saved: ${r.path}`, "ok", 10000);
  } else if (act === "folder") {
    await guarded(() => api("POST", "/api/open-folder"));
  } else if (act === "summary") {
    const m = modal({ title: "Ticket summary", wide: true, body: h("div", { class: "muted" }, h("span", { class: "spinner" }), " Asking the AI for a summary…"), buttons: [{ label: "Close" }] });
    try {
      const r = await api("POST", "/api/export/summary");
      const ta = h("textarea", { rows: 18, value: r.text });
      m.box.querySelector(".content").replaceChildren(ta, h("div", { class: "muted small" }, `Saved to ${r.path}`),
        h("button", { class: "primary", style: "align-self:flex-start", onclick: () => { clipWrite(ta.value); toast("Copied.", "ok"); } }, "Copy"));
    } catch (e) {
      m.box.querySelector(".content").replaceChildren(h("div", { class: "warnbox" }, e.message));
    }
  }
}

// ------------------------------------------------------------------ layout & wiring

function hideMenus() { for (const m of document.querySelectorAll(".menu")) m.classList.add("hidden"); }

function toggleMenu(menu) {
  const wasHidden = menu.classList.contains("hidden");
  hideMenus();
  if (wasHidden) menu.classList.remove("hidden");
}

function setupSplitters() {
  const root = document.documentElement;
  const chatW = store.get("dat-chat-w", null);
  const queueH = store.get("dat-queue-h", null);
  if (chatW) root.style.setProperty("--chat-w", chatW);
  if (queueH) root.style.setProperty("--queue-h", queueH);
  const drag = (el, onMove, key, prop) => el.addEventListener("mousedown", (e) => {
    e.preventDefault();
    const move = (ev) => { root.style.setProperty(prop, onMove(ev)); fitTerm(S.terms[S.activeSid]); };
    const up = () => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      store.set(key, root.style.getPropertyValue(prop));
      fitTerm(S.terms[S.activeSid], true);
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
  drag($("#split-v"), (ev) => `${Math.min(Math.max(ev.clientX, 280), window.innerWidth - 320)}px`, "dat-chat-w", "--chat-w");
  drag($("#split-h"), (ev) => `${Math.min(Math.max(window.innerHeight - ev.clientY, 90), window.innerHeight - 250)}px`, "dat-queue-h", "--queue-h");
  let pending = null;
  new ResizeObserver(() => {
    clearTimeout(pending);
    pending = setTimeout(() => fitTerm(S.terms[S.activeSid], true), 60);
  }).observe($("#terms"));
}

function init() {
  setupSplitters();
  $("#chat-form").addEventListener("submit", sendChat);
  $("#chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) sendChat(e);
  });
  $("#stop-btn").addEventListener("click", () => guarded(() => api("POST", "/api/stop")));
  $("#case-btn").addEventListener("click", () => openCaseModal(false));
  $("#model-btn").addEventListener("click", openModelPicker);
  $("#attest-btn").addEventListener("click", () => guarded(async () => {
    const r = await api("POST", "/api/attest");
    toast(r.status === "verified" ? `Enclave attested: ${r.summary}` : `Attestation failed: ${r.error}`, r.status === "verified" ? "ok" : "error", 10000);
  }));
  $("#settings-btn").addEventListener("click", () => openSettings());
  $("#export-btn").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu($("#export-menu")); });
  $("#export-menu").addEventListener("click", (e) => { const a = e.target.closest("button")?.dataset.act; if (a) doExport(a); });
  $("#new-session-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    if (!S.state.case) return toast("Start a case first.");
    renderSessionMenu();
    toggleMenu($("#session-menu"));
  });
  document.addEventListener("click", (e) => { if (!e.target.closest(".menu")) hideMenus(); });
  $("#send-results-btn").addEventListener("click", () => guarded(openSendResults));
  $("#send-selection-btn").addEventListener("click", () => guarded(sendSelection));
  $("#show-done").addEventListener("change", renderQueue);
  connectEvents();
}

init();
