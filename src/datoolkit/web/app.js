"use strict";
/* DAToolkit frontend. The AI never reaches a terminal from here except through the
   technician clicking Run/Insert on a queue item. */

// ------------------------------------------------------------------ token & helpers

// In the app window the server reaches GTK for the clipboard and the Save dialog (see app.Desktop).
const DESKTOP = (() => {
  const fromUrl = new URLSearchParams(location.search).get("desktop") === "1";
  try {
    if (fromUrl) sessionStorage.setItem("dat-desktop", "1");
    return fromUrl || sessionStorage.getItem("dat-desktop") === "1";
  } catch { return fromUrl; }
})();

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
  rdps: {},           // sid -> {client, keyboard, host, view, state, clipboard, typedOk}
  activeSid: null,
  streaming: null,    // {text, reasoning, model, tier}
  promptModals: {},   // prompt id -> modal
  rows: new Map(),    // queue num -> row element
  chatHistory: [],    // messages typed by the technician this session (up-arrow recall)
  histIdx: -1,
  histDraft: "",
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

// AI output is untrusted (it can be steered by prompt injection in command output), so it
// must not be able to load remote resources: no images/media/embeds, and links open outside.
const MD_OPTS = {
  FORBID_TAGS: ["img", "picture", "source", "svg", "math", "video", "audio", "iframe", "object", "embed", "form", "input", "button", "style", "link", "meta", "base"],
  FORBID_ATTR: ["style", "srcset", "poster", "background", "ping", "formaction"],
  ALLOWED_URI_REGEXP: /^(?:https?|mailto):/i,
};
DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A") { node.setAttribute("target", "_blank"); node.setAttribute("rel", "noopener noreferrer"); }
});
function md(text) {
  return DOMPurify.sanitize(marked.parse(text || "", { breaks: true }), MD_OPTS);
}

const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* ignore */ } },
};

async function clipWrite(text) {
  try {
    if (DESKTOP) return await api("POST", "/api/desktop/clipboard", { text });
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = h("textarea", { value: text });
    document.body.append(ta); ta.select(); document.execCommand("copy"); ta.remove();
  }
}

async function clipRead() {
  if (DESKTOP) return (await api("GET", "/api/desktop/clipboard")).text || "";
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
      if (S.state.busy && S.state.search_requests?.length && !S.streaming) {
        S.streaming = { text: "", reasoning: "", phase: "tool", tool: "web_search", startAt: Date.now(), lastAt: Date.now(),
          searches: S.state.search_requests };
      }
      renderAll();
      break;
    case "companion":
      S.phones = ev.phones;
      renderPhoneBtn();
      S.phoneDialog?.refresh();
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
      S.streaming = { text: "", reasoning: "", model: ev.model, tier: ev.tier, phase: "waiting", tool: "",
        startAt: Date.now(), lastAt: Date.now() };
      S.lastTurn = null;
      renderBusy();
      renderChat(true);
      break;
    case "delta":
      if (!S.streaming) break;
      S.streaming.lastAt = Date.now();
      if (ev.kind === "tool") {
        S.streaming.phase = "tool";
        S.streaming.tool = ev.name;
        renderStatus();
        break;
      }
      S.streaming.phase = ev.kind === "text" ? "writing" : "reasoning";
      S.streaming[ev.kind === "text" ? "text" : "reasoning"] += ev.text;
      scheduleStreamRender();
      break;
    case "turn_end":
      S.state.busy = false;
      S.lastTurn = { error: ev.error, secs: S.streaming ? Math.round((Date.now() - S.streaming.startAt) / 1000) : null };
      S.streaming = null;
      S.state.chat = ev.chat;
      S.state.can_retry = !!ev.can_retry;
      S.state.last_error = ev.error || null;
      if (ev.usage) S.state.last_usage = ev.usage;
      if (document.hidden) document.title = ev.error ? "✕ DAToolkit" : "✓ DAToolkit: your turn";
      renderBusy();
      renderChat(true);
      renderUsage();
      if (ev.error && ev.error !== "stopped") toast(ev.error.startsWith("not sent: ") ? `AI request ${ev.error}` : `AI request failed: ${ev.error}`, "error", 12000);
      break;
    case "search":
      if (!S.streaming) break;
      S.streaming.searches = S.streaming.searches || [];
      {
        const i = S.streaming.searches.findIndex((r) => r.id === ev.search.id);
        if (i >= 0) S.streaming.searches[i] = ev.search; else S.streaming.searches.push(ev.search);
      }
      S.streaming.lastAt = Date.now();
      scheduleStreamRender();
      renderStatus();
      break;
    case "toast":
      toast(ev.text, ev.level || "info", 10000);
      break;
    case "hypotheses":
      S.state.hypotheses = ev.items;
      renderHypotheses();
      break;
    case "similar":
      S.state.similar_cases = ev.cases;
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
  const lab = attestLabel(att);
  ab.classList.toggle("hidden", !lab);
  if (lab) {
    ab.className = `ghost small ${lab.cls}`;
    ab.textContent = lab.text;
    ab.title = lab.title + (att.status === "verified" || att.status === "failed" ? "\nClick to re-check." : "");
  }
  const banner = $("#keyring-banner");
  banner.textContent = st.keyring_error ? `⚠ ${st.keyring_error} API keys and passwords cannot be stored.` : "";
  banner.classList.toggle("hidden", !st.keyring_error);
}

// Attestation state of a model, as short text and a CSS class (chat model and vision helper).
// TEE models: Intel TDX + NVIDIA attestation (llm/tee.py); "partial" names what couldn't be checked.
const TEE_CLEAR = "A TEE model runs in an attested enclave, but prompts still pass NanoGPT's gateway in the clear; only private/ models are end-to-end encrypted.";
function attestLabel(att) {
  if (!att) return null;
  const tee = att.kind === "tee";
  if (att.status === "checking") return { text: tee ? "TEE attesting…" : "attesting…", cls: "",
    title: tee ? "Verifying the enclave: Intel TDX quote, Intel's revocation lists and TCB, NVIDIA's GPU verdict…" : "Verifying the enclave…" };
  if (att.status === "failed") return { text: tee ? "⚠ TEE attestation refused" : "⚠ attestation failed", cls: "bad", title: `${att.error}\nNothing is sent to it until attestation succeeds.` };
  if (tee) {
    const partial = att.level === "partial";
    return { text: partial ? "🛡 TEE attested (partial)" : "🛡 TEE attested", cls: partial ? "warn" : "ok",
      title: `${att.summary}.\n${att.detail}.` + (att.signing_address ? `\nReply-signing key: ${att.signing_address}` : "") + `\n${TEE_CLEAR}` };
  }
  return { text: att.latest_release ? "🔐 attested (older release)" : "🔐 attested", cls: "ok",
    title: `End-to-end encrypted to an attested enclave.\n${att.summary}\nKey sha256: ${(att.hpke_key_sha256 || "").slice(0, 16)}…` };
}

// A TEE reply: attested before it was sent; was the reply signed by that enclave's key?
const TEE_SIGNATURE = {
  checking: ["checking signature…", "", "Asking NanoGPT for this reply's signature…"],
  signed: ["✔ signed", "ok", "NanoGPT holds a record of this reply's id signed by the attested enclave's key. The record hashes the request and reply as NanoGPT's gateway saw them, so it shows the enclave answered, not that every byte you see is what it wrote."],
  failed: ["✖ signature mismatch", "bad", "The reply's signature was NOT made by the attested enclave's key. Treat this reply with suspicion and re-check the attestation."],
  unsigned: ["unsigned", "", "This model's provider signs no replies; every instance that could answer was attested before sending instead."],
  unchecked: ["signature unchecked", "warn", "The signature couldn't be fetched (the provider kept none, or it was out of reach). That is not a failed check."],
};
function teeBadge(e) {
  const [text, cls, why] = TEE_SIGNATURE[e.tee_signature] || ["", "", ""];
  return h("span", { class: `tee-badge ${cls}`, title: `Attested before sending: ${e.tee_attested}.${why ? `\n\nReply: ${why}` : ""}\n\n${TEE_CLEAR}` },
    "🛡 TEE", text ? ` · ${text}` : "");
}

// Can images be used with the current model? native: it reads them; helper: a vision model
// describes them first (slower); none: image features are disabled, with the reason.
function visionState() { return S.state?.vision || { mode: "none", why: "Choose a model first." }; }

function renderVision() {
  const v = visionState();
  const b = $("#vision-btn");
  const hl = v.mode === "helper" ? attestLabel(S.state.helper_attestation) : null;
  b.textContent = { native: "👁", helper: `👁 via ${v.helper}${hl ? ` · ${hl.text}` : ""}`, none: "no images" }[v.mode];
  b.className = `ghost small vision ${v.mode}${hl ? ` att-${hl.cls}` : ""}`;
  b.title = v.mode === "native" ? `${v.model} reads images directly.`
    : v.mode === "helper" ? `${v.model} can't read images: ${v.helper} describes each image first and ${v.model} gets the description. Replies with images take longer. Change in Settings → Model.${hl ? `\n\nVision helper: ${hl.title}` : ""}`
    : `${v.why}\nImage features are off. Choose a vision model, or set a vision helper in Settings → Model.`;
  for (const el of document.querySelectorAll("[data-needs-vision]")) {
    if (!el.dataset.title) el.dataset.title = el.title;
    el.disabled = v.mode === "none";
    el.title = v.mode === "none" ? `Not available: ${v.why}` : v.mode === "helper"
      ? `${el.dataset.title}\n(${v.helper} will describe it for ${v.model}; this takes a little longer.)` : el.dataset.title;
  }
}

function needVision() {
  const v = visionState();
  if (v.mode === "none") throw new Error(`${v.why} Choose a vision model, or set a vision helper in Settings → Model.`);
  if (v.mode === "helper" && !S.helperNoticeShown) {
    S.helperNoticeShown = true;
    toast(`${v.model} can't see images, so ${v.helper} will describe them first. Replies with images take longer.`, "info", 9000);
  }
}

function fmtTokens(n) { return n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n); }

function renderUsage() {
  const el = $("#usage");
  const u = S.state?.last_usage;
  if (!u || !u.prompt_tokens) { el.textContent = ""; el.className = "muted small"; el.title = ""; return; }
  const warn = S.state.config.settings.context_warn_tokens || 100000;
  const p = u.prompt_tokens;
  el.textContent = `ctx ${fmtTokens(p)}`;
  el.className = `muted small usage${p >= warn * 1.5 ? " bad" : p >= warn ? " warn" : ""}`;
  el.title = `Last request: ${p.toLocaleString()} prompt tokens, ${(u.completion_tokens || 0).toLocaleString()} completion tokens.`
    + (p >= warn ? `\nThe conversation is getting long (warning threshold ${warn.toLocaleString()} in Settings → General). Consider a new case or a ticket summary.` : "");
}

function renderAll() {
  renderTop();
  renderVision();
  renderUsage();
  renderHypotheses();
  renderChat(true);
  renderSessions();
  renderQueue();
  renderBusy();
  for (const p of S.state.prompts || []) if (!S.promptModals[p.id]) showCredentialPrompt(p);
  if (!S.state.case && !document.querySelector(".case-modal")) openCaseModal(true);
}

// ------------------------------------------------------------------ hypothesis board

function renderHypotheses() {
  const box = $("#hyp-board");
  const items = S.state?.hypotheses || [];
  box.classList.toggle("hidden", !items.length);
  if (!items.length) return;
  const mark = (id, m) => guarded(() => api("POST", `/api/hypotheses/${encodeURIComponent(id)}/mark`, { mark: m }));
  $("#hyp-list").replaceChildren(...items.map((x) => {
    const pinned = x.tech_mark === "pinned", killed = x.tech_mark === "ruled_out" || x.status === "ruled_out";
    return h("div", { class: `hyp${killed ? " dead" : ""}${pinned ? " pinned" : ""}`, title: x.evidence || "" },
      h("span", { class: `badge ${x.status === "supported" ? "read_only" : x.status === "ruled_out" ? "" : "modifying"}` }, x.status.replace("_", " ")),
      h("span", { class: "text" }, x.text),
      h("div", { class: "bar", title: `confidence ${Math.round(x.confidence * 100)}%` }, h("div", { style: `width:${Math.round(x.confidence * 100)}%` })),
      h("span", { class: "pct" }, `${Math.round(x.confidence * 100)}%`),
      h("button", { class: `small ghost${pinned ? " on" : ""}`, title: "Pin: tell the AI to focus on this", onclick: () => mark(x.id, pinned ? "" : "pinned") }, "📌"),
      h("button", { class: `small ghost${x.tech_mark === "ruled_out" ? " on" : ""}`, title: "Rule out: tell the AI to drop this", onclick: () => mark(x.id, x.tech_mark === "ruled_out" ? "" : "ruled_out") }, "✕"));
  }));
}

// ------------------------------------------------------------------ chat

function nearBottom(el) { return el.scrollHeight - el.scrollTop - el.clientHeight < 80; }

function renderEntry(e, live = false) {
  if (e.kind === "note") {
    const last = S.state.chat[S.state.chat.length - 1] === e;
    return h("div", { class: "msg note" }, e.text, e.retry && last && S.state.can_retry && !S.state.busy ? h("span", {}, " ", retryButton()) : null);
  }
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
    if (e.images?.length) {
      const strip = h("div", { class: "thumbs" });
      for (const name of e.images) {
        const img = h("img", { class: "thumb", alt: name, title: name });
        caseImage(name).then((url) => { if (url) img.src = url; });
        strip.append(img);
      }
      box.append(strip);
      for (const [name, d] of Object.entries(e.image_notes || {})) {
        box.append(h("details", {}, h("summary", { title: d.sealed ? `End-to-end encrypted: ${d.sealed}` : d.tee_attested ? `TEE attested before sending: ${d.tee_attested}` : "" },
          `👁 ${name} as described by ${d.model} for the chat model${d.sealed ? " · 🔐" : d.tee_attested ? " · 🛡 TEE" : ""}`), h("pre", {}, d.text)));
      }
    }
    return box;
  }
  // assistant (final or streaming)
  const who = h("div", { class: "who" }, "AI", e.model ? ` · ${e.model}` : "",
    e.tier ? h("span", { class: `badge ${e.tier}` }, e.tier) : null,
    e.sealed ? h("span", { class: "sealed", title: e.sealed }, "🔐 end-to-end encrypted") : null,
    e.tee_attested ? teeBadge(e) : null,
    !e.streaming && e.text ? h("button", { type: "button", class: "small ghost copy", title: "Copy this message (Markdown)",
      onclick: () => guarded(async () => { await clipWrite(e.text); toast("Message copied.", "ok", 2000); }) }, "Copy") : null);
  const box = h("div", { class: "msg assistant" }, who);
  if (e.reasoning) {
    const noMessage = !e.streaming && !e.text;
    box.append(h("details", { open: !e.text ? true : false },
      h("summary", {}, noMessage ? "Reasoning (the AI wrote no message this turn)" : "Reasoning"), h("div", { class: "reasoning" }, e.reasoning)));
  }
  box.append(h("div", { class: "body", html: md(e.text) }));
  if (e.searches?.length) box.append(h("div", { class: "scards" }, e.searches.map((r) => searchCard(r))));
  if (e.withdrawn?.length || e.reordered?.length) {
    box.append(h("div", { class: "revisions small" },
      ...(e.withdrawn || []).map((w) => h("div", {}, h("span", { class: "chip", onclick: () => flashQueueItem(w.num) }, `#${w.num}`), " withdrawn", w.reason ? `: ${w.reason}` : "")),
      e.reordered?.length ? h("div", {}, "Reordered pending: ", e.reordered.map((n) => `#${n}`).join(" → ")) : null));
  }
  if (e.questions?.length) box.append(questionBlock(e.questions, live));
  if (e.hyp_changes?.length) box.append(hypChanges(e.hyp_changes));
  if (e.proposals?.length) box.append(h("div", { class: "pcards" }, e.proposals.map(proposalCard)));
  return box;
}

// Questions from ask_technician. Quick replies on the latest AI message fill the draft; the
// technician still presses Enter, so they can add context or answer several at once.
function questionBlock(questions, live) {
  return h("div", { class: "questions" }, questions.map((q) => h("div", { class: "question" },
    h("div", { class: "q" }, q.question),
    live && q.options?.length ? h("div", { class: "qopts" }, q.options.map((o) =>
      h("button", { type: "button", class: "small", title: "Add this answer to your reply",
        onclick: (ev) => {
          for (const b of ev.target.parentElement.children) b.classList.toggle("on", b === ev.target);
          answerQuestion(q.question, o);
        } }, o))) : null)));
}

function answerQuestion(question, answer) {
  const input = $("#chat-input");
  const prefix = `Q: ${question} — `;
  const lines = input.value ? input.value.split("\n") : [];
  const i = lines.findIndex((l) => l.startsWith(prefix));
  if (i >= 0) lines[i] = prefix + answer; else lines.push(prefix + answer);
  input.value = lines.join("\n");
  input.focus();
  input.selectionStart = input.selectionEnd = input.value.length;
}

function hypChanges(list) {
  const pct = (x) => `${Math.round((x || 0) * 100)}%`;
  const label = (c) => ({ new: `+ ${c.id} ${pct(c.to)}`, up: `▲ ${c.id} ${pct(c.from)}→${pct(c.to)}`,
    down: `▼ ${c.id} ${pct(c.from)}→${pct(c.to)}`, supported: `✓ ${c.id} ${pct(c.to)}`,
    ruled_out: `✕ ${c.id}`, dropped: `${c.id}` })[c.kind] || c.id;
  return h("div", { class: "hyp-changes" }, h("span", { class: "muted" }, "Hypotheses"),
    list.map((c) => h("span", { class: `hc ${c.kind}`, title: `${c.text}${c.kind === "dropped" ? " (dropped)" : c.kind === "ruled_out" ? " (ruled out)" : ""}` }, label(c))));
}

// A web search the AI asked for. While it awaits approval the query is editable; the search
// runs only when the technician clicks Search (or the mode is auto on an Open case).
const SEARCH_STATE = { pending: "queued", awaiting: "waiting for your approval", running: "searching…",
  declined: "skipped by you", cancelled: "cancelled" };

function searchCard(rec) {
  const state = rec.status === "done" ? `${rec.results.length} result${rec.results.length === 1 ? "" : "s"}${typeof rec.cost === "number" ? ` · $${rec.cost.toFixed(3)}` : ""}${rec.edited_by_technician ? " · query edited by you" : ""}`
    : rec.status === "failed" || rec.status === "unavailable" ? `not run: ${rec.error}` : SEARCH_STATE[rec.status] || rec.status;
  const el = h("div", { class: `scard s-${rec.status}` },
    h("div", { class: "phead" }, h("span", {}, "🔎 Web search"), h("span", { class: "muted small" }, rec.provider),
      h("span", { class: "spacer" }), h("span", { class: "small sstate" }, state)));
  if (rec.status === "awaiting") {
    const q = h("input", { type: "text", value: rec.query, spellcheck: "false" });
    const answer = (approve) => guarded(async () => { await api("POST", `/api/web-search/${rec.id}`, { approve, query: q.value }); });
    q.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); answer(true); } });
    el.append(q, rec.reason ? h("div", { class: "muted small" }, rec.reason) : null,
      h("div", { class: "muted small" }, "The query goes to the search provider through NanoGPT, in the clear. Edit out anything that identifies the client."),
      h("div", { class: "pactions" }, h("button", { type: "button", class: "small primary", onclick: () => answer(true) }, "Search"),
        h("button", { type: "button", class: "small", onclick: () => answer(false) }, "Skip")));
  } else {
    el.append(h("div", { class: "pcmd" }, rec.query), rec.reason ? h("div", { class: "muted small" }, rec.reason) : null,
      rec.note ? h("div", { class: "small warn" }, rec.note) : null);
  }
  if (rec.status === "done" && rec.results.length) {
    el.append(h("details", {}, h("summary", {}, "Results"), h("ol", { class: "sresults" }, rec.results.map((r) =>
      h("li", {}, r.url ? h("a", { href: r.url, target: "_blank", rel: "noopener noreferrer" }, r.title || r.url) : (r.title || "(untitled)"),
        r.date ? h("span", { class: "muted small" }, ` · ${r.date}`) : null,
        r.url ? h("div", { class: "muted small surl" }, r.url) : null)))));
  }
  return el;
}

// A proposal inside the AI's message: a live view of its queue item, updated in place on
// every queue change. Run goes through runItem, the same technician action as the queue.
function proposalCard(num) {
  const el = h("div", { class: "pcard", "data-num": num });
  fillProposalCard(el);
  return el;
}

function fillProposalCard(el) {
  const num = Number(el.dataset.num);
  const item = (S.state?.queue || []).find((i) => i.num === num);
  if (!item) {
    el.className = "pcard st-sent";
    el.replaceChildren(h("span", { class: "muted small" }, `#${num} is no longer in the queue`));
    return;
  }
  el.className = `pcard risk-${item.risk} st-${item.status}`;
  const pending = item.status === "pending";
  const btn = (label, fn, kind = "", title = "") => h("button", { type: "button", class: `small ${kind}`, title, onclick: () => guarded(fn) }, label);
  const sessOpen = (S.state.sessions || []).some((s) => s.id === item.session_id && !s.exited);
  let actions = null;
  if (pending) {
    const run = btn("Run", () => runItem(num, "run"), item.risk === "disruptive" ? "danger" : "primary", "Type into the terminal and press Enter");
    run.disabled = !sessOpen;
    actions = h("div", { class: "pactions" }, run,
      btn("Skip…", () => skipItem(item), "", "Skip, with a reason for the AI"),
      btn("Force skip", () => forceSkip(item), "ghost", "Skip in one click; the AI is told you chose not to run it"));
  } else if (item.status === "withdrawn") {
    actions = h("div", { class: "pactions" },
      btn("Restore", () => api("POST", `/api/queue/${num}`, { status: "pending", note: "" }), "ghost", "Put it back in the queue as pending"));
  }
  el.replaceChildren(...[
    h("div", { class: "phead" },
      h("span", { class: "chip", title: "Show in queue", onclick: () => flashQueueItem(num) }, `#${num}`),
      h("span", { class: `badge risk ${item.risk}` }, item.risk.replace("_", " ")),
      item.sensitive?.length ? h("span", { class: "badge sensitive", title: sensitiveTitle(item) }, "sensitive") : null,
      h("span", { class: "muted small" }, item.session_id),
      h("span", { class: "spacer" }),
      h("span", { class: `status ${item.status}` }, STATUS_LABEL[item.status] + (item.status === "skipped" && !item.note ? " (no reason)" : ""))),
    h("div", { class: "pcmd" }, item.command),
    item.purpose ? h("div", { class: "muted small" }, item.purpose) : null,
    item.cuts_session ? h("div", { class: "cuts small" }, `⚠ Cuts this session: ${item.cuts_session}`) : null,
    item.sensitive?.length ? h("div", { class: "sens small" }, `🔍 May expose sensitive data: ${item.sensitive.join("; ")}`) : null,
    reviewLine(item),
    item.note ? h("div", { class: "small warn" }, `Note: ${item.note}`) : null,
    actions].filter(Boolean));
}

function updateProposalCards() {
  for (const el of document.querySelectorAll("#chat-log .pcard")) fillProposalCard(el);
}

function renderChat(scroll) {
  const log = $("#chat-log");
  const stick = scroll || nearBottom(log);
  const chat = S.state.chat || [];
  // quick replies only on the newest AI message that the technician hasn't answered yet
  let live = -1;
  for (let i = chat.length - 1; i >= 0; i--) {
    if (chat[i].kind === "user") break;
    if (chat[i].kind === "assistant") { live = i; break; }
  }
  log.replaceChildren(...chat.map((e, i) => renderEntry(e, i === live && !S.streaming)));
  if (S.streaming) log.append(streamingBubble());
  if (!chat.length && !S.streaming) {
    log.append(h("div", { class: "empty" },
      "Describe the problem as you would to a colleague. The AI will talk it through with you, ask what it needs to know, and propose commands; nothing runs until you click Run."));
  }
  if (stick) log.scrollTop = log.scrollHeight;
}

function streamingBubble() {
  const el = renderEntry({ ...S.streaming, streaming: true });
  el.id = "streaming";
  el.classList.add("streaming");
  (el.querySelector(".body > :last-child") || el.querySelector(".body")).append(h("span", { class: "caret" }));
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

// The status line under the chat always says whose turn it is: while the AI works it names
// the phase and flags silence (tool-call arguments stream without visible text), and once the
// reply is complete it says so and lists what is waiting for the technician.
const TOOL_PHASE = { propose_commands: "Preparing commands", update_hypotheses: "Updating hypotheses",
  ask_technician: "Writing questions", run_recipe: "Queuing a recipe", revise_queue: "Revising the queue",
  web_search: "Searching the web", describe_image: "Vision helper is reading the image (this adds a step)" };

function clock(secs) { return `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`; }

function renderStatus() {
  const bar = $("#chat-status"), text = $("#chat-status-text");
  if (!S.state) return;
  if (S.state.busy) {
    const st = S.streaming || { phase: "waiting", startAt: Date.now(), lastAt: Date.now() };
    const now = Date.now(), total = Math.round((now - st.startAt) / 1000), quiet = Math.round((now - st.lastAt) / 1000);
    if ((st.searches || []).some((r) => r.status === "awaiting")) {
      bar.className = "waiting";
      text.textContent = `Waiting for you: approve or skip the web search in the AI's message  ·  ${clock(total)}`;
      return;
    }
    let msg = { waiting: "Waiting for the model", reasoning: "Thinking", writing: "Writing",
      tool: TOOL_PHASE[st.tool] || "Working" }[st.phase] || "Working";
    if (st.phase === "waiting" && total >= 8) msg += " (slow to start; still waiting)";
    else if (st.phase !== "tool" && st.phase !== "waiting" && quiet >= 5) msg += `… still working, no new text for ${quiet}s`;
    else msg += "…";
    bar.className = "busy";
    text.textContent = `AI is responding: ${msg}  ·  ${clock(total)}`;
    return;
  }
  // checked first: after a failed request the last chat entry is the technician's own message
  if (S.state.can_retry) {
    bar.className = "failed";
    const err = S.lastTurn?.error || S.state.last_error;
    text.replaceChildren(err === "stopped" ? "■ Stopped. " : "✕ The last message got no answer. ",
      retryButton(), h("span", { class: "muted" }, " or change the model first, or type a new message."));
    return;
  }
  const chat = S.state.chat || [];
  const last = [...chat].reverse().find((e) => e.kind === "assistant" || e.kind === "user");
  if (!last || last.kind !== "assistant") { bar.className = "hidden"; return; }
  const pending = S.state.queue.filter((i) => i.status === "pending").length;
  const ready = readyItems().length;
  const todo = [];
  if (pending) todo.push(`${pending} command${pending > 1 ? "s" : ""} to run or skip`);
  if (ready) todo.push(`${ready} result${ready > 1 ? "s" : ""} ready to send`);
  if (last.questions?.length) todo.push(`${last.questions.length} question${last.questions.length > 1 ? "s" : ""} to answer`);
  bar.className = "done";
  text.textContent = `✓ AI finished${S.lastTurn?.secs ? ` (${clock(S.lastTurn.secs)})` : ""}. Your turn${todo.length ? ": " + todo.join(" · ") : "."}`;
}

function retryButton() {
  return h("button", { type: "button", class: "small primary", title: "Send the last message again, with the model selected now",
    onclick: () => guarded(() => api("POST", "/api/retry")) }, `Retry with ${S.state.config.active_model || "the selected model"}`);
}

function renderBusy() {
  const busy = !!S.state.busy;
  renderStatus();
  $("#stop-btn").classList.toggle("hidden", !busy);
  $("#send-btn").disabled = busy;
  $("#send-results-btn").disabled = busy || readyItems().length === 0;
  renderChatResults();
}

const imageCache = new Map();
async function caseImage(name) {
  if (imageCache.has(name)) return imageCache.get(name);
  try {
    const r = await fetch(`/api/case/file/${encodeURIComponent(name)}`, { headers: { "X-Token": TOKEN } });
    if (!r.ok) return null;
    const url = URL.createObjectURL(await r.blob());
    imageCache.set(name, url);
    return url;
  } catch { return null; }
}

// Photos attached to the next message: resized on this machine, sent as data URLs.
const pendingImages = [];
function renderAttachments() {
  const strip = $("#attachments");
  strip.replaceChildren(...pendingImages.map((d, i) => h("div", { class: "att" }, h("img", { src: d, class: "thumb" }),
    h("button", { class: "small ghost", title: "Remove", onclick: () => { pendingImages.splice(i, 1); renderAttachments(); } }, "×"))));
  strip.classList.toggle("hidden", !pendingImages.length);
}
async function attachPhoto(file) {
  if (!file || !file.type.startsWith("image/")) return;
  needVision();
  if (pendingImages.length >= 4) return toast("Up to four photos per message.");
  const url = URL.createObjectURL(file);
  try {
    const full = await shrinkImage(url, 4000, "image/jpeg", 0.92);      // decode once, at working size
    const redacted = await redactImage(full, `Photo: black out anything sensitive`);
    if (!redacted) return;
    pendingImages.push(await shrinkImage(redacted, IMAGE_MAX, "image/jpeg", 0.9));
    renderAttachments();
  } finally {
    URL.revokeObjectURL(url);
  }
}

async function sendChat(ev) {
  ev?.preventDefault();
  const input = $("#chat-input");
  const message = input.value.trim();
  if (!message && !pendingImages.length) return;
  await guarded(async () => {
    await api("POST", "/api/send", { message, images: pendingImages.slice() });
    pendingImages.length = 0;
    renderAttachments();
    if (message && S.chatHistory[S.chatHistory.length - 1] !== message) S.chatHistory.push(message);
    S.histIdx = -1;
    input.value = "";
  });
}

// Up/Down at the edge of the chat box recalls earlier messages, like a shell.
function chatHistoryKey(e) {
  const input = e.target;
  const hist = S.chatHistory;
  if (!hist.length || e.altKey || e.ctrlKey || e.metaKey) return;
  const atTop = input.selectionStart === 0 && input.selectionEnd === 0;
  const atEnd = input.selectionStart === input.value.length;
  if (e.key === "ArrowUp" && (atTop || S.histIdx !== -1)) {
    if (S.histIdx === -1) { S.histDraft = input.value; S.histIdx = hist.length; }
    if (S.histIdx > 0) { S.histIdx -= 1; input.value = hist[S.histIdx]; e.preventDefault(); }
  } else if (e.key === "ArrowDown" && S.histIdx !== -1 && atEnd) {
    S.histIdx += 1;
    if (S.histIdx >= hist.length) { S.histIdx = -1; input.value = S.histDraft; } else input.value = hist[S.histIdx];
    e.preventDefault();
  }
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
    if (e.type !== "keydown") return true;
    if (isGlobalShortcut(e)) return false;   // let it bubble to the document handler
    if (!e.ctrlKey || !e.shiftKey) return true;
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
  for (const [id, r] of Object.entries(S.rdps)) r.host.style.display = id === sid ? "" : "none";
  for (const tab of document.querySelectorAll(".tab")) tab.classList.toggle("active", tab.dataset.sid === sid);
  const t = S.terms[sid];
  if (t) { fitTerm(t, true); if (focus) t.term.focus(); }
  const r = S.rdps[sid];
  if (r) { rdpResize(r); if (focus) r.view.focus(); }
}

// Sessions on the same device get the same colour; the 🔗 button links or unlinks.
const DEVICE_COLOURS = ["#a371f7", "#3fb97f", "#e0a93a", "#4f9cf5", "#e5534b", "#39c5cf"];

function linkMenu(s) {
  const sessions = (S.state.sessions || []).filter((x) => x.id !== s.id && !x.exited);
  const linkedWith = sessions.filter((x) => x.device === s.device);
  const choose = (to) => guarded(async () => { await api("POST", `/api/sessions/${encodeURIComponent(s.id)}/link`, { to }); m.close(); });
  const m = modal({ title: `Device link for ${s.id}`,
    body: h("div", { style: "display:flex;flex-direction:column;gap:8px" },
      h("div", { class: "muted small" }, "Linked sessions reach the same machine; the AI is told so, and sends commands to the shell while you look at the desktop. Sessions to the same address link automatically."),
      h("div", {}, linkedWith.length ? `Linked with: ${linkedWith.map((x) => x.id).join(", ")} (${s.link === "manual" ? "linked by you" : "automatic"})` : "Not linked to any other session."),
      ...sessions.filter((x) => x.device !== s.device).map((x) =>
        h("button", { type: "button", onclick: () => choose(x.id) }, `Same machine as ${x.id} (${x.kind} ${x.target})`)),
      linkedWith.length ? h("button", { type: "button", class: "danger", onclick: () => choose(null) },
        "Unlink (and don't link it automatically again)") : null),
    buttons: [{ label: "Close" }] });
}

function renderSessions() {
  const sessions = S.state.sessions || [];
  const ids = new Set(sessions.map((s) => s.id));
  for (const [id, t] of Object.entries(S.terms)) {
    if (!ids.has(id)) { t.ws?.close(); t.term.dispose(); t.host.remove(); delete S.terms[id]; }
  }
  for (const [id, r] of Object.entries(S.rdps)) {
    if (!ids.has(id)) { try { r.client.disconnect(); } catch { /* gone */ } r.host.remove(); delete S.rdps[id]; }
  }
  const counts = {};
  for (const s of sessions) if (!s.exited) counts[s.device] = (counts[s.device] || 0) + 1;
  const colour = {};
  for (const d of Object.keys(counts)) if (counts[d] > 1) colour[d] = DEVICE_COLOURS[Object.keys(colour).length % DEVICE_COLOURS.length];
  const list = $("#tab-list");
  list.replaceChildren();
  for (const s of sessions) {
    if (s.kind === "rdp") ensureRdp(s); else ensureTerm(s);
    const linked = colour[s.device];
    const tab = h("div", { class: `tab${s.exited ? " exited" : ""}${linked ? " linked" : ""}${s.kind === "rdp" && !s.connected ? " offline" : ""}`,
      "data-sid": s.id, title: `${s.target} ${s.os_hint || ""}`, style: linked ? `--dev:${linked}` : null,
      onclick: () => activateTab(s.id) },
      h("span", { class: "dot" }), h("span", { class: "name" }, s.id), h("span", { class: "kind" }, s.kind),
      h("button", { class: `link${linked ? " on" : ""}`, title: linked ? "Linked to another session on the same machine: click to change" : "Link to another session on the same machine",
        onclick: (e) => { e.stopPropagation(); linkMenu(s); } }, "🔗"),
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

// Text of the terminal buffer from a marker to the next marker (or the cursor). When the
// buffer no longer has it (reload, closed session, resumed case) the server slices the
// transcript file instead.
async function captureFor(item) {
  if (S.rdps[item.session_id] || (S.state.sessions || []).find((s) => s.id === item.session_id)?.kind === "rdp") {
    return { text: "", rdp: true };
  }
  const local = captureFromBuffer(item);
  if (!local.error) return local;
  try {
    const remote = await api("GET", `/api/queue/${item.num}/capture`);
    if (!remote.error) return { text: remote.text, source: "transcript" };
    return { text: "", error: `${local.error} (${remote.error})` };
  } catch (e) {
    return { text: "", error: `${local.error} (${e.message})` };
  }
}

function captureFromBuffer(item) {
  const t = S.terms[item.session_id];
  if (!t) return { text: "", error: "Session is no longer open" };
  const m = t.markers.get(item.num);
  if (!m) return { text: "", error: "Not run from this window" };
  if (m.isDisposed || m.line < 0) return { text: "", error: "Output has scrolled out of the terminal buffer" };
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

// ------------------------------------------------------------------ terminal screenshot

// Appliance logins (OPNsense, pfSense, Sophos, switch consoles) often land on a menu, not a
// shell; as plain text the model can miss that. This draws the visible screen, colours and
// inverse video included, from xterm's buffer onto a canvas (the DOM renderer has no canvas
// to copy, and the CSP rightly blocks screenshot libraries).
const ANSI16 = ["#2e3436", "#cc0000", "#4e9a06", "#c4a000", "#3465a4", "#75507b", "#06989a", "#d3d7cf",
  "#555753", "#ef2929", "#8ae234", "#fce94f", "#729fcf", "#ad7fa8", "#34e2e2", "#eeeeec"];

function paletteColor(n) {
  if (n < 16) return ANSI16[n];
  if (n < 232) {
    const v = [0, 95, 135, 175, 215, 255], i = n - 16;
    return `rgb(${v[Math.floor(i / 36)]},${v[Math.floor(i / 6) % 6]},${v[i % 6]})`;
  }
  const g = 8 + (n - 232) * 10;
  return `rgb(${g},${g},${g})`;
}

function cellColor(cell, fg, fallback) {
  if (fg ? cell.isFgDefault() : cell.isBgDefault()) return fallback;
  const c = fg ? cell.getFgColor() : cell.getBgColor();
  if (fg ? cell.isFgRGB() : cell.isBgRGB()) return `#${c.toString(16).padStart(6, "0")}`;
  return paletteColor(c);
}

function terminalScreenshot(sid) {
  const t = S.terms[sid];
  if (!t) throw new Error("No terminal open.");
  const term = t.term, buf = term.buffer.active;
  const theme = term.options.theme || {};
  const bg0 = theme.background || "#000000", fg0 = theme.foreground || "#ffffff";
  const size = term.options.fontSize || 13, family = term.options.fontFamily || "monospace";
  const ctx0 = document.createElement("canvas").getContext("2d");
  ctx0.font = `${size}px ${family}`;
  const cw = ctx0.measureText("W").width, ch = Math.ceil(size * 1.3), pad = 8;
  const atBottom = buf.viewportY === buf.baseY;
  const cell = buf.getNullCell();
  // only the rows in use: down to the last row with content (or the cursor)
  let rows = atBottom ? buf.cursorY + 1 : 1;
  for (let row = term.rows - 1; row >= rows; row--) {
    const line = buf.getLine(buf.viewportY + row);
    if (line && line.translateToString(true).trim()) { rows = row + 1; break; }
  }
  const baseW = term.cols * cw + pad * 2, baseH = rows * ch + pad * 2;
  const scale = baseW * 2 <= 2048 ? 2 : 1;                 // whole-number scale: fractional blurs every glyph
  const W = Math.ceil(baseW * scale), H = Math.ceil(baseH * scale);
  const canvas = document.createElement("canvas");
  canvas.width = W; canvas.height = H;
  const ctx = canvas.getContext("2d");
  // Text goes on its own transparent layer: text drawn straight onto an opaque background may be
  // sub-pixel antialiased (coloured fringes on every letter); on a transparent layer it can't be.
  const layer = document.createElement("canvas");
  layer.width = W; layer.height = H;
  const tx = layer.getContext("2d");
  ctx.scale(scale, scale);
  tx.scale(scale, scale);
  ctx.fillStyle = bg0;
  ctx.fillRect(0, 0, baseW, baseH);
  tx.textBaseline = "middle";
  for (let row = 0; row < rows; row++) {
    const line = buf.getLine(buf.viewportY + row);
    if (!line) continue;
    const y = pad + row * ch;
    for (let col = 0; col < term.cols; col++) {
      if (!line.getCell(col, cell) || cell.getWidth() === 0) continue;   // right half of a wide char
      let fg = cellColor(cell, true, fg0), bg = cellColor(cell, false, null);
      if (cell.isInverse()) [fg, bg] = [bg || bg0, fg];
      if (cell.isBold() && !cell.isFgDefault() && cell.isFgPalette() && cell.getFgColor() < 8) fg = ANSI16[cell.getFgColor() + 8];
      const x = pad + col * cw, w = cw * cell.getWidth();
      if (bg) { ctx.fillStyle = bg; ctx.fillRect(x, y, w + 0.5, ch); }
      const chars = cell.getChars();
      if (!chars || chars === " " || cell.isInvisible()) continue;
      tx.font = `${cell.isItalic() ? "italic " : ""}${cell.isBold() ? "bold " : ""}${size}px ${family}`;
      tx.globalAlpha = cell.isDim() ? 0.6 : 1;
      tx.fillStyle = fg;
      tx.fillText(chars, x, y + ch / 2);
      if (cell.isUnderline()) tx.fillRect(x, y + ch - 2, w, 1);
      tx.globalAlpha = 1;
    }
  }
  if (atBottom) {                                           // cursor, when the screen isn't scrolled back
    tx.strokeStyle = fg0;
    tx.lineWidth = 1;
    tx.strokeRect(pad + buf.cursorX * cw + 0.5, pad + buf.cursorY * ch + 0.5, cw - 1, ch - 1);
  }
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.drawImage(layer, 0, 0);
  return canvas.toDataURL("image/png");
}

// ------------------------------------------------------------------ RDP (guacd)

// The server does the guacd handshake (the password never reaches this page) and relays the
// Guacamole protocol; guacamole-common-js draws the desktop and sends mouse and keyboard.
const RDP_STATES = ["idle", "connecting…", "waiting…", "connected", "disconnecting…", "disconnected"];

function rdpViewSize(r) {
  const w = r.view.clientWidth || $("#terms").clientWidth || 1280;
  const hh = r.view.clientHeight || ($("#terms").clientHeight - 34) || 800;
  return [Math.max(200, Math.floor(w)), Math.max(200, Math.floor(hh))];
}

function rdpFit(r) {
  const d = r.client?.getDisplay();
  if (!d || !d.getWidth() || !r.view.clientWidth) return;
  const scale = Math.min(r.view.clientWidth / d.getWidth(), r.view.clientHeight / d.getHeight());
  d.scale(isFinite(scale) && scale > 0 ? scale : 1);
}

let rdpResizeTimer = null;
function rdpResize(r) {
  rdpFit(r);
  clearTimeout(rdpResizeTimer);
  rdpResizeTimer = setTimeout(() => {
    if (r.state === 3) r.client.sendSize(...rdpViewSize(r));   // remote follows (display-update)
  }, 400);
}

function needsVision(el) { el.setAttribute("data-needs-vision", ""); return el; }

function ensureRdp(sess) {
  if (S.rdps[sess.id]) return S.rdps[sess.id];
  const r = { id: sess.id, state: 0, clipboard: "", typedOk: false };
  const btn = (label, title, fn) => h("button", { type: "button", class: "small ghost", title, onclick: () => guarded(fn) }, label);
  r.view = h("div", { class: "rdp-view", tabindex: 0 });
  r.status = h("span", { class: "rdp-status muted small" }, "connecting…");
  r.clipBtn = btn("Clipboard → AI", "Send the text last copied in the remote desktop to the AI", () => sendRdpClipboard(r));
  r.clipBtn.disabled = true;
  r.bar = h("div", { class: "rdp-bar" },
    btn("Ctrl+Alt+Del", "Send Ctrl+Alt+Del", () => rdpKeys(r, [0xFFE3, 0xFFE9, 0xFFFF])),
    btn("Win", "Press the Windows key", () => rdpKeys(r, [0xFFEB])),
    btn("Win+R", "Open the Run dialog", () => rdpKeys(r, [0xFFEB, 0x72])),
    needsVision(btn("Screenshot → chat", "Attach a screenshot of the remote desktop to your next message", attachScreenshot)),
    r.clipBtn,
    btn("Paste text…", "Put text on the remote clipboard, or type it into the focused window", () => rdpPasteText(r)),
    btn("Reconnect", "Reconnect the remote desktop", () => rdpConnect(r)),
    h("span", { class: "spacer" }), r.status);
  r.host = h("div", { class: "term-host rdp-host" }, r.bar, r.view);
  setTimeout(renderVision, 0);
  $("#terms").append(r.host);
  r.keyboard = new Guacamole.Keyboard(r.view);
  r.keyboard.onkeydown = (keysym) => { if (r.state === 3) r.client.sendKeyEvent(1, keysym); return false; };
  r.keyboard.onkeyup = (keysym) => { if (r.state === 3) r.client.sendKeyEvent(0, keysym); };
  r.view.addEventListener("blur", () => r.keyboard.reset());
  r.view.addEventListener("mousedown", () => r.view.focus());
  S.rdps[sess.id] = r;
  rdpConnect(r);
  return r;
}

function rdpConnect(r) {
  try { r.client?.disconnect(); } catch { /* already gone */ }
  const client = new Guacamole.Client(new Guacamole.WebSocketTunnel(`ws://${location.host}/ws/rdp/${encodeURIComponent(r.id)}`));
  r.client = client;
  const display = client.getDisplay();
  r.view.replaceChildren(display.getElement());
  client.onstatechange = (st) => {
    if (client !== r.client) return;
    r.state = st;
    r.status.textContent = RDP_STATES[st] || "";
    r.host.classList.toggle("rdp-off", st === 5);
    if (st === 3) rdpResize(r);
  };
  client.onerror = (status) => {
    r.status.textContent = `error: ${status.message || status.code}`;
    toast(`RDP ${r.id}: ${status.message || `error ${status.code}`}`, "error", 12000);
  };
  client.onclipboard = (stream, mimetype) => {
    if (!/^text\//.test(mimetype)) return;
    const reader = new Guacamole.StringReader(stream);
    let data = "";
    reader.ontext = (t) => { data += t; };
    reader.onend = () => { r.clipboard = data; r.clipBtn.disabled = !data.trim(); };
  };
  display.onresize = () => rdpFit(r);
  const mouse = new Guacamole.Mouse(display.getElement());
  mouse.onEach(["mousedown", "mousemove", "mouseup"], (e) => { if (r.state === 3) client.sendMouseState(e.state, true); });
  const [w, hh] = rdpViewSize(r);
  client.connect(`t=${encodeURIComponent(TOKEN)}&width=${w}&height=${hh}&dpi=96`);
}

function rdpKeys(r, keysyms) {
  if (r.state !== 3) throw new Error("The remote desktop is not connected.");
  for (const k of keysyms) r.client.sendKeyEvent(1, k);
  for (const k of [...keysyms].reverse()) r.client.sendKeyEvent(0, k);
  r.view.focus();
}

function keysymFor(ch) {
  if (ch === "\n") return 0xFF0D;
  if (ch === "\t") return 0xFF09;
  const c = ch.codePointAt(0);
  return (c >= 0x20 && c <= 0x7E) || (c >= 0xA0 && c <= 0xFF) ? c : 0x01000000 | c;
}

// Type text into whichever window has focus on the remote desktop (Run adds Enter).
async function rdpType(r, text, enter) {
  if (r.state !== 3) throw new Error("The remote desktop is not connected.");
  const chars = [...text.replace(/\r\n?/g, "\n")];
  for (let i = 0; i < chars.length; i++) {
    const k = keysymFor(chars[i]);
    r.client.sendKeyEvent(1, k);
    r.client.sendKeyEvent(0, k);
    if (i % 16 === 15) await new Promise((res) => setTimeout(res, 15));   // let the remote app keep up
  }
  if (enter) { r.client.sendKeyEvent(1, 0xFF0D); r.client.sendKeyEvent(0, 0xFF0D); }
}

function rdpPasteText(r) {
  const ta = h("textarea", { rows: 6, class: "mono", spellcheck: "false", placeholder: "Text to send to the remote desktop" });
  modal({ title: `Paste into ${r.id}`, body: h("div", { class: "field" }, ta,
    h("div", { class: "muted small" }, "“Remote clipboard” puts it on the remote clipboard; then press Ctrl+V there. “Type it” types it into the focused window.")),
    buttons: [{ label: "Cancel" },
      { label: "Type it", onClick: async () => { await rdpType(r, ta.value, false); } },
      { label: "Remote clipboard", kind: "primary", onClick: () => {
        const writer = new Guacamole.StringWriter(r.client.createClipboardStream("text/plain"));
        writer.sendText(ta.value);
        writer.sendEnd();
        toast("On the remote clipboard: press Ctrl+V in the remote desktop.", "ok", 4000);
        r.view.focus();
      } }] });
}

async function sendRdpClipboard(r) {
  if (!r.clipboard.trim()) return toast("Copy some text in the remote desktop first.");
  await sendExcerpt(r.id, r.clipboard, `Send copied text from ${r.id}`);
}

// ------------------------------------------------------------------ screenshots & redaction

// Every image goes through this before it is attached: drag rectangles to black out, then
// Attach. The pixels are replaced in this page, so the original never leaves it.
function redactImage(dataUrl, title = "Black out anything sensitive, then attach") {
  return new Promise((resolve) => {
    const img = new Image();
    img.onerror = () => resolve(null);
    img.onload = () => {
      const canvas = h("canvas", { class: "redact-canvas" });
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
      const ctx = canvas.getContext("2d");
      const rects = [];
      let drag = null, result = null;
      const draw = () => {
        ctx.globalAlpha = 1;
        ctx.globalCompositeOperation = "source-over";
        ctx.drawImage(img, 0, 0);
        ctx.fillStyle = "#000";                 // opaque black painted INTO the one bitmap: no layers
        for (const b of rects) ctx.fillRect(b.x, b.y, b.w, b.h);
        if (drag) {
          ctx.fillRect(drag.x, drag.y, drag.w, drag.h);
          ctx.strokeStyle = "#e5534b";
          ctx.lineWidth = Math.max(2, canvas.width / 500);
          ctx.strokeRect(drag.x, drag.y, drag.w, drag.h);
        }
        count.textContent = rects.length ? `${rects.length} area(s) blacked out` : "Nothing blacked out yet";
      };
      const pt = (e) => {
        const b = canvas.getBoundingClientRect();
        return { x: Math.max(0, Math.min(canvas.width, (e.clientX - b.left) * canvas.width / b.width)),
          y: Math.max(0, Math.min(canvas.height, (e.clientY - b.top) * canvas.height / b.height)) };
      };
      const move = (e) => {
        if (!drag) return;
        const p = pt(e);
        Object.assign(drag, { x: Math.min(drag.x0, p.x), y: Math.min(drag.y0, p.y), w: Math.abs(p.x - drag.x0), h: Math.abs(p.y - drag.y0) });
        draw();
      };
      // Snap outwards to whole pixels: a fractional box would be anti-aliased, leaving its
      // edge pixels only partly black, with a trace of what was under them.
      const snap = (b) => {
        const x = Math.floor(b.x), y = Math.floor(b.y);
        return { x, y, w: Math.min(canvas.width, Math.ceil(b.x + b.w)) - x, h: Math.min(canvas.height, Math.ceil(b.y + b.h)) - y };
      };
      const up = () => {
        if (drag && drag.w > 2 && drag.h > 2) rects.push(snap(drag));
        drag = null;
        draw();
      };
      canvas.addEventListener("mousedown", (e) => { e.preventDefault(); const p = pt(e); drag = { x0: p.x, y0: p.y, x: p.x, y: p.y, w: 0, h: 0 }; });
      window.addEventListener("mousemove", move);
      window.addEventListener("mouseup", up);
      const count = h("span", { class: "muted small" });
      const zoom = h("button", { type: "button", class: "small", onclick: () => {
        canvas.classList.toggle("fit");
        zoom.textContent = canvas.classList.contains("fit") ? "Actual size" : "Fit to window";
      } }, "Fit to window");
      const m = modal({ title, wide: true,
        body: h("div", { class: "redact" }, h("div", { class: "row", style: "align-items:center" }, h("div", { class: "muted small", style: "flex:1" },
          "Drag over anything that shouldn't reach the AI (names, addresses, keys). Blacked-out areas are replaced with black pixels before the image leaves this window. ", count), zoom),
          h("div", { class: "redact-wrap" }, canvas)),
        buttons: [
          { label: "Undo", onClick: () => { rects.pop(); draw(); return true; } },
          { label: "Clear", onClick: () => { rects.length = 0; draw(); return true; } },
          { label: "Cancel" },
          { label: "Attach", kind: "primary", onClick: () => { drag = null; draw(); result = canvas.toDataURL("image/png"); } }],
        // (the server also re-encodes every image from its pixels, dropping all metadata)
        onClose: () => {
          window.removeEventListener("mousemove", move);
          window.removeEventListener("mouseup", up);
          resolve(result);
        } });
      m.box.classList.add("xl");
      draw();
    };
    img.src = dataUrl;
  });
}

// Fit an image within `max` px on its long side. Images already within it are left untouched
// (every resample softens text); larger ones are halved step by step, then scaled once.
const IMAGE_MAX = 2048;
function shrinkImage(dataUrl, max = IMAGE_MAX, type = "image/png", quality = 0.9) {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => {
      let w = img.naturalWidth, hh = img.naturalHeight;
      if (Math.max(w, hh) <= max && dataUrl.startsWith(`data:${type}`)) return resolve(dataUrl);
      let src = img;
      while (Math.max(w, hh) / 2 >= max) {
        const c = document.createElement("canvas");
        c.width = Math.round(w / 2); c.height = Math.round(hh / 2);
        const cx = c.getContext("2d"); cx.imageSmoothingQuality = "high";
        cx.drawImage(src, 0, 0, c.width, c.height);
        src = c; w = c.width; hh = c.height;
      }
      const scale = Math.min(1, max / Math.max(w, hh));
      const c = document.createElement("canvas");
      c.width = Math.round(w * scale); c.height = Math.round(hh * scale);
      const cx = c.getContext("2d");
      cx.imageSmoothingQuality = "high";
      cx.drawImage(src, 0, 0, c.width, c.height);
      resolve(c.toDataURL(type, quality));
    };
    img.src = dataUrl;
  });
}

// Screenshot of the active session (terminal or remote desktop) -> redaction -> attachment.
async function attachScreenshot() {
  needVision();
  const sess = activeSession();
  if (!sess) return toast("Open and select a session first.");
  if (pendingImages.length >= 4) return toast("Up to four images per message.");
  const r = S.rdps[sess.id];
  let shot;
  if (r) {
    if (r.state !== 3) return toast("The remote desktop is not connected.");
    shot = r.client.getDisplay().flatten().toDataURL("image/png");
  } else {
    shot = terminalScreenshot(sess.id);
  }
  const redacted = await redactImage(shot, `Screenshot of ${sess.id}: black out anything sensitive`);
  if (!redacted) return;
  pendingImages.push(await shrinkImage(redacted));
  renderAttachments();
  const input = $("#chat-input");
  if (!input.value.trim()) {
    input.value = r ? `This is what I see on the remote desktop of session \`${sess.id}\` right now (screenshot attached).`
      : `This is what I see in the terminal of session \`${sess.id}\` right now (screenshot attached).`;
  }
  input.focus();
  input.selectionStart = input.selectionEnd = input.value.length;
}

// ------------------------------------------------------------------ queue

const STATUS_LABEL = { pending: "pending", ran: "ran", inserted: "inserted", skipped: "skipped", sent: "sent to AI", withdrawn: "withdrawn by AI" };
const HIDDEN_WHEN_DONE = ["sent", "withdrawn"];

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
  const items = S.state.queue.filter((i) => showDone || !HIDDEN_WHEN_DONE.includes(i.status));
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
  renderChatResults();
  updateProposalCards();
  if (!S.state.busy) renderStatus();
}

function renderChatResults() {
  const b = $("#chat-results-btn");
  const ready = readyItems().length;
  b.classList.toggle("hidden", !ready);
  b.textContent = `Results (${ready})`;
  b.disabled = !!S.state?.busy;
}

function autosize(ta) { ta.style.height = "auto"; ta.style.height = `${ta.scrollHeight + 2}px`; }

// The reviewer's verdict on a queue item: { cls, icon, label } or null.
const REVIEW_LEVEL = {
  ok: { icon: "✓", label: "Reviewer: looks fine" },
  care: { icon: "⚠", label: "Reviewer: proceed with care" },
  stop: { icon: "✗", label: "Reviewer: do not run" },
};

function reviewLine(item) {
  const r = item.review || {};
  if (!r.status) return null;
  if (r.status === "checking") return h("div", { class: "qreview checking" }, h("span", { class: "spinner" }), " Reviewer checking…");
  if (r.status === "error") return h("div", { class: "qreview error", title: r.error }, `Review not run: ${r.error}`);
  const lv = REVIEW_LEVEL[r.level] || { icon: "•", label: `Reviewer: ${r.verdict || "no verdict"}` };
  return h("div", { class: `qreview ${r.level || ""}`, title: `${r.model}${r.auto ? " (automatic)" : ""} — click for the full review`,
    onclick: () => showReview(item) },
    h("b", {}, `${lv.icon} ${lv.label}`), r.summary ? ` — ${r.summary}` : "",
    r.data ? h("div", { class: "qreview-data" }, `🔍 Reviewer: ${r.data}`) : null);
}

function showReview(item) {
  const r = item.review || {};
  modal({ title: `Second opinion on #${item.num}`, wide: true, body: h("div", {},
    h("pre", { class: "prompt-text" }, item.command),
    h("div", { class: "muted small" }, `Reviewer: ${r.model} (${r.tier})${r.auto ? " · automatic" : ""}${r.different_model ? "" : " — same model as the proposer"}`),
    h("div", { class: "pre" }, r.text)), buttons: [{ label: "Close" }] });
}

function sensitiveTitle(item) {
  return `May expose sensitive data:\n${item.sensitive.map((x) => `• ${x}`).join("\n")}\n\nNot run by Ctrl+Shift+Enter. Output is redacted before sending, but only on a best-effort basis: check it.`;
}

function buildRow(item) {
  const cmd = h("textarea", { class: "cmd", rows: 1, spellcheck: "false" });
  cmd.addEventListener("input", () => autosize(cmd));
  cmd.addEventListener("change", () => saveCommand(item.num, cmd.value));
  const sel = h("select", { class: "sess", onchange: (e) => guarded(() => api("POST", `/api/queue/${item.num}`, { session_id: e.target.value })) });
  const row = h("div", { class: "qitem", "data-num": item.num },
    h("div", { class: "num" }, `#${item.num}`),
    sel,
    h("div", {}, cmd, h("div", { class: "purpose" }), h("div", { class: "rollback" }), h("div", { class: "cuts" }),
      h("div", { class: "sens" }), h("div", { class: "qreview-slot" }), h("div", { class: "note" })),
    h("div", { class: "meta" }, h("span", { class: "badge risk" }), h("span", { class: "badge sensitive" }), h("span", { class: "group" }), h("span", { class: "status" })),
    h("div", { class: "actions" }));
  return row;
}

async function saveCommand(num, value) {
  const item = S.state.queue.find((i) => i.num === num);
  if (!item || item.command === value.trim()) return;
  await guarded(() => api("POST", `/api/queue/${num}`, { command: value }));
}

function elapsed(ts) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

function tickElapsed() {
  for (const item of S.state?.queue || []) {
    if (!item.ran_at || !["ran", "inserted"].includes(item.status)) continue;
    const el = S.rows.get(item.num)?.querySelector(".status");
    if (el) el.textContent = `${item.status} · ${elapsed(item.ran_at)} ago`;
  }
}

function updateRow(row, item) {
  const pending = item.status === "pending";
  row.className = `qitem risk-${item.risk}${HIDDEN_WHEN_DONE.includes(item.status) ? " done" : ""}${item.status === "withdrawn" ? " withdrawn" : ""}`;
  const cmd = $(".cmd", row);
  if (document.activeElement !== cmd) cmd.value = item.command;
  cmd.readOnly = !pending;
  requestAnimationFrame(() => autosize(cmd));
  const sel = $(".sess", row);
  sel.replaceChildren(...sessionOptions(item));
  sel.disabled = !pending;
  $(".purpose", row).textContent = item.purpose + (item.edited ? "  (edited)" : "") + (item.recipe ? `  [recipe ${item.recipe}]` : "");
  $(".rollback", row).textContent = item.rollback ? `Rollback: ${item.rollback}` : (item.risk !== "read_only" && !item.dry_run_of ? "No rollback given" : "");
  $(".rollback", row).classList.toggle("missing", !item.rollback && item.risk !== "read_only" && !item.dry_run_of);
  $(".cuts", row).textContent = item.cuts_session ? `⚠ Cuts this session: ${item.cuts_session}` : "";
  const sensList = item.sensitive || [];
  $(".sens", row).textContent = sensList.length ? `🔍 May expose sensitive data: ${sensList.join("; ")}` : "";
  const sb = $(".badge.sensitive", row);
  sb.textContent = sensList.length ? "sensitive" : "";
  sb.hidden = !sensList.length;
  sb.title = sensList.length ? sensitiveTitle(item) : "";
  $(".qreview-slot", row).replaceChildren(...[reviewLine(item)].filter(Boolean));
  $(".note", row).textContent = item.note ? `Note: ${item.note}` : "";
  const grp = $(".group", row);
  grp.textContent = item.group ? `⇄ ${item.group}` : "";
  grp.title = item.group ? "Paired probe: run the whole group together" : "";
  const badge = $(".risk", row);
  badge.className = `badge risk ${item.risk}`;
  badge.textContent = item.risk.replace("_", " ");
  badge.title = [`Model said: ${item.model_risk}`, ...item.risk_reasons.map((r) => `Local rule: ${r}`)].join("\n");
  const status = $(".status", row);
  status.className = `status ${item.status}`;
  status.textContent = STATUS_LABEL[item.status];
  if (item.ran_at && (item.status === "ran" || item.status === "inserted")) status.textContent += ` · ${elapsed(item.ran_at)} ago`;

  const actions = $(".actions", row);
  const btn = (label, fn, kind = "", title = "") => h("button", { class: `small ${kind}`, title, onclick: () => guarded(fn) }, label);
  const set = (status, extra = {}) => api("POST", `/api/queue/${item.num}`, { status, ...extra });
  const sessOpen = (S.state.sessions || []).some((s) => s.id === item.session_id && !s.exited);
  const btns = [];
  if (pending) {
    const run = btn("Run", () => runItem(item.num, "run"), item.risk === "disruptive" ? "danger" : "primary", "Type into the terminal and press Enter");
    const ins = btn("Insert", () => runItem(item.num, "insert"), "", "Type into the terminal without pressing Enter");
    run.disabled = ins.disabled = !sessOpen;
    btns.push(run, ins);
    if (item.group) btns.push(btn("Run group", () => runGroup(item.group), "", "Type every pending item of this group at the same moment"));
    if (item.risk !== "read_only" && !item.dry_run_of) btns.push(btn("Dry run", () => api("POST", `/api/queue/${item.num}/dry-run`), "", "Queue the rehearsal form of this command first"));
    if (item.risk === "read_only" && !item.watch) btns.push(btn("Watch", () => watchItem(item), "", "Repeat this read-only command for a bounded time and keep only the changes"));
    if (item.risk !== "read_only" || item.sensitive?.length) btns.push(btn("2nd opinion", () => secondOpinion(item), "", "Ask a reviewer model what could go wrong"));
    btns.push(btn("Skip…", () => skipItem(item), "", "Skip, with a reason for the AI"),
      btn("Force skip", () => forceSkip(item), "ghost", "Skip in one click; the AI is told you chose not to run it"),
      btn("↑", () => api("POST", `/api/queue/${item.num}/move`, { delta: -1 }), "ghost", "Move up"),
      btn("↓", () => api("POST", `/api/queue/${item.num}/move`, { delta: 1 }), "ghost", "Move down"));
  } else if (item.status === "ran" || item.status === "inserted") {
    btns.push(btn("Preview", () => previewCapture(item)), btn("Reset", () => set("pending"), "ghost", "Back to pending"));
  } else if (item.status === "skipped") {
    btns.push(btn("Unskip", () => set("pending", { note: "" }), "ghost"));
  } else if (item.status === "withdrawn") {
    btns.push(btn("Restore", () => set("pending", { note: "" }), "ghost", "Put it back in the queue as pending"));
  }
  btns.push(btn("Copy", async () => { await clipWrite(item.command); toast("Command copied.", "ok", 2000); }, "ghost", "Copy the command text"));
  actions.replaceChildren(...btns);
}

function flashQueueItem(num) {
  if (!$("#show-done").checked && HIDDEN_WHEN_DONE.includes(S.state.queue.find((i) => i.num === num)?.status)) {
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
  const rv = item.review?.status === "done" ? item.review : null;
  const rvBox = rv ? h("div", { class: `qreview ${rv.level || ""}` }, h("b", {}, `${(REVIEW_LEVEL[rv.level] || { icon: "•" }).icon} Second opinion (${rv.model}): `),
    rv.summary || rv.verdict, rv.data ? h("div", { class: "qreview-data" }, `🔍 ${rv.data}`) : null) : null;
  if (item.risk !== "disruptive" && rv?.level === "stop") {
    const ok = await confirmModal("The reviewer said not to run this", h("div", {}, rvBox,
      h("pre", { class: "prompt-text" }, item.command), h("div", { class: "pre small" }, rv.text),
      h("p", {}, `Target: ${sess.id} (${sess.target})`)), mode === "run" ? "Run it anyway" : "Insert it anyway", "danger");
    if (!ok) return;
  }
  if (item.risk === "disruptive") {
    const reasons = item.risk_reasons.length ? ` (${item.risk_reasons.join(", ")})` : "";
    const review = h("div", { class: "review hidden" });
    const ok = await confirmModal(item.cuts_session ? "This will cut your own session" : "Disruptive command",
      h("div", {}, h("p", {}, `This command is flagged DISRUPTIVE${reasons}. It may interrupt service, lose data or cut off access.`),
        item.cuts_session ? h("div", { class: "warnbox" }, `Blast radius: it ${item.cuts_session}. You will lose this terminal; make sure you can get back in (console, another path, or a scheduled re-enable).`) : null,
        rvBox,
        h("pre", { class: "prompt-text" }, item.command),
        item.rollback ? h("p", { class: "small" }, `Rollback: ${item.rollback}`) : h("p", { class: "small warn" }, "No rollback was given for this command."),
        h("p", {}, `Target: ${sess.id} (${sess.target})`),
        h("button", { type: "button", class: "small", onclick: async (e) => {
          e.target.disabled = true; review.classList.remove("hidden"); review.textContent = "Asking the reviewer…";
          try { const r = await api("POST", `/api/queue/${num}/review`); review.replaceChildren(h("b", {}, `Second opinion (${r.model}${r.different_model ? "" : ", same model as the proposer"}): `), h("div", { class: "pre" }, r.text)); }
          catch (err) { review.textContent = err.message; }
        } }, rv ? "Ask again" : "Get a second opinion"), review),
      mode === "run" ? "Run it" : "Insert it", "danger");
    if (!ok) return;
  }
  const r = S.rdps[sess.id];
  if (r) {
    if (r.state !== 3) throw new Error(`The remote desktop ${sess.id} is not connected.`);
    if (!r.typedOk) {
      const ok = await confirmModal("Type into the remote desktop", h("div", {},
        h("p", {}, `This types the command into whichever window has focus on ${sess.id}${mode === "run" ? " and presses Enter" : ""}. Click into the right window first (for example an elevated PowerShell).`),
        h("pre", { class: "prompt-text" }, item.command),
        h("p", { class: "muted small" }, "Output isn't captured from a remote desktop: copy it there and use Clipboard → AI, or attach a screenshot. You won't be asked again for this session.")),
      mode === "run" ? "Type and press Enter" : "Type it", "primary");
      if (!ok) return;
      r.typedOk = true;
    }
    activateTab(sess.id);
    await api("POST", `/api/queue/${num}`, { status: mode === "run" ? "ran" : "inserted" });
    await rdpType(r, item.command, mode === "run");
    return;
  }
  activateTab(sess.id);
  const t = S.terms[sess.id];
  if (!t?.ws) throw new Error("Terminal is not connected.");
  // status first: the server records the transcript position, so the output capture starts here
  await api("POST", `/api/queue/${num}`, { status: mode === "run" ? "ran" : "inserted" });
  t.markers.get(num)?.dispose();
  t.markers.set(num, t.term.registerMarker(0));
  t.term.paste(item.command);
  if (mode === "run") termSend(t, { type: "input", data: "\r" });
  t.term.focus();
}

async function runGroup(group) {
  const nums = (await api("GET", `/api/queue/group/${encodeURIComponent(group)}`)).nums;
  if (!nums.length) return toast("Nothing pending in that group.");
  const items = nums.map((n) => S.state.queue.find((i) => i.num === n));
  if (items.some((i) => i.risk !== "read_only")) {
    const ok = await confirmModal("Run paired probes", h("div", {}, h("p", {}, "This group contains non read-only commands:"),
      h("pre", { class: "prompt-text" }, items.map((i) => `#${i.num} [${i.risk}] ${i.session_id}: ${i.command}`).join("\n"))), "Run all", "danger");
    if (!ok) return;
  }
  for (const i of items) {
    const sess = (S.state.sessions || []).find((s) => s.id === i.session_id);
    const ready = S.rdps[i.session_id] ? S.rdps[i.session_id].state === 3 : S.terms[i.session_id]?.ws;
    if (!sess || sess.exited || !ready) throw new Error(`Session ${i.session_id} is not open or not connected.`);
  }
  // record all start positions first, then type everything in one go so the starts line up
  await Promise.all(items.map((i) => api("POST", `/api/queue/${i.num}`, { status: "ran" })));
  for (const i of items) {
    if (S.rdps[i.session_id]) { rdpType(S.rdps[i.session_id], i.command, true); continue; }
    const t = S.terms[i.session_id];
    t.markers.get(i.num)?.dispose();
    t.markers.set(i.num, t.term.registerMarker(0));
    t.term.paste(i.command);
    termSend(t, { type: "input", data: "\r" });
  }
  toast(`Started ${items.length} probes together.`, "ok", 3000);
}

function watchItem(item) {
  const interval = h("input", { type: "number", value: 10, min: 1, style: "width:80px" });
  const count = h("input", { type: "number", value: 30, min: 1, max: 720, style: "width:80px" });
  modal({
    title: `Watch #${item.num}`,
    body: h("div", { class: "field" }, h("pre", { class: "prompt-text" }, item.command),
      h("div", { class: "row" }, h("label", {}, "every ", interval, " s"), h("label", {}, " for ", count, " samples")),
      h("div", { class: "muted small" }, "The command is wrapped in a bounded loop. When you send the result, iterations identical to the previous one are dropped, so the AI sees only what changed.")),
    buttons: [{ label: "Cancel" }, { label: "Make it a watch", kind: "primary",
      onClick: () => api("POST", `/api/queue/${item.num}/watch`, { interval: Number(interval.value), count: Number(count.value) }) }],
  });
}

async function secondOpinion(item) {
  const body = h("div", { class: "muted" }, h("span", { class: "spinner" }), " Asking the reviewer…");
  const m = modal({ title: `Second opinion on #${item.num}`, wide: true, body, buttons: [{ label: "Close" }] });
  try {
    const r = await api("POST", `/api/queue/${item.num}/review`);
    m.box.querySelector(".content").replaceChildren(h("pre", { class: "prompt-text" }, item.command),
      h("div", { class: "muted small" }, `Reviewer: ${r.model} (${r.tier})${r.different_model ? "" : " — same model as the proposer; set a different reviewer in Settings → Model"}`),
      h("div", { class: "pre" }, r.text));
  } catch (e) {
    m.box.querySelector(".content").replaceChildren(h("div", { class: "warnbox" }, e.message));
  }
}

// ------------------------------------------------------------------ recipes, rollback ledger, baselines

function activeSession() {
  return (S.state?.sessions || []).find((s) => s.id === S.activeSid && !s.exited) || null;
}

function osFamily(sess) {
  const t = `${sess?.kind || ""} ${sess?.shell || ""} ${sess?.os_hint || ""}`.toLowerCase();
  return sess?.kind === "winrm" || t.includes("windows") || t.includes("powershell") ? "windows" : "linux";
}

async function openRecipes() {
  hideMenus();
  const sess = activeSession();
  if (!sess) return toast("Open and select a session first.");
  const all = (await api("GET", "/api/recipes")).recipes;
  const fam = osFamily(sess);
  const list = h("div", { class: "recipe-list" });
  const search = h("input", { type: "text", placeholder: "Filter…" });
  const render = () => {
    const q = search.value.toLowerCase();
    const shown = all.filter((r) => (r.os === "any" || r.os === fam) && (!q || `${r.id} ${r.name} ${r.tags.join(" ")}`.toLowerCase().includes(q)));
    list.replaceChildren(...shown.map((r) => h("details", { class: "recipe" },
      h("summary", {}, h("b", {}, r.name), " ", h("span", { class: "muted small" }, `${r.id} · ${r.steps.length} step(s)${r.baseline ? " · baseline" : ""}${r.source !== "builtin" ? ` · ${r.source}` : ""}`)),
      r.description ? h("div", { class: "muted small" }, r.description) : null,
      h("ol", { class: "steps" }, r.steps.map((st) => h("li", {}, h("code", { class: "mono" }, st.command), h("div", { class: "muted small" }, `${st.purpose} · ${st.risk.replace("_", " ")}`)))),
      h("div", { class: "row" },
        h("button", { class: "small primary", onclick: () => guarded(async () => { await api("POST", `/api/recipes/${r.id}/queue`, { session_id: sess.id }); toast(`Queued ${r.name} for ${sess.id}.`, "ok"); }) }, `Queue for ${sess.id}`),
        r.install ? h("button", { class: "small", title: r.install, onclick: () => guarded(async () => { await api("POST", `/api/recipes/${r.id}/queue`, { session_id: sess.id, include_install: true }); toast(`Queued with install step.`, "ok"); }) }, "Queue with install") : null))));
    if (!shown.length) list.append(h("div", { class: "muted" }, "No recipes match."));
  };
  search.addEventListener("input", render);
  render();
  modal({ title: `Recipes for ${sess.id} (${fam})`, wide: true,
    body: h("div", { style: "display:flex;flex-direction:column;gap:8px" },
      h("div", { class: "muted small" }, "Each step lands in the queue as a normal item. Your own recipes go in ~/.config/datoolkit/recipes/*.toml."), search, list),
    buttons: [{ label: "Close" }] });
}

async function openRollbackLedger() {
  hideMenus();
  const items = (await api("GET", "/api/rollback")).items;
  if (!items.length) return toast("No state-changing commands have run in this case.");
  const checks = items.map((i) => h("input", { type: "checkbox", checked: i.has_rollback, disabled: !i.has_rollback }));
  modal({ title: "Rollback ledger", wide: true,
    body: h("div", { style: "display:flex;flex-direction:column;gap:8px" },
      h("div", { class: "muted small" }, "Changes that ran, newest first. Queuing rollbacks adds them as normal items in this order; nothing runs until you click Run."),
      ...items.map((i, idx) => h("label", { class: `result-block${i.has_rollback ? "" : " excluded"}` },
        h("div", { class: "head" }, checks[idx], h("b", {}, `#${i.num}`), h("span", { class: `badge risk ${i.risk}` }, i.risk.replace("_", " ")), h("span", { class: "muted" }, i.session_id), h("code", { class: "mono" }, i.command)),
        h("div", { class: "small" }, i.has_rollback ? `↩ ${i.rollback}` : "No rollback recorded for this item.")))),
    buttons: [{ label: "Close" }, { label: "Queue selected rollbacks", kind: "primary", onClick: async () => {
      const nums = items.filter((_, idx) => checks[idx].checked).map((i) => i.num);
      const r = await api("POST", "/api/rollback/queue", { nums });
      toast(`Queued ${r.items.length} rollback item(s).`, "ok");
    } }] });
}

async function baselineAction(act) {
  hideMenus();
  const sess = activeSession();
  if (!sess) return toast("Open and select a session first.");
  if (act === "queue") {
    const fam = osFamily(sess);
    await api("POST", `/api/recipes/baseline-${fam}/queue`, { session_id: sess.id });
    return toast(`Baseline snapshot queued for ${sess.id}. Run the items, then use Save baseline.`, "ok", 8000);
  }
  if (act === "save") {
    const r = await api("POST", "/api/baselines/save", { session_id: sess.id });
    return toast(`Baseline saved for ${r.host}: ${r.sections.join(", ")}`, "ok", 8000);
  }
  if (act === "diff") {
    const r = await api("POST", "/api/baselines/diff", { session_id: sess.id });
    const ta = h("textarea", { class: "mono", rows: 18, spellcheck: "false", value: r.text });
    const message = h("textarea", { rows: 2, placeholder: "Message for the AI (optional)", value: "Baseline diff for this host; changed sections are leads." });
    modal({ title: `Baseline diff · ${r.host}`, wide: true,
      body: h("div", { style: "display:flex;flex-direction:column;gap:8px" },
        h("div", { class: "muted small" }, `Against the snapshot taken ${r.taken}. Changed: ${r.changed.join(", ") || "nothing"}. Unchanged: ${r.same.join(", ") || "nothing"}.`),
        ta, message),
      buttons: [{ label: "Close" }, { label: "Send to AI", kind: "primary",
        onClick: () => api("POST", "/api/send", { message: message.value.trim(), snippets: [{ session_id: sess.id, text: ta.value }] }) }] });
  }
}

// ------------------------------------------------------------------ context view & timeline

async function openContextView() {
  hideMenus();
  const ctx = await api("GET", "/api/context");
  const checks = ctx.groups.map(() => h("input", { type: "checkbox" }));
  modal({ title: "What the AI knows", wide: true,
    body: h("div", { style: "display:flex;flex-direction:column;gap:8px" },
      h("div", { class: "muted small" }, `About ${fmtTokens(ctx.total_tokens)} tokens will be sent on the next turn (estimate). Tick exchanges to remove them from the AI's context; the chat and audit log keep them.`),
      h("details", {}, h("summary", {}, `System prompt · ~${fmtTokens(ctx.system_tokens)} tokens`), h("pre", { class: "prompt-text", style: "max-height:30vh;overflow:auto" }, ctx.system)),
      ...ctx.groups.map((g, i) => h("label", { class: "result-block" }, h("div", { class: "head" }, checks[i], h("b", {}, `Exchange ${i + 1}`),
        h("span", { class: "muted small" }, `~${fmtTokens(g.tokens)} tokens · ${g.messages} message(s)`)), h("div", { class: "small mono" }, g.summary)))),
    buttons: [{ label: "Close" }, { label: "Remove ticked", kind: "danger", onClick: async () => {
      const groups = ctx.groups.map((_, i) => i).filter((i) => checks[i].checked);
      if (!groups.length) throw new Error("Nothing ticked.");
      await api("POST", "/api/context/drop", { groups });
      toast(`Removed ${groups.length} exchange(s) from the AI's context.`, "ok");
    } }] });
}

async function openTimeline() {
  hideMenus();
  const tl = await api("GET", "/api/case/timeline");
  if (!tl.events.length) return toast("No events yet.");
  const t0 = tl.events[0].ts, t1 = Math.max(tl.events[tl.events.length - 1].ts, ...Object.values(tl.sessions).flat().map((x) => x[0]));
  const slider = h("input", { type: "range", min: 0, max: 1000, value: 1000, style: "width:100%" });
  const clock = h("span", { class: "mono" });
  const evList = h("div", { class: "timeline" });
  const sids = Object.keys(tl.sessions);
  const sidSel = h("select", {}, sids.map((s) => h("option", { value: s }, s)));
  const termPre = h("pre", { class: "prompt-text", style: "max-height:35vh;overflow:auto;flex:1" });
  const transcripts = {};
  let playing = null;
  const at = () => t0 + (t1 - t0) * (Number(slider.value) / 1000);
  const render = async () => {
    const t = at();
    clock.textContent = new Date(t * 1000).toLocaleTimeString();
    evList.replaceChildren(...tl.events.filter((e) => e.ts <= t).slice(-60).map((e) =>
      h("div", { class: `tl-ev ${e.kind}` }, h("span", { class: "muted mono small" }, new Date(e.ts * 1000).toLocaleTimeString()), " ", e.text)));
    evList.scrollTop = evList.scrollHeight;
    const sid = sidSel.value;
    if (!sid) return;
    if (!transcripts[sid]) transcripts[sid] = (await api("GET", `/api/case/transcript?sid=${encodeURIComponent(sid)}`)).text;
    const times = tl.sessions[sid];
    let off = 0;
    for (const [ts, o] of times) { if (ts <= t) off = o; else break; }
    const bytes = new TextEncoder().encode(transcripts[sid]).slice(0, off);
    termPre.textContent = new TextDecoder().decode(bytes).slice(-6000);
    termPre.scrollTop = termPre.scrollHeight;
  };
  slider.addEventListener("input", render);
  sidSel.addEventListener("change", render);
  const play = h("button", { type: "button", onclick: () => {
    if (playing) { clearInterval(playing); playing = null; play.textContent = "▶ Play"; return; }
    if (Number(slider.value) >= 1000) slider.value = 0;
    play.textContent = "⏸ Pause";
    playing = setInterval(() => { slider.value = Math.min(1000, Number(slider.value) + 4); render(); if (Number(slider.value) >= 1000) play.click(); }, 200);
  } }, "▶ Play");
  modal({ title: `Timeline · ${tl.case.name}`, wide: true, onClose: () => playing && clearInterval(playing),
    body: h("div", { style: "display:flex;flex-direction:column;gap:8px" },
      h("div", { class: "row" }, play, clock, h("span", { class: "spacer" }), sids.length ? h("label", {}, "Terminal ", sidSel) : null),
      slider,
      h("div", { class: "row", style: "align-items:stretch;gap:10px" }, h("div", { style: "flex:1;min-width:0" }, evList), sids.length ? termPre : null)),
    buttons: [{ label: "Close" }] });
  render();
}

async function openSimilar() {
  hideMenus();
  const q = h("input", { type: "text", placeholder: "Symptoms, host type, error text…", value: S.state.case?.name || "" });
  const list = h("div", { class: "case-list" });
  const run = async () => {
    list.replaceChildren(h("div", { class: "muted small" }, "Searching…"));
    const cases = (await api("GET", `/api/search?q=${encodeURIComponent(q.value)}`)).cases;
    list.replaceChildren(...(cases.length ? cases.map((c) => h("div", { class: "case-row", style: "flex-direction:column;align-items:stretch" },
      h("div", { class: "row" }, h("b", {}, c.name), h("span", { class: `badge ${c.sensitivity}` }, c.sensitivity), h("span", { class: "muted small" }, `${c.started.slice(0, 10)} · matched ${c.matched.join(", ")}`), h("span", { class: "spacer" }), c.has_runbook ? h("span", { class: "badge read_only" }, "runbook") : null),
      c.runbook_preview ? h("pre", { class: "prompt-text small", style: "max-height:120px;overflow:auto" }, c.runbook_preview) : null))
      : [h("div", { class: "muted small" }, "No similar cases.")]));
  };
  q.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); guarded(run); } });
  modal({ title: "Similar past cases", wide: true,
    body: h("div", { style: "display:flex;flex-direction:column;gap:8px" }, h("div", { class: "row" }, q, h("button", { type: "button", onclick: () => guarded(run) }, "Search")), list),
    buttons: [{ label: "Close" }] });
  if (q.value) guarded(run);
}

// Ctrl+Shift+Enter: run the first pending read-only item whose session is open. Items flagged
// sensitive, or that the reviewer said not to run, need a deliberate click.
function runNextReadOnly() {
  const item = (S.state?.queue || []).find((i) => i.status === "pending" && i.risk === "read_only"
    && !i.sensitive?.length && i.review?.level !== "stop"
    && (S.state.sessions || []).some((s) => s.id === i.session_id && !s.exited));
  if (!item) return toast("No pending read-only command with an open session (sensitive ones need a click).");
  flashQueueItem(item.num);
  guarded(() => runItem(item.num, "run"));
}

function isGlobalShortcut(e) {
  if (e.altKey && !e.ctrlKey && !e.metaKey && /^Digit[1-9]$/.test(e.code)) return true;
  if (e.ctrlKey && e.shiftKey && (e.key === "Enter" || e.code === "KeyK")) return true;
  return false;
}

function globalKeys(e) {
  if (!isGlobalShortcut(e) || document.querySelector(".overlay")) return;
  e.preventDefault();
  if (e.altKey) {
    const sessions = S.state?.sessions || [];
    const idx = Number(e.code.slice(5)) - 1;
    if (sessions[idx]) activateTab(sessions[idx].id);
  } else if (e.key === "Enter") runNextReadOnly();
  else if (e.code === "KeyK") $("#chat-input").focus();
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

// One click, no dialog: the AI is told the technician chose not to run it (see Engine.send).
function forceSkip(item) {
  return api("POST", `/api/queue/${item.num}`, { status: "skipped", note: "" });
}

function warnList(prev, cap) {
  const out = [];
  if (cap?.error) out.push(h("div", { class: "warnbox" }, `${cap.error}; paste the output manually.`));
  if (cap?.source === "transcript") out.push(h("div", { class: "muted small" }, "Captured from the transcript file (the terminal buffer no longer had it)."));
  if (prev?.sensitive?.length) out.push(h("div", { class: "warnbox sens" }, h("b", {}, "🔍 This command may have printed sensitive data: "),
    `${prev.sensitive.join("; ")}. Redaction is best-effort: read the text below and remove anything the AI doesn't need.`));
  if (prev?.warnings?.length) out.push(h("div", { class: "warnbox" }, h("b", {}, "Possible prompt injection: "),
    `${prev.warnings.join("; ")}. The AI is told to ignore instructions in output, but check before sending.`));
  return out;
}

async function previewCapture(item) {
  const cap = await captureFor(item);
  modal({
    title: `Captured output for #${item.num}`, wide: true,
    body: h("div", {}, ...warnList(null, cap),
      h("pre", { class: "prompt-text", style: "max-height:60vh;overflow:auto" }, cap.text || "(empty)")),
    buttons: [{ label: "Close" }],
  });
}

async function openSendResults(draft = "") {
  const items = readyItems();
  if (!items.length) return toast("Nothing ready to send. Run or skip queue items first.");
  const caps = await Promise.all(items.map((i) => (i.status === "skipped" ? { text: "" } : captureFor(i))));
  const prev = (await api("POST", "/api/preview", { texts: caps.map((c) => c.text), nums: items.map((i) => i.num) })).items;
  const images = [];
  const thumbs = h("div", { class: "thumbs" });
  const addShot = (sid) => guarded(async () => {
    needVision();
    const r = S.rdps[sid];
    if (!r || r.state !== 3) throw new Error(`The remote desktop ${sid} is not connected.`);
    if (images.length >= 4) throw new Error("Up to four images per message.");
    const shot = await redactImage(r.client.getDisplay().flatten().toDataURL("image/png"), `Screenshot of ${sid}: black out anything sensitive`);
    if (!shot) return;
    images.push(await shrinkImage(shot));
    thumbs.replaceChildren(...images.map((d) => h("img", { src: d, class: "thumb" })));
  });
  const blocks = items.map((item, idx) => {
    const include = h("input", { type: "checkbox", checked: true });
    const skipped = item.status === "skipped";
    const rdp = !!caps[idx].rdp;
    const ta = skipped ? null : h("textarea", { spellcheck: "false", value: prev[idx].text,
      placeholder: rdp ? "Output isn't captured from a remote desktop: paste it here, or attach a screenshot below." : "" });
    const rdpTools = rdp && !skipped ? h("div", { class: "row" },
      h("button", { type: "button", class: "small", "data-needs-vision": true, onclick: () => addShot(item.session_id) }, "📸 Screenshot of the desktop"),
      h("button", { type: "button", class: "small", title: "Text last copied in the remote desktop",
        onclick: () => { const r = S.rdps[item.session_id]; if (r?.clipboard) { ta.value = r.clipboard; autosize(ta); } else toast("Copy the output in the remote desktop first."); } }, "Copied text")) : null;
    const note = h("input", { type: "text", placeholder: "Note to the AI (optional)", value: item.note || "" });
    const info = [];
    if (prev[idx].redactions) info.push(`${prev[idx].redactions} redaction(s) applied`);
    if (prev[idx].truncated) info.push("truncated");
    if (prev[idx].collapsed) info.push(`${prev[idx].collapsed} unchanged watch iteration(s) dropped`);
    const block = h("div", { class: "result-block" },
      h("label", { class: "head" }, include, h("b", {}, `#${item.num}`),
        h("span", { class: `status ${item.status}` }, item.status.toUpperCase()),
        h("span", { class: "muted" }, item.session_id), h("code", { class: "mono" }, item.command)),
      ...warnList(prev[idx], caps[idx]),
      ta, rdpTools, info.length ? h("div", { class: "muted small" }, info.join(" · ")) : null, note);
    include.addEventListener("change", () => block.classList.toggle("excluded", !include.checked));
    if (ta) setTimeout(() => autosize(ta), 30);
    return { item, include, ta, note };
  });
  const message = h("textarea", { rows: 2, placeholder: "Add a message for the AI (optional)", value: draft });
  setTimeout(renderVision, 0);
  modal({
    title: "Review results before sending", wide: true,
    body: h("div", { style: "display:flex;flex-direction:column;gap:10px" },
      h("div", { class: "muted small" }, "This exact text is what the AI receives. Edit or untick anything that shouldn't be shared."),
      ...blocks.map((b) => b.include.closest(".result-block")), thumbs, h("div", { class: "field" }, h("span", {}, "Message"), message)),
    buttons: [{ label: "Cancel" }, {
      label: "Send to AI", kind: "primary", onClick: async () => {
        const results = blocks.filter((b) => b.include.checked)
          .map((b) => ({ num: b.item.num, text: b.ta ? b.ta.value : "", note: b.note.value.trim() }));
        if (!results.length && !message.value.trim()) throw new Error("Nothing selected.");
        await api("POST", "/api/send", { message: message.value.trim(), results, images });
        if (draft && $("#chat-input").value === draft) $("#chat-input").value = "";
      },
    }],
  });
}

async function sendSelection() {
  const r = S.rdps[S.activeSid];
  if (r) return sendRdpClipboard(r);
  const t = S.terms[S.activeSid];
  const sel = t?.term.getSelection() || "";
  if (!sel.trim()) return toast("Select some text in the terminal first.");
  await sendExcerpt(S.activeSid, sel, `Send terminal excerpt from ${S.activeSid}`);
}

async function sendExcerpt(sid, text, title) {
  const prev = (await api("POST", "/api/preview", { texts: [text] })).items[0];
  const ta = h("textarea", { spellcheck: "false", class: "mono", rows: 12, value: prev.text });
  const message = h("textarea", { rows: 2, placeholder: "Add a message for the AI (optional)" });
  modal({
    title, wide: true,
    body: h("div", { style: "display:flex;flex-direction:column;gap:8px" },
      prev.redactions || prev.truncated ? h("div", { class: "muted small" },
        [prev.redactions ? `${prev.redactions} redaction(s) applied` : "", prev.truncated ? "truncated" : ""].filter(Boolean).join(" · ")) : null,
      ...warnList(prev, null),
      ta, h("div", { class: "field" }, h("span", {}, "Message"), message)),
    buttons: [{ label: "Cancel" }, {
      label: "Send to AI", kind: "primary",
      onClick: () => api("POST", "/api/send", { message: message.value.trim(), snippets: [{ session_id: sid, text: ta.value }] }),
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
      yesNo && !/certificate/i.test(p.text) ? h("div", { class: "muted small" }, "Verify the fingerprint with the device owner or console before accepting.") : null,
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
    st.case && st.chat.length ? h("div", { class: "muted small" }, "The current case's log stays on disk and can be resumed later. Open sessions carry over; the conversation and queue start fresh.") : null);
  const resumeList = h("div", { class: "case-list" }, h("div", { class: "muted small" }, "Loading…"));
  const filter = h("input", { type: "search", placeholder: "Filter by name, notes or date" });
  const selAll = h("input", { type: "checkbox", title: "Select every case shown" });
  const delSel = h("button", { type: "button", class: "small danger", disabled: true }, "Delete selected");
  const tools = h("div", { class: "row case-tools" }, h("label", { class: "check" }, selAll, "All"), filter, delSel);
  const resumeBox = h("details", { class: "resume" }, h("summary", {}, "Previous cases (resume or delete)"), tools, resumeList);
  body.append(resumeBox);
  let m;
  let cases = [];
  const chosen = new Set();
  const shown = () => {
    const q = filter.value.trim().toLowerCase();
    return q ? cases.filter((c) => `${c.name} ${c.notes} ${c.started} ${c.sensitivity}`.toLowerCase().includes(q)) : cases;
  };
  const syncBulk = () => {
    const vis = shown();
    delSel.disabled = !chosen.size;
    delSel.textContent = chosen.size ? `Delete selected (${chosen.size})` : "Delete selected";
    selAll.checked = vis.length > 0 && vis.every((c) => chosen.has(c.id));
    selAll.indeterminate = !selAll.checked && vis.some((c) => chosen.has(c.id));
  };
  const remove = async (list) => {
    const what = list.length === 1 ? `the case "${list[0].name}"` : `${list.length} cases`;
    if (!(await confirmModal("Delete cases", h("div", {},
      h("p", {}, `Permanently delete ${what}? This removes the conversation, command queue, audit log, terminal transcripts, AI request log, images and any runbook from disk.`),
      list.length > 1 ? h("ul", { class: "small" }, ...list.slice(0, 12).map((c) => h("li", {}, c.name)), list.length > 12 ? h("li", {}, `…and ${list.length - 12} more`) : null) : null,
      h("p", { class: "muted small" }, "Exports you saved elsewhere are not touched. This can't be undone.")), "Delete", "danger"))) return;
    const r = await api("POST", "/api/cases/delete", { ids: list.map((c) => c.id) });
    for (const id of r.deleted) chosen.delete(id);
    cases = cases.filter((c) => !r.deleted.includes(c.id));
    draw();
    if (r.deleted.length) toast(`Deleted ${r.deleted.length} case(s).`, "ok");
    if (r.errors.length) toast(`Not deleted: ${r.errors.join("; ")}`, "error", 10000);
  };
  const draw = () => {
    const vis = shown();
    resumeList.replaceChildren(...(vis.length ? vis.map((c) => {
      const pick = h("input", { type: "checkbox", checked: chosen.has(c.id), title: "Select for deletion",
        onchange: (e) => { if (e.target.checked) chosen.add(c.id); else chosen.delete(c.id); syncBulk(); } });
      return h("div", { class: "case-row" },
        h("label", { class: "case-pick" }, pick,
          h("div", {}, h("b", {}, c.name), " ", h("span", { class: `badge ${c.sensitivity}` }, c.sensitivity),
            h("div", { class: "muted small" }, `${(c.started || c.id).replace("T", " ")} · ${c.resumable ? `${c.messages} message(s)` : "log only (no saved conversation)"}`))),
        h("div", { class: "row" },
          h("button", { type: "button", class: "small primary", onclick: () => guarded(async () => {
            await api("POST", "/api/case/open", { id: c.id });
            m.close();
            toast(`Resumed ${c.name}.`, "ok");
          }) }, "Open"),
          h("button", { type: "button", class: "small ghost danger", title: "Delete this case from disk", onclick: () => guarded(() => remove([c])) }, "Delete")));
    }) : [h("div", { class: "muted small" }, cases.length ? "No case matches the filter." : "No previous cases.")]));
    syncBulk();
  };
  filter.addEventListener("input", draw);
  selAll.addEventListener("change", () => {
    for (const c of shown()) { if (selAll.checked) chosen.add(c.id); else chosen.delete(c.id); }
    draw();
  });
  delSel.addEventListener("click", () => guarded(() => remove(cases.filter((c) => chosen.has(c.id)))));
  resumeBox.addEventListener("toggle", async () => {
    if (!resumeBox.open) return;
    try {
      cases = (await api("GET", "/api/cases")).cases.filter((c) => c.id !== st.case?.id);
      draw();
    } catch (e) {
      resumeList.replaceChildren(h("div", { class: "warnbox" }, e.message));
    }
  });
  m = modal({
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

// One model picker for every place a model is chosen: search, provider switch, capability and
// tier badges, blocked models the case's sensitivity doesn't allow. `filter` narrows the list
// (the vision helper shows only vision models); `onPick(provider, model)` does the choosing.
function pickModel({ title, current = "", filter = null, filterNote = "", note = "", onPick, extraButtons = [] }) {
  const st = S.state;
  const providers = st.config.providers;
  if (!providers.length) {
    toast("Add an AI provider first.");
    return openSettings("providers");
  }
  const [curProv, curModel] = current.includes("|") ? current.split("|", 2) : ["", ""];
  let prov = curProv && providers.some((p) => p.name === curProv) ? curProv : (st.config.active_provider || providers[0].name);
  let models = [];
  const search = h("input", { type: "text", placeholder: "Filter models… (e.g. opus, glm, kimi, tee)" });
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
    const pool = filter ? models.filter(filter) : models;
    const shown = pool.filter((x) => !q || x.id.toLowerCase().includes(q) || x.tier.includes(q));
    shown.sort((a, b) => (b.allowed - a.allowed) || ((recent.indexOf(a.id) + 1 || 99) - (recent.indexOf(b.id) + 1 || 99)) || a.id.localeCompare(b.id));
    listEl.replaceChildren(...shown.slice(0, 400).map((x) => h("div", {
      class: `model-row${x.allowed ? "" : " blocked"}${x.id === curModel && prov === curProv ? " current" : ""}`,
      title: x.allowed ? x.id : `Not permitted for a ${sens} case`,
      onclick: async () => {
        if (!x.allowed) return;
        await guarded(async () => { await onPick(prov, x.id); m.close(); });
      },
    }, h("span", { class: "id" }, x.id), recent.includes(x.id) ? h("span", { class: "muted small" }, "recent") : null,
    x.reasoning ? h("span", { class: "cap", title: `Reasoning model${x.efforts?.length ? `: effort ${x.efforts.join(" / ")}` : ""}` }, "🧠") : null,
    h("span", { class: `cap${x.vision ? "" : " off"}`, title: x.vision === true ? "Reads images" : x.vision === false ? "Text only: can't read images" : "Unknown whether it reads images (set it under Settings → Providers)" },
      x.vision === true ? "👁" : x.vision === false ? "text only" : "?"),
    h("span", { class: `badge ${x.tier}` }, x.tier))));
    if (!shown.length) {
      listEl.append(h("div", { class: "muted" }, pool.length || !filter ? "No matching models." : `No ${filterNote || "matching"} models from ${prov}.`));
    }
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
    title, wide: true,
    body: h("div", { style: "display:flex;flex-direction:column;gap:8px" },
      note ? h("div", { class: "small" }, note) : null,
      h("div", { class: "muted small" }, legend, " Tier is detected from the model id; override it in Settings → Providers."),
      h("div", { class: "muted small" }, filter ? `Showing ${filterNote} models only.` : "👁 reads images · text only: needs a vision helper (Settings → Model) for screenshots and photos · 🧠 reasoning model."),
      h("div", { class: "row" }, providers.length > 1 ? provSel : null, search,
        h("button", { type: "button", onclick: () => load(true) }, "Refresh")),
      listEl),
    buttons: [...extraButtons.map((b) => ({ ...b, onClick: async () => { await b.onClick(); } })), { label: "Close" }],
  });
  setTimeout(() => search.focus(), 40);
  load(false);
  return m;
}

function openModelPicker() {
  const c = S.state.config;
  pickModel({ title: "Choose model", current: c.active_model ? `${c.active_provider}|${c.active_model}` : "",
    onPick: (prov, model) => api("POST", "/api/model", { provider: prov, model }) });
}

// A setting that holds a model ("provider|model"): shows the choice, opens the picker, and
// offers "none". Saves as soon as a model is picked.
function modelSetting({ key, value, noneLabel, pickTitle, filter, filterNote, note, onSaved }) {
  const shown = h("span", { class: "mono" }, value ? value.replace("|", " · ") : noneLabel);
  const save = async (v) => {
    await api("POST", "/api/settings", { [key]: v });
    shown.textContent = v ? v.replace("|", " · ") : noneLabel;
    toast("Saved.", "ok", 2000);
    onSaved?.(v);
  };
  return h("div", { class: "model-setting" }, shown, h("span", { class: "spacer" }),
    h("button", { type: "button", onclick: () => pickModel({ title: pickTitle, current: value, filter, filterNote, note,
      onPick: (prov, model) => save(`${prov}|${model}`) }) }, "Choose…"),
    h("button", { type: "button", class: "ghost", onclick: () => guarded(() => save("")) }, noneLabel.startsWith("None") ? "None" : "Clear"));
}

// ------------------------------------------------------------------ settings

function openSettings(tab = "providers") {
  const tabs = h("div", { class: "tabs-inline" });
  const pane = h("div", { style: "display:flex;flex-direction:column;gap:10px" });
  const show = (name) => {
    for (const b of tabs.children) b.classList.toggle("active", b.dataset.tab === name);
    pane.replaceChildren(({ providers: providersPane, hosts: hostsPane, model: modelPane, tools: toolsPane, general: generalPane })[name]());
  };
  // The state event from a save can arrive after the HTTP reply, so fetch the config before redrawing a list.
  const reshow = async (name) => {
    S.state.config = (await api("GET", "/api/state")).config;
    show(name);
  };
  for (const [key, label] of [["providers", "AI providers"], ["model", "Model"], ["hosts", "Hosts"], ["tools", "Tool cache"], ["general", "General"]]) {
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
          await guarded(async () => {
            await api("DELETE", `/api/providers/${encodeURIComponent(p.name)}`);
            await reshow("providers");
          });
        }
      } }, "Delete"))) : h("div", { class: "muted" }, "No providers yet."));
    return h("div", { style: "display:flex;flex-direction:column;gap:10px" }, list,
      h("div", { class: "row" },
        h("button", { class: "primary", onclick: () => pane.replaceChildren(providerForm({ name: "NanoGPT", base_url: "https://nano-gpt.com/api/v1", _new: true })) }, "Add NanoGPT"),
        h("button", { onclick: () => pane.replaceChildren(providerForm({ name: "Local", base_url: "http://localhost:11434/v1", _new: true })) }, "Add local (Ollama / LM Studio / vLLM)"),
        h("button", { onclick: () => pane.replaceChildren(providerForm({ name: "", base_url: "", _new: true })) }, "Add other"),
        h("button", { title: "A scripted fake model for practising the workflow. Nothing leaves this machine.",
          onclick: () => pane.replaceChildren(providerForm({ name: "Training", base_url: "training://disk-full", _new: true })) }, "Add training provider")));
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
    const visionOv = h("textarea", { rows: 2, class: "mono",
      value: Object.entries(p.vision_overrides || {}).map(([k, v]) => `${k} = ${v}`).join("\n"),
      placeholder: "One per line: model-id = yes | no   (for providers that don't report it, e.g. local models)" });
    const status = h("div", { class: "muted small" });
    const payload = () => {
      const tier_overrides = {};
      for (const line of overrides.value.split("\n")) {
        const [k, v] = line.split("=").map((s) => (s || "").trim());
        if (k && v) tier_overrides[k] = v.toLowerCase();
      }
      const vision_overrides = {};
      for (const line of visionOv.value.split("\n")) {
        const [k, v] = line.split("=").map((s) => (s || "").trim());
        if (k && v) vision_overrides[k] = v.toLowerCase();
      }
      return { provider: { name: name.value.trim(), base_url: url.value.trim(), default_model: def.value.trim(), tier_overrides, vision_overrides },
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
      h("label", { class: "field" }, h("span", {}, "Reads images (overrides)"), visionOv),
      h("div", { class: "muted small" }, "Tiers: STANDARD = normal cloud; TEE = runs in an enclave, attested before anything is sent (Intel TDX quote, Intel's revocation lists and TCB, NVIDIA's GPU verdict) and each reply's signature checked, but the prompt passes the provider's gateway in the clear (TEE/, phala/); E2EE = sealed on this machine to an attested enclave (NanoGPT private/… models, attested with Tinfoil's verifier); LOCAL = your own hardware (localhost/private IP URLs)."),
      status,
      h("div", { class: "row" },
        h("button", { onclick: () => guarded(() => reshow("providers")) }, "Back"),
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
          await reshow("providers");
        }) }, "Save")));
  }

  function hostsPane() {
    const hosts = S.state.config.hosts;
    const list = h("div", { class: "list" }, hosts.length ? hosts.map((x) => h("div", { class: "list-item" },
      h("div", { class: "grow" }, h("b", {}, x.name), "  ",
        h("span", { class: "muted small" }, `${x.kind.toUpperCase()} ${x.user ? x.user + "@" : ""}${x.host}${x.port ? ":" + x.port : ""} · ${x.auth}${x.os_hint ? " · " + x.os_hint : ""}`)),
      x.has_password ? h("span", { class: "small" }, "🔑") : null,
      x.kind === "rdp" ? h("span", { class: "small", title: x.pinned ? `Pinned certificate SHA-256 ${x.pinned}` : "No certificate pinned yet" }, x.pinned ? "📌 cert pinned" : "") : null,
      h("button", { class: "small", onclick: () => pane.replaceChildren(hostForm(x)) }, "Edit"),
      h("button", { class: "small danger", onclick: async () => {
        if (await confirmModal("Delete host", `Delete ${x.name} and any stored password?`, "Delete", "danger")) {
          await guarded(async () => {
            await api("DELETE", `/api/hosts/${encodeURIComponent(x.name)}`);
            await reshow("hosts");
          });
        }
      } }, "Delete"))) : h("div", { class: "muted" }, "No saved hosts yet."));
    return h("div", { style: "display:flex;flex-direction:column;gap:10px" }, list,
      h("div", { class: "row" },
        h("button", { class: "primary", onclick: () => pane.replaceChildren(hostForm({ kind: "ssh", auth: "agent", _new: true })) }, "Add SSH host"),
        h("button", { onclick: () => pane.replaceChildren(hostForm({ kind: "winrm", auth: "ntlm", winrm_ssl: true, winrm_cert_validation: true, _new: true })) }, "Add WinRM host"),
        h("button", { title: "Remote desktop through guacd (sudo apt install guacd)",
          onclick: () => pane.replaceChildren(hostForm({ kind: "rdp", auth: "password", rdp_security: "any", rdp_layout: "en-us-qwerty", _new: true })) }, "Add RDP host")));
  }

  function hostForm(x) {
    const ssh = x.kind === "ssh";
    const rdp = x.kind === "rdp";
    const layouts = ["en-us-qwerty", "en-gb-qwerty", "de-de-qwertz", "de-ch-qwertz", "fr-fr-azerty", "fr-be-azerty", "fr-ch-qwertz",
      "it-it-qwerty", "es-es-qwerty", "es-latam-qwerty", "pt-br-qwerty", "sv-se-qwerty", "da-dk-qwerty", "no-no-qwerty", "hu-hu-qwertz", "ja-jp-qwerty", "tr-tr-qwerty", "failsafe"];
    const f = {
      name: h("input", { type: "text", value: x.name || "", placeholder: "e.g. acme-fs01" }),
      host: h("input", { type: "text", value: x.host || "", placeholder: "hostname or IP" }),
      port: h("input", { type: "number", value: x.port || "", placeholder: ssh ? "22" : rdp ? "3389" : (x.winrm_ssl === false ? "5985" : "5986") }),
      user: h("input", { type: "text", value: x.user || "", placeholder: ssh ? "username" : rdp ? "DOMAIN\\user or user" : "DOMAIN\\user or user@domain" }),
      rdp_security: h("select", {}, [["any", "Negotiate (any)"], ["nla", "NLA"], ["nla-ext", "NLA (extended)"], ["tls", "TLS"], ["rdp", "Legacy RDP security"]]
        .map(([v, l]) => h("option", { value: v, selected: v === (x.rdp_security || "any") }, l))),
      rdp_layout: h("select", {}, layouts.map((v) => h("option", { value: v, selected: v === (x.rdp_layout || "en-us-qwerty") }, v))),
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
      h("div", { class: "row" }, field("User", f.user), rdp ? field("Security", f.rdp_security) : field(ssh ? "Authentication" : "Auth method", f.auth)),
      rdp ? field("Keyboard layout on the remote machine", f.rdp_layout) : null,
      field("Password", f.password),
      ssh ? h("div", { class: "row" }, field("Key file", f.key_file), field("Jump host", f.jump)) : null,
      ssh ? field("Extra SSH options", f.ssh_options) : null,
      ssh || rdp ? null : h("div", { class: "row" },
        h("label", { class: "check" }, f.winrm_ssl, "HTTPS (5986)"),
        h("label", { class: "check" }, f.winrm_cert_validation, "Validate certificate")),
      field("OS / device hint for the AI", f.os_hint),
      rdp ? h("div", { class: "muted small" }, x.pinned
        ? `Pinned certificate SHA-256: ${x.pinned}. A different certificate blocks the connection until you forget this pin.`
        : "No certificate pinned yet: you'll be shown the server's certificate on first connection and asked to trust it.") : null,
      rdp && x.pinned ? h("button", { class: "small", style: "align-self:flex-start", onclick: () => guarded(async () => {
        if (!(await confirmModal("Forget pinned certificate", `Forget the pinned certificate for ${x.name}? Only do this if you know why it changed (server rebuilt, certificate renewed).`, "Forget", "danger"))) return;
        await api("POST", `/api/hosts/${encodeURIComponent(x.name)}/forget-pin`);
        x.pinned = "";
        toast("Pin removed. You'll be asked to verify the certificate on the next connection.", "ok");
        pane.replaceChildren(hostForm(x));
      }) }, "Forget pinned certificate") : null,
      x.has_password ? h("button", { class: "small", style: "align-self:flex-start", onclick: () => guarded(async () => {
        await api("POST", `/api/hosts/${encodeURIComponent(x.name)}/forget-password`);
        toast("Password removed from keyring.", "ok");
      }) }, "Forget stored password") : null,
      h("div", { class: "row" },
        h("button", { onclick: () => guarded(() => reshow("hosts")) }, "Back"), h("span", { class: "spacer" }),
        h("button", { class: "primary", onclick: () => guarded(async () => {
          await api("POST", "/api/hosts", {
            host: {
              name: f.name.value.trim(), kind: x.kind, host: f.host.value.trim(), port: f.port.value ? Number(f.port.value) : null,
              user: f.user.value.trim(), auth: rdp ? "password" : f.auth.value, key_file: f.key_file.value.trim(), jump: f.jump.value.trim(),
              rdp_security: f.rdp_security.value, rdp_layout: f.rdp_layout.value,
              ssh_options: f.ssh_options.value.split("\n"), winrm_ssl: f.winrm_ssl.checked,
              winrm_cert_validation: f.winrm_cert_validation.checked, os_hint: f.os_hint.value.trim(),
            },
            password: f.password.value || null, original_name: x._new ? null : x.name,
          });
          toast("Host saved.", "ok");
          await reshow("hosts");
        }) }, "Save")));
  }

  function toolsPane() {
    const box = h("div", { style: "display:flex;flex-direction:column;gap:10px" });
    const list = h("div", { class: "case-list" });
    const sess = activeSession();
    const load = async () => {
      const tools = (await api("GET", "/api/tools")).tools;
      list.replaceChildren(...(tools.length ? tools.map((t) => h("div", { class: "case-row" },
        h("div", {}, h("b", {}, t.name), " ", h("span", { class: `badge ${t.status === "ok" ? "read_only" : "disruptive"}` }, t.status),
          h("div", { class: "muted small" }, `${t.file} · ${t.os} · ${(t.size / 1048576).toFixed(1)} MB · sha256 ${t.sha256.slice(0, 12)}…`),
          t.notes ? h("div", { class: "small" }, t.notes) : null, t.run ? h("div", { class: "small mono" }, `run: ${t.run}`) : null),
        h("div", { class: "row" },
          h("button", { class: "small primary", disabled: !sess || t.status !== "ok", title: sess ? `Queue a transfer to ${sess.id}` : "Select a session first",
            onclick: () => guarded(async () => { const r = await api("POST", `/api/tools/${encodeURIComponent(t.name)}/transfer`, { session_id: sess.id }); toast(`Transfer queued as #${r.nums.join(", #")}. Expected sha256 ${r.expected_sha256.slice(0, 12)}…`, "ok", 8000); }) }, "Send to session"),
          h("button", { class: "small ghost", onclick: () => guarded(async () => { if (await confirmModal("Remove tool", `Remove ${t.name} from the cache?`, "Remove", "danger")) { await api("DELETE", `/api/tools/${encodeURIComponent(t.name)}`); load(); } }) }, "Remove"))))
        : [h("div", { class: "muted small" }, "No tools cached yet.")]));
    };
    const f = { path: h("input", { type: "text", placeholder: "/path/to/WizTree64.exe" }), name: h("input", { type: "text", placeholder: "WizTree" }),
      os: h("select", {}, ["windows", "linux", "any"].map((o) => h("option", { value: o }, o))),
      notes: h("input", { type: "text", placeholder: "Where it came from, licence, what it does" }),
      run: h("input", { type: "text", placeholder: "How to run it once copied, e.g. & $env:TEMP\\WizTree64.exe /export=C:\\wiztree.csv /admin=1" }) };
    box.append(h("div", { class: "muted small" }, "Vetted portable CLI tools, hashed and version-pinned. \"Send to session\" queues an scp (SSH hosts, from a local shell) or an inline PowerShell write (WinRM, under 4 MB); the hash is checked on arrival. Nothing runs without your click."),
      list, h("h3", {}, "Add a tool"),
      h("div", { class: "row" }, h("label", { class: "field" }, h("span", {}, "File on this machine"), f.path), h("label", { class: "field" }, h("span", {}, "Name"), f.name), h("label", { class: "field" }, h("span", {}, "OS"), f.os)),
      h("label", { class: "field" }, h("span", {}, "Notes"), f.notes), h("label", { class: "field" }, h("span", {}, "Run command"), f.run),
      h("div", { class: "row" }, h("span", { class: "spacer" }), h("button", { class: "primary", onclick: () => guarded(async () => {
        await api("POST", "/api/tools", { path: f.path.value.trim(), name: f.name.value.trim(), os: f.os.value, notes: f.notes.value, run: f.run.value });
        toast("Tool added.", "ok"); f.path.value = f.name.value = ""; load();
      }) }, "Add")));
    load();
    return box;
  }

  function helperAttestation() {
    const v = visionState();
    const lab = v.mode === "helper" ? attestLabel(S.state.helper_attestation) : null;
    if (!lab) return h("span", {});
    const att = S.state.helper_attestation;
    return h("div", { class: `vision-now ${lab.cls === "ok" ? "native" : lab.cls === "bad" ? "none" : "helper"}` },
      h("b", {}, `Vision helper ${v.helper}: ${lab.text}`), h("div", { class: "muted small pre" }, lab.title),
      att.status === "verified" || att.status === "failed" ? h("button", { type: "button", class: "small", onclick: () => guarded(async () => {
        const r = await api("POST", "/api/attest", { slot: "helper" });
        toast(r.status === "verified" ? `Vision helper attested: ${r.summary}` : `Attestation ${r.kind === "tee" ? "refused" : "failed"}: ${r.error}`, r.status === "verified" ? "ok" : "error", 10000);
        show("model");
      }) }, "Re-check") : null);
  }

  function modelPane() {
    const g = S.state.config.settings.generation || {};
    const num = (key, attrs = {}) => h("input", { type: "number", value: g[key] ?? "", placeholder: "model default", ...attrs });
    const f = {
      temperature: num("temperature", { step: 0.05, min: 0, max: 2 }),
      top_p: num("top_p", { step: 0.05, min: 0, max: 1 }),
      max_tokens: num("max_tokens", { step: 256, min: 1 }),
      frequency_penalty: num("frequency_penalty", { step: 0.1, min: -2, max: 2 }),
      presence_penalty: num("presence_penalty", { step: 0.1, min: -2, max: 2 }),
      seed: num("seed", { step: 1, min: 0 }),
      reasoning_effort: h("select", {}, [["", "model default"], ["none", "none (no reasoning)"], ["minimal", "minimal"], ["low", "low"],
        ["medium", "medium"], ["high", "high"], ["xhigh", "extra high"], ["max", "max"]].map(([v, l]) => h("option", { value: v, selected: (g.reasoning_effort || "") === v }, l))),
    };
    const field = (label, el, hint) => h("label", { class: "field" }, h("span", {}, label), el, hint ? h("div", { class: "muted small" }, hint) : null);
    const helper = modelSetting({ key: "vision_model", value: S.state.config.settings.vision_model || "",
      noneLabel: "None (image features off for text-only models)", pickTitle: "Choose a vision helper",
      filter: (m) => m.vision === true, filterNote: "vision",
      note: "The vision helper describes each image for a chat model that can't read images. Only models that read images are listed.",
      onSaved: () => setTimeout(() => show("model"), 300) });
    const v = visionState();
    const autoReview = h("select", { disabled: !S.state.config.settings.review_model,
      onchange: (e) => guarded(async () => { await api("POST", "/api/settings", { auto_review: e.target.value }); toast("Saved.", "ok", 2000); }) },
      [["off", "Off"], ["disruptive", "Disruptive commands"], ["flagged", "Everything flagged (modifying, disruptive or sensitive)"]]
        .map(([val, l]) => h("option", { value: val, selected: val === (S.state.config.settings.auto_review || "off") }, l)));
    const save = async () => {
      const generation = Object.fromEntries(Object.entries(f).map(([k, el]) => [k, el.value]));
      await api("POST", "/api/settings", { generation });
      toast("Model settings saved.", "ok");
    };
    return h("div", { style: "display:flex;flex-direction:column;gap:10px" },
      h("h3", {}, "Generation"),
      h("div", { class: "muted small" }, "Sent with every request. Blank means the model's own default. Reasoning effort only goes to reasoning models, moved to the nearest level each model accepts (e.g. Kimi and GLM take low / high / max)."),
      h("div", { class: "row" }, field("Temperature", f.temperature, "0 = focused, 1 = varied. 0.3 suits diagnostics."),
        field("Reasoning effort", f.reasoning_effort, "Higher thinks longer: slower and more tokens.")),
      h("div", { class: "row" }, field("Max output tokens", f.max_tokens), field("Top-p", f.top_p)),
      h("div", { class: "row" }, field("Frequency penalty", f.frequency_penalty), field("Presence penalty", f.presence_penalty), field("Seed", f.seed)),
      h("div", { class: "row" }, h("span", { class: "spacer" }),
        h("button", { type: "button", onclick: () => { for (const [k, el] of Object.entries(f)) el.value = { temperature: 0.3, reasoning_effort: "low" }[k] ?? ""; } }, "Reset to defaults")),
      h("h3", {}, "Images"),
      h("div", { class: `vision-now ${v.mode}` }, v.mode === "native" ? `The current model (${v.model}) reads images directly.`
        : v.mode === "helper" ? `The current model (${v.model}) can't read images; ${v.helper} describes them for it.`
        : `Image features are off: ${v.why}`),
      helperAttestation(),
      field("Vision helper", helper, "Used only when the chat model can't read images: it describes each image once (text exactly, then the rest), and the chat model gets the description. That adds a request per image, so replies with images take longer. It must be allowed by the case's sensitivity. With no helper, image features are disabled for text-only models."),
      h("h3", {}, "Second opinion"),
      field("Reviewer model", modelSetting({ key: "review_model", value: S.state.config.settings.review_model || "",
        noneLabel: "Clear (use the chat model)", pickTitle: "Choose the second-opinion reviewer",
        note: "The reviewer sees only the command and case notes, never the proposer's reasoning. A different model gives a more independent opinion; a fast, cheap one suits automatic reviews.",
        onSaved: () => setTimeout(() => show("model"), 300) }),
        "It must be allowed by the case's sensitivity. With none set, the chat model answers the 2nd opinion button."),
      field("Automatic review", autoReview,
        S.state.config.settings.review_model
          ? "Asked in the background as each command is queued; the verdict shows on the item. It only adds warnings: it never lowers a risk level or clears a flag. Each review is one request to the reviewer model."
          : "Choose a reviewer model first: automatic reviews never fall back to the chat model."),
      h("div", { class: "row" }, h("span", { class: "spacer" }), h("button", { class: "primary", onclick: () => guarded(save) }, "Save generation settings")));
  }

  function generalPane() {
    const s = S.state.config.settings;
    const num = (v) => h("input", { type: "number", value: v });
    const f = { capture_max_lines: num(s.capture_max_lines), capture_max_chars: num(s.capture_max_chars),
      scrollback: num(s.scrollback), font_size: num(s.font_size), context_warn_tokens: num(s.context_warn_tokens || 100000),
      companion_port: num(s.companion_port || 48443) };
    const sel = (value, opts) => h("select", {}, opts.map(([v, l]) => h("option", { value: v, selected: v === value }, l)));
    const nanos = S.state.config.providers.filter((p) => /(^|\.)nano-gpt\.com$/.test((() => { try { return new URL(p.base_url).hostname; } catch { return ""; } })()));
    const search = {
      search_mode: sel(s.search_mode || "ask", [["ask", "Ask me before each search"], ["auto", "Search without asking (Open cases only)"], ["off", "Off"]]),
      search_provider: sel(s.search_provider || "kagi", ["kagi", "perplexity", "linkup", "tavily", "exa", "brave", "valyu"].map((x) => [x, x])),
      search_via: sel(s.search_via || "", [["", nanos.length ? `First NanoGPT provider (${nanos[0].name})` : "No NanoGPT provider configured"], ...nanos.map((p) => [p.name, p.name])]),
    };
    const testQ = h("input", { type: "text", placeholder: "Test query, e.g. OPNsense 25.1 release notes" });
    const testOut = h("div", { class: "muted small" });
    return h("div", { style: "display:flex;flex-direction:column;gap:10px" },
      h("div", { class: "row" },
        h("label", { class: "field" }, h("span", {}, "Max lines per result"), f.capture_max_lines),
        h("label", { class: "field" }, h("span", {}, "Max characters per result"), f.capture_max_chars)),
      h("div", { class: "row" },
        h("label", { class: "field" }, h("span", {}, "Terminal scrollback (new sessions)"), f.scrollback),
        h("label", { class: "field" }, h("span", {}, "Terminal font size (new sessions)"), f.font_size)),
      h("div", { class: "row" },
        h("label", { class: "field" }, h("span", {}, "Warn when prompt tokens exceed"), f.context_warn_tokens),
        h("label", { class: "field" }, h("span", {}, "Phone companion port (HTTPS)"), f.companion_port)),
      h("div", { class: "muted small" }, "The phone companion always uses this port, so a firewall only needs this one open (e.g. sudo ufw allow <port>/tcp). Changing it while the companion runs restarts it, and phones pair again."),
      h("h3", {}, "Web search"),
      h("div", { class: "row" },
        h("label", { class: "field" }, h("span", {}, "AI web searches"), search.search_mode),
        h("label", { class: "field" }, h("span", {}, "Search provider"), search.search_provider),
        h("label", { class: "field" }, h("span", {}, "Paid with the key of"), search.search_via)),
      h("div", { class: "muted small" }, "Searches go through NanoGPT to the provider in the clear, whatever the chat model's tier, and are billed to that NanoGPT key. Sovereign cases never search; Confidential cases always ask. Each query is redacted first and you can edit it before it runs."),
      h("div", { class: "row" }, testQ, h("button", { type: "button", onclick: () => guarded(async () => {
        testOut.textContent = "Searching…";
        await api("POST", "/api/settings", Object.fromEntries(Object.entries(search).map(([k, el]) => [k, el.value])));
        try {
          const r = await api("POST", "/api/web-search-test", { query: testQ.value });
          testOut.replaceChildren(`✓ ${r.provider}: ${r.count} result(s)${typeof r.cost === "number" ? `, cost $${r.cost.toFixed(3)}` : ""}. ${r.note ? r.note + "." : ""}`,
            ...r.results.map((x) => h("div", {}, `· ${x.title || "(untitled)"} ${x.url}`)));
        } catch (e) { testOut.textContent = `✗ ${e.message}`; }
      }) }, "Save & test search")),
      testOut,
      h("div", { class: "muted small" }, "Shortcuts: Alt+1…9 switch terminal tabs · Ctrl+Shift+Enter runs the next pending read-only command · Ctrl+Shift+K focuses the chat · Ctrl+Shift+C/V copy/paste in the terminal."),
      h("div", { class: "muted small" }, `Case logs are stored under ${S.state.case ? S.state.case.dir.replace(/\/[^/]+$/, "") : "~/.local/share/datoolkit/cases"}.`),
      h("div", { class: "row" }, h("span", { class: "spacer" }), h("button", { class: "primary", onclick: () => guarded(async () => {
        await api("POST", "/api/settings", { ...Object.fromEntries(Object.entries(f).map(([k, el]) => [k, Number(el.value)])),
          ...Object.fromEntries(Object.entries(search).map(([k, el]) => [k, el.value])) });
        toast("Settings saved.", "ok");
      }) }, "Save")));
  }
}

// ------------------------------------------------------------------ phone companion

function renderPhoneBtn() {
  const b = $("#phone-btn");
  b.textContent = S.phoneRunning ? (S.phones ? `📱 ${S.phones}` : "📱 on") : "📱";
  b.classList.toggle("on", !!S.phoneRunning);
}

// Pairing takes two scans (see companion.py): the first opens a page with no secret so the
// certificate's fingerprint can be checked on the phone; only then is the code with the token shown.
// A phone already paired can skip to the connect step: one that hasn't accepted this certificate
// (new, or its browser forgot) gets the browser's warning there, before the token is sent, and is
// sent back to step 1.
async function openPhone() {
  let info = await api("GET", "/api/phone");
  let step = 1;
  const body = h("div", { class: "phone" });
  const act = (action, confirmText) => guarded(async () => {
    if (confirmText && !(await confirmModal("Phone companion", confirmText, "Continue", "danger"))) return;
    info = await api("POST", `/api/phone/${action}`);
    if (action !== "token") step = 1;
    render();
  });
  const fp = () => {
    const pairs = (info.fingerprint || "").split(":");
    const rows = [];
    for (let i = 0; i < pairs.length; i += 8) rows.push(pairs.slice(i, i + 8).join(":"));
    return h("div", { class: "fingerprint" }, rows.map((r) => h("div", {}, r)));
  };
  const qr = (src, caption, isUrl = true) => h("div", { class: "qr" }, h("img", { src, alt: "QR code" }),
    h("div", { class: `small muted${isUrl ? " mono url" : ""}` }, caption));
  function render() {
    S.phoneRunning = info.running; S.phones = info.phones; renderPhoneBtn();
    if (!info.running) {
      body.replaceChildren(h("div", { class: "phone" },
        h("p", {}, "Follow the case on your phone: the AI's last message, the hypotheses and the queue, with \"I ran it\" and \"Skip\" for commands you type at a console away from this machine. The phone can't reach a terminal, change settings or send anything to the AI."),
        h("p", {}, `It is served over HTTPS on port ${info.port} of this machine (change it in Settings → General). With a firewall on, open that port once, e.g. `, h("code", {}, `sudo ufw allow ${info.port}/tcp`), "."),
        info.fingerprint ? h("div", {}, h("div", { class: "small muted" }, `This install's certificate (SHA-256, expires ${info.expires}):`), fp()) : null,
        h("div", { class: "row" }, h("span", { class: "spacer" }), h("button", { class: "primary", onclick: () => act("start") }, "Start"))));
      return;
    }
    const head = h("div", { class: "row small" }, h("span", {}, `Serving on ${info.ip}:${info.port}`), h("span", { class: "muted" },
      info.phones ? ` · ${info.phones} phone${info.phones > 1 ? "s" : ""} connected` : " · no phone connected"), h("span", { class: "spacer" }),
      h("button", { class: "small", title: "Disconnect paired phones; they scan the connect code again", onclick: () => act("token") }, "New code"),
      h("button", { class: "small", title: "Make a new certificate; every phone checks the new fingerprint again",
        onclick: () => act("certificate", "Make a new certificate? Every phone will have to accept it and check its fingerprint again.") }, "New certificate"),
      h("button", { class: "small danger", onclick: () => act("stop") }, "Stop"));
    if (step === 1) {
      body.replaceChildren(head,
        h("h3", {}, "Step 1 of 2: check the certificate"),
        h("div", { class: "phone-step" }, qr(info.pair_qr, info.pair_url), h("div", {},
          h("p", {}, "Scan this with the phone. The browser warns that the connection isn't private: accept it, then open the certificate details (the page on the phone says where) and compare its SHA-256 fingerprint with this one:"),
          fp(),
          h("p", { class: "small" }, "Check every pair, not just the first and last few: someone in the middle can make a certificate whose ends match."),
          h("p", { class: "small muted" }, "Phone already paired? Its browser has accepted this certificate, so skip to step 2. If it warns there after all, come back here."),
          h("div", { class: "row" }, h("button", { class: "danger", onclick: () => act("stop") }, "It doesn't match: stop"),
            h("span", { class: "spacer" }), h("button", { onclick: () => { step = 2; render(); } }, "Already paired: skip"),
            h("button", { class: "primary", onclick: () => { step = 2; render(); } }, "Fingerprint matches")))));
    } else {
      body.replaceChildren(head,
        h("h3", {}, "Step 2 of 2: connect"),
        h("div", { class: "phone-step" }, qr(info.connect_qr, "Contains this session's token: don't share or photograph it", false), h("div", {},
          h("p", {}, "Scan this with the phone and open it in the browser you checked the certificate in."),
          h("div", { class: "warnbox" }, h("b", {}, "If the browser warns that the connection isn't private, don't continue. "),
            "Close that page and check the certificate (step 1) first. The browser warns when this phone has never accepted this certificate, when it has forgotten it (browsers do after a while), and when someone is in the middle: it's the same warning, so treat it the same way. Without a warning, the token only reaches this machine."),
          h("p", { class: "small muted" }, "The code changes every time the companion starts. New code disconnects phones paired with the old one."),
          h("div", { class: "row" }, h("button", { onclick: () => { step = 1; render(); } }, "Check the certificate (step 1)")))));
    }
  }
  render();
  S.phoneDialog = { refresh: async () => { info = await api("GET", "/api/phone"); render(); } };
  modal({ title: "Phone companion", body, wide: true, buttons: [{ label: "Close" }], onClose: () => { S.phoneDialog = null; } });
}

// ------------------------------------------------------------------ export

// Save a file where the technician chooses: the native Save dialog in the app window, the
// browser's download otherwise. Returns the path (app window), "downloaded", or null.
async function saveFile(name, blob) {
  if (DESKTOP) {
    const res = await fetch(`/api/desktop/save?name=${encodeURIComponent(name)}`, { method: "POST", headers: { "X-Token": TOKEN }, body: blob });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || data.detail || `${res.status} ${res.statusText}`);
    const path = data.path;
    if (path) toast(`Saved: ${path}`, "ok", 8000);
    return path || null;
  }
  const url = URL.createObjectURL(blob);
  const a = h("a", { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
  return "downloaded";
}

function openFullExport() {
  const terms = h("input", { type: "checkbox" });
  modal({ title: "Full export",
    body: h("div", { style: "display:flex;flex-direction:column;gap:8px" },
      h("div", {}, "A ZIP with everything the AI was sent and everything it returned:"),
      h("ul", { class: "small" },
        h("li", {}, "every model request, in order: the system prompt (in full whenever it changed), the messages sent, the model's reasoning and its reply with tool calls"),
        h("li", {}, "the images exactly as the model received them"),
        h("li", {}, "the Markdown transcript and any ticket summary, client update or runbook"),
        h("li", {}, "raw data: request log, conversation, chat, queue, hypotheses and the audit log")),
      h("label", { class: "check" }, terms, "Also include the raw terminal transcripts (unredacted: everything the terminals showed)"),
      h("div", { class: "muted small" }, "Cases started before this version have no request log; their earlier conversation is reconstructed and marked as such.")),
    buttons: [{ label: "Cancel" }, { label: "Export…", kind: "primary", onClick: async () => {
      const r = await fetch(`/api/export/full?terminals=${terms.checked}`, { headers: { "X-Token": TOKEN } });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `${r.status} ${r.statusText}`);
      const name = (r.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/)?.[1] || "full-export.zip";
      const where = await saveFile(name, await r.blob());
      if (!where) return true;          // cancelled: keep the dialog open
    } }] });
}

async function doExport(act) {
  hideMenus();
  if (act === "markdown") {
    const r = await guarded(() => api("POST", "/api/export/markdown"));
    if (r) await guarded(() => saveFile(r.filename, new Blob([r.content], { type: "text/markdown" })));
  } else if (act === "full") {
    openFullExport();
  } else if (act === "folder") {
    await guarded(() => api("POST", "/api/open-folder"));
  } else if (act === "timeline") {
    await guarded(openTimeline);
  } else if (act === "context") {
    await guarded(openContextView);
  } else if (act === "similar") {
    await guarded(openSimilar);
  } else if (act === "summary" || act === "client" || act === "runbook") {
    const titles = { summary: "Ticket summary", client: "Client update", runbook: "Runbook" };
    const m = modal({ title: titles[act], wide: true, body: h("div", { class: "muted" }, h("span", { class: "spinner" }), " Asking the AI…"), buttons: [{ label: "Close" }] });
    try {
      const r = await api("POST", `/api/export/${act}`);
      const ta = h("textarea", { rows: 18, value: r.text });
      const file = { summary: "ticket-summary", client: "client-update", runbook: "runbook" }[act];
      m.box.querySelector(".content").replaceChildren(ta, h("div", { class: "muted small" }, `A copy is in the case folder: ${r.path}`),
        h("div", { class: "row" },
          h("button", { class: "primary", onclick: () => { clipWrite(ta.value); toast("Copied.", "ok"); } }, "Copy"),
          h("button", { onclick: () => guarded(() => saveFile(`${S.state.case.id}-${file}.md`, new Blob([ta.value], { type: "text/markdown" }))) }, "Save as…")));
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
    pending = setTimeout(() => {
      fitTerm(S.terms[S.activeSid], true);
      if (S.rdps[S.activeSid]) rdpResize(S.rdps[S.activeSid]);
    }, 60);
  }).observe($("#terms"));
}

function init() {
  setupSplitters();
  $("#chat-form").addEventListener("submit", sendChat);
  $("#chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) sendChat(e);
    else chatHistoryKey(e);
  });
  document.addEventListener("keydown", globalKeys);
  // DAToolkit's own shortcuts still work while the remote desktop has the keyboard
  $("#terms").addEventListener("keydown", (e) => {
    if (e.target.closest?.(".rdp-view") && isGlobalShortcut(e)) { e.stopPropagation(); globalKeys(e); }
  }, true);
  setInterval(tickElapsed, 10000);
  setInterval(() => { if (S.state?.busy) renderStatus(); }, 1000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) document.title = "DAToolkit"; });
  $("#shot-btn").addEventListener("click", () => guarded(attachScreenshot));
  $("#tab-shot-btn").addEventListener("click", () => guarded(attachScreenshot));
  $("#stop-btn").addEventListener("click", () => guarded(() => api("POST", "/api/stop")));
  $("#case-btn").addEventListener("click", () => openCaseModal(false));
  $("#model-btn").addEventListener("click", openModelPicker);
  $("#attest-btn").addEventListener("click", () => guarded(async () => {
    const r = await api("POST", "/api/attest");
    toast(r.status === "verified" ? `${r.kind === "tee" ? "TEE" : "Enclave"} attested: ${r.summary}` : `Attestation ${r.kind === "tee" ? "refused" : "failed"}: ${r.error}`, r.status === "verified" ? "ok" : "error", 10000);
  }));
  $("#settings-btn").addEventListener("click", () => openSettings());
  $("#phone-btn").addEventListener("click", () => guarded(openPhone));
  api("GET", "/api/phone").then((i) => { S.phoneRunning = i.running; S.phones = i.phones; renderPhoneBtn(); }).catch(() => {});
  $("#vision-btn").addEventListener("click", () => openSettings("model"));
  $("#export-btn").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu($("#export-menu")); });
  $("#export-menu").addEventListener("click", (e) => { const a = e.target.closest("button")?.dataset.act; if (a) doExport(a); });
  $("#new-session-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    if (!S.state.case) return toast("Start a case first.");
    renderSessionMenu();
    toggleMenu($("#session-menu"));
  });
  document.addEventListener("click", (e) => { if (!e.target.closest(".menu")) hideMenus(); });
  $("#send-results-btn").addEventListener("click", () => guarded(() => openSendResults()));
  // from the composer: whatever is typed becomes the message sent with the results
  $("#chat-results-btn").addEventListener("click", () => guarded(() => openSendResults($("#chat-input").value.trim() ? $("#chat-input").value : "")));
  $("#tools-btn").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu($("#tools-menu")); });
  $("#tools-menu").addEventListener("click", (e) => {
    const a = e.target.closest("button")?.dataset.act;
    if (a === "recipes") guarded(openRecipes);
    else if (a === "rollback") guarded(openRollbackLedger);
    else if (a?.startsWith("baseline-")) guarded(() => baselineAction(a.slice(9)));
  });
  $("#attach-btn").addEventListener("click", () => $("#photo-input").click());
  $("#photo-input").addEventListener("change", async (e) => { const files = [...e.target.files]; e.target.value = ""; for (const f of files) await guarded(() => attachPhoto(f)); });
  $("#chat-input").addEventListener("paste", async (e) => {
    const files = [...(e.clipboardData?.items || [])].filter((it) => it.type.startsWith("image/")).map((it) => it.getAsFile());
    for (const f of files) await guarded(() => attachPhoto(f));
  });
  $("#hyp-toggle").addEventListener("click", () => $("#hyp-list").classList.toggle("hidden"));
  $("#send-selection-btn").addEventListener("click", () => guarded(sendSelection));
  $("#show-done").addEventListener("change", renderQueue);
  connectEvents();
}

init();
