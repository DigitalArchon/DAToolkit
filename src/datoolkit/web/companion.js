"use strict";
/* Phone companion: read the chat and queue, mark items done or skipped. No terminal access. */
// The pairing code puts the token in the fragment (never sent in a request line); keep it for
// reloads in this tab only, and take it out of the address bar and history.
const TOKEN = (() => {
  const fromHash = new URLSearchParams(location.hash.slice(1)).get("t");
  try {
    if (fromHash) sessionStorage.setItem("dat-companion", fromHash);
    history.replaceState(null, "", "/companion");
    return fromHash || sessionStorage.getItem("dat-companion");
  } catch { return fromHash; }
})();
const $ = (s) => document.querySelector(s);
let lastSpoken = "";

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v; else if (k.startsWith("on")) el.addEventListener(k.slice(2), v); else el.setAttribute(k, v);
  }
  for (const c of kids.flat()) if (c !== null && c !== undefined) el.append(c instanceof Node ? c : String(c));
  return el;
}
async function api(method, path, body) {
  const r = await fetch(path, { method, headers: { "X-Token": TOKEN, "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || d.detail || r.statusText);
  return d;
}
function toast(t) { const el = h("div", { class: "toast error" }, t); $("#toasts").append(el); setTimeout(() => el.remove(), 5000); }

function render(st) {
  $("#case").textContent = st.case ? `${st.case.name} · ${st.case.sensitivity}` : "no case";
  const last = [...st.chat].reverse().find((e) => e.kind === "assistant");
  const text = last ? last.text : (st.busy ? "Thinking…" : "—");
  $("#ai-text").textContent = text;
  if ($("#speak").checked && last && last.text && last.text !== lastSpoken && "speechSynthesis" in window) {
    lastSpoken = last.text;
    speechSynthesis.cancel();
    speechSynthesis.speak(new SpeechSynthesisUtterance(last.text.replace(/[`*#_]/g, "").slice(0, 1200)));
  }
  const hyps = st.hypotheses || [];
  $("#hyps").classList.toggle("hidden", !hyps.length);
  $("#hyp-list").replaceChildren(...hyps.map((x) => h("div", { class: "hyp" },
    h("span", { class: `badge ${x.status === "ruled_out" ? "" : x.status === "supported" ? "read_only" : "modifying"}` }, x.status.replace("_", " ")),
    h("span", { style: "flex:2" }, x.text), h("div", { class: "bar" }, h("div", { style: `width:${Math.round(x.confidence * 100)}%` })))));
  const items = (st.queue || []).filter((i) => i.status !== "sent" && i.status !== "withdrawn");
  $("#queue").replaceChildren(...(items.length ? items.map((i) => h("div", { class: `card risk-${i.risk}` },
    h("div", { class: "row" }, h("b", {}, `#${i.num}`), h("span", { class: `badge risk ${i.risk}` }, i.risk.replace("_", " ")),
      h("span", { class: "muted small" }, i.session_id), h("span", { class: "spacer" }), h("span", { class: `status ${i.status}` }, i.status)),
    h("div", { class: "cmd" }, i.command), h("div", { class: "muted small" }, i.purpose),
    i.cuts_session ? h("div", { class: "warnbox" }, `Cuts your session: ${i.cuts_session}`) : null,
    i.status === "pending" ? h("div", { class: "big" },
      h("button", { class: "primary", onclick: () => mark(i.num, "ran") }, "I ran it"),
      h("button", { onclick: () => mark(i.num, "skipped") }, "Skip")) :
    (i.status === "ran" || i.status === "skipped") ? h("div", { class: "big" }, h("button", { class: "ghost", onclick: () => mark(i.num, "pending") }, "Undo")) : null))
    : [h("div", { class: "muted" }, "No pending commands.")]));
}
async function mark(num, status) {
  try { await api("POST", `/api/companion/queue/${num}`, { status, note: status === "skipped" ? "skipped from companion" : "" }); }
  catch (e) { toast(e.message); }
}
function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/events?t=${encodeURIComponent(TOKEN)}`);
  ws.onmessage = (m) => { const ev = JSON.parse(m.data); if (ev.type === "state") render(ev.state); };
  ws.onclose = (e) => {
    if (e.code === 4403) { toast("Disconnected: DAToolkit stopped the companion or issued a new code. Scan the connect code again."); return; }
    setTimeout(reconnect, 2000);
  };
}
// After a drop, only reconnect while the token is still good (a restarted companion has a new one).
async function reconnect() {
  try { render(await api("GET", "/api/companion/state")); connect(); }
  catch (e) {
    if (e instanceof TypeError) setTimeout(reconnect, 3000);  // unreachable for now: keep trying
    else toast("Disconnected: DAToolkit stopped the companion or issued a new code. Scan the connect code again.");
  }
}
if (!TOKEN) toast("No pairing token. Scan the connect code on the DAToolkit screen.");
else { api("GET", "/api/companion/state").then(render).catch((e) => toast(e.message)); connect(); }
