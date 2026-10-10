"use strict";
/* Phone companion: read the chat and queue, mark items done or skipped, send a photo with a
   description to the AI. No terminal access. */
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
let lastSpoken = "", lastText = "", photosAllowed = false;

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
function toast(t, kind = "error") { const el = h("div", { class: `toast ${kind}` }, t); $("#toasts").append(el); setTimeout(() => el.remove(), 5000); }

// ---- read aloud: only messages that arrive after it is switched on, plus ▶ Read on demand.
// Spoken a sentence at a time: the browsers' own pause()/resume() are unreliable (Android Chrome
// stops and can't resume), and Chrome cuts long utterances off. Pause cancels and remembers the
// sentence; resume starts that sentence again.
const canSpeak = "speechSynthesis" in window;
const speech = { text: "", parts: [], i: 0, state: "idle", current: null };  // idle | playing | paused

function sentences(text) {
  // a line break ends a sentence (lists, headings) unless the line already did
  const clean = text.replace(/[`*#_]/g, "").replace(/([.!?;:])?\s*\n+\s*/g, (_, stop) => `${stop || "."} `)
    .replace(/\s+/g, " ").trim();
  const out = [];
  for (const s of clean.match(/.+?(?:[.!?;:](?=\s|$)|$)/g) || []) {  // a stop followed by a space: not 10.1.0.10
    let rest = s.trim();
    while (rest.length > 220) {  // very long runs: break at a comma or space
      const cut = Math.max(rest.lastIndexOf(", ", 220), rest.lastIndexOf(" ", 220));
      out.push(rest.slice(0, cut > 40 ? cut + 1 : 220).trim());
      rest = rest.slice(cut > 40 ? cut + 1 : 220);
    }
    if (out.length && out[out.length - 1].length + rest.length < 80) out[out.length - 1] += " " + rest;
    else if (rest.trim()) out.push(rest.trim());
  }
  return out;
}
function speakNext() {
  if (speech.state !== "playing") return;
  if (speech.i >= speech.parts.length) { speech.state = "idle"; speech.current = null; speechButtons(); return; }
  const u = new SpeechSynthesisUtterance(speech.parts[speech.i]);
  const done = () => { if (speech.current === u && speech.state === "playing") { speech.i++; speakNext(); } };
  u.onend = done;
  u.onerror = done;  // a cancel (pause/stop) fires this too; `current` no longer matches then
  speech.current = u;
  speechSynthesis.speak(u);
}
function speak(text) {
  if (!canSpeak || !text) return;
  speechSynthesis.cancel();
  Object.assign(speech, { text, parts: sentences(text), i: 0, state: "playing", current: null });
  speakNext();
  speechButtons();
}
function pauseSpeech() {
  speech.state = "paused"; speech.current = null;
  speechSynthesis.cancel();
  speechButtons();
}
function resumeSpeech() {
  speech.state = "playing";
  speakNext();
  speechButtons();
}
function stopSpeech() {
  Object.assign(speech, { state: "idle", current: null, i: 0 });
  if (canSpeak) speechSynthesis.cancel();
  speechButtons();
}
function speechButtons() {
  const b = $("#read-now");
  const sameMessage = speech.text === lastText;
  b.textContent = speech.state === "playing" ? "⏸ Pause" : speech.state === "paused" && sameMessage ? "▶ Resume" : "▶ Read";
  b.disabled = speech.state === "idle" && !lastText;
  $("#read-stop").classList.toggle("hidden", speech.state === "idle");
}
if (!canSpeak) {
  $("#read-now").classList.add("hidden");
  $("#speak").disabled = true;
  $(".speak").after(h("div", { class: "muted small" }, "This browser can't read aloud."));
}
$("#speak").addEventListener("change", (e) => {
  if (e.target.checked) {
    lastSpoken = lastText;  // what's already on screen isn't read; ▶ Read does that
    // spoken from the tap itself: iOS only lets a page speak after a user gesture has
    speak("Read aloud is on. New messages from the AI will be read out.");
  } else stopSpeech();
});
$("#read-now").addEventListener("click", () => {
  if (speech.state === "playing") pauseSpeech();
  else if (speech.state === "paused" && speech.text === lastText) resumeSpeech();
  else { lastSpoken = lastText; speak(lastText); }
});
$("#read-stop").addEventListener("click", stopSpeech);

function render(st) {
  $("#case").textContent = st.case ? `${st.case.name} · ${st.case.sensitivity}` : "no case";
  const last = [...st.chat].reverse().find((e) => e.kind === "assistant");
  const text = last ? last.text : (st.busy ? "Thinking…" : "—");
  $("#ai-text").textContent = text;
  lastText = last?.text || "";
  if ($("#speak").checked && lastText && lastText !== lastSpoken) {
    lastSpoken = lastText;
    speak(lastText);
  }
  speechButtons();
  const why = !st.case ? "Start a case on the computer first." : !st.photos?.ok ? st.photos?.why || "The chat model can't use images."
    : st.busy ? "The AI is responding: send when it has finished." : "";
  photosAllowed = !why;
  for (const id of ["#photo-take", "#photo-pick", "#photo-send"]) $(id).disabled = !photosAllowed;
  $("#photo-why").textContent = why;
  $("#photo-why").classList.toggle("hidden", !why);
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
    ["ran", "inserted", "skipped"].includes(i.status) ? h("div", { class: "big" }, h("button", { class: "ghost", onclick: () => mark(i.num, "pending") }, "Undo")) : null))
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
    if (e.code === 4403) { toast("Disconnected: DA Toolkit stopped the companion or issued a new code. Scan the connect code again."); return; }
    setTimeout(reconnect, 2000);
  };
}
// After a drop, only reconnect while the token is still good (a restarted companion has a new one).
async function reconnect() {
  try { render(await api("GET", "/api/companion/state")); connect(); }
  catch (e) {
    if (e instanceof TypeError) setTimeout(reconnect, 3000);  // unreachable for now: keep trying
    else toast("Disconnected: DA Toolkit stopped the companion or issued a new code. Scan the connect code again.");
  }
}
if (!TOKEN) toast("No pairing token. Scan the connect code on the DA Toolkit screen.");
else { api("GET", "/api/companion/state").then(render).catch((e) => toast(e.message)); connect(); }

// ---- photo to AI: take or choose, black out, describe, send
const PHOTO_MAX = 2048;
const photo = { img: null, rects: [], drag: null };
const canvas = $("#photo-canvas"), ctx = canvas.getContext("2d");
$("#photo-take").addEventListener("click", () => $("#photo-cam").click());
$("#photo-pick").addEventListener("click", () => $("#photo-file").click());
for (const id of ["#photo-cam", "#photo-file"]) $(id).addEventListener("change", (e) => { const f = e.target.files[0]; e.target.value = ""; if (f) loadPhoto(f); });

function loadPhoto(file) {
  const fr = new FileReader();
  fr.onload = () => {
    const img = new Image();
    img.onload = () => {
      // fit within PHOTO_MAX on the long side, drawn once into the canvas that is edited and sent
      const scale = Math.min(1, PHOTO_MAX / Math.max(img.naturalWidth, img.naturalHeight));
      canvas.width = Math.round(img.naturalWidth * scale);
      canvas.height = Math.round(img.naturalHeight * scale);
      const base = document.createElement("canvas");
      base.width = canvas.width; base.height = canvas.height;
      const bx = base.getContext("2d"); bx.imageSmoothingQuality = "high";
      bx.drawImage(img, 0, 0, base.width, base.height);
      Object.assign(photo, { img: base, rects: [], drag: null });
      draw();
      $("#photo-edit").classList.remove("hidden");
      $("#photo-edit").scrollIntoView({ behavior: "smooth" });
    };
    img.onerror = () => toast("That file couldn't be read as an image.");
    img.src = fr.result;
  };
  fr.readAsDataURL(file);
}
function draw() {
  if (!photo.img) return;
  ctx.drawImage(photo.img, 0, 0);
  ctx.fillStyle = "#000";  // opaque black painted into the one bitmap that is sent: no layers
  for (const b of photo.rects) ctx.fillRect(b.x, b.y, b.w, b.h);
  const d = photo.drag;
  if (d) {
    ctx.fillRect(d.x, d.y, d.w, d.h);
    ctx.strokeStyle = "#e5534b"; ctx.lineWidth = Math.max(3, canvas.width / 300);
    ctx.strokeRect(d.x, d.y, d.w, d.h);
  }
  $("#photo-count").textContent = photo.rects.length ? `${photo.rects.length} area(s) blacked out` : "Nothing blacked out";
}
function pt(e) {
  const b = canvas.getBoundingClientRect();
  return { x: Math.max(0, Math.min(canvas.width, (e.clientX - b.left) * canvas.width / b.width)),
    y: Math.max(0, Math.min(canvas.height, (e.clientY - b.top) * canvas.height / b.height)) };
}
canvas.addEventListener("pointerdown", (e) => {
  if (!photo.img) return;
  e.preventDefault();
  canvas.setPointerCapture(e.pointerId);
  const p = pt(e);
  photo.drag = { x0: p.x, y0: p.y, x: p.x, y: p.y, w: 0, h: 0 };
});
canvas.addEventListener("pointermove", (e) => {
  const d = photo.drag;
  if (!d) return;
  const p = pt(e);
  Object.assign(d, { x: Math.min(d.x0, p.x), y: Math.min(d.y0, p.y), w: Math.abs(p.x - d.x0), h: Math.abs(p.y - d.y0) });
  draw();
});
function endDrag() {
  const d = photo.drag;
  // whole pixels, rounded outwards: a fractional edge would be blended and keep a trace
  if (d && d.w > 4 && d.h > 4) {
    const x = Math.floor(d.x), y = Math.floor(d.y);
    photo.rects.push({ x, y, w: Math.min(canvas.width, Math.ceil(d.x + d.w)) - x, h: Math.min(canvas.height, Math.ceil(d.y + d.h)) - y });
  }
  photo.drag = null;
  draw();
}
canvas.addEventListener("pointerup", endDrag);
canvas.addEventListener("pointercancel", endDrag);
$("#photo-undo").addEventListener("click", () => { photo.rects.pop(); draw(); });
$("#photo-clear").addEventListener("click", () => { photo.rects = []; draw(); });
function closePhoto() {
  Object.assign(photo, { img: null, rects: [], drag: null });
  $("#photo-text").value = "";
  $("#photo-edit").classList.add("hidden");
}
$("#photo-cancel").addEventListener("click", closePhoto);
$("#photo-send").addEventListener("click", async () => {
  if (!photo.img || !photosAllowed) return;
  const btn = $("#photo-send");
  btn.disabled = true; btn.textContent = "Sending…";
  try {
    photo.drag = null; draw();
    // (the server also re-encodes it from its pixels, dropping any metadata)
    await api("POST", "/api/companion/photo", { message: $("#photo-text").value.trim(), image: canvas.toDataURL("image/jpeg", 0.9) });
    closePhoto();
    toast("Sent to the AI.", "ok");
  } catch (e) { toast(e.message); }
  finally { btn.textContent = "Send to AI"; btn.disabled = !photosAllowed; }
});
