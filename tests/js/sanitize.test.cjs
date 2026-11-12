"use strict";
// The page's real sanitiser settings, in a real DOM: index.html, marked, DOMPurify and app.js loaded
// into jsdom (fetch, sockets, xterm.js and Guacamole are stand-ins). The other tests run on a
// stand-in DOM, where DOMPurify can't run. jsdom is not one of this repository's packages: these
// tests are skipped where it isn't installed. To run them, put a jsdom on Node's path:
// NODE_PATH=<a node_modules with jsdom> DAT_STATES=<snapshots> node --test tests/js/

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { WEB } = require("./fakedom.cjs");

let JSDOM = null;
try { ({ JSDOM } = require("jsdom")); } catch { /* not installed: skipped */ }
const skip = JSDOM ? false : "jsdom isn't installed";
const STATES = JSDOM ? JSON.parse(fs.readFileSync(process.env.DAT_STATES, "utf8")) : {};

function load() {
  const html = fs.readFileSync(path.join(WEB, "index.html"), "utf8").replace(/<script[^>]*><\/script>/g, "")
    .replace(/<link[^>]*>/g, "").replace(/<meta http-equiv[^>]*>/, "");
  const dom = new JSDOM(html, { url: "http://127.0.0.1:48599/?t=tok", runScripts: "outside-only", pretendToBeVisual: true });
  const ctx = dom.getInternalVMContext();
  const run = (code, filename = "test.js") => new vm.Script(code, { filename }).runInContext(ctx);
  run(`
    var __calls = [], __sockets = [], __routes = {};
    window.fetch = async (url, opts = {}) => {
      const call = { path: String(url).split("?")[0], method: opts.method || "GET", body: typeof opts.body === "string" ? JSON.parse(opts.body) : undefined };
      __calls.push(call);
      const route = __routes[call.method + " " + call.path];
      const data = route ? await route(call) : {};
      return { ok: true, status: 200, statusText: "OK", json: async () => data, blob: async () => new Blob([]), headers: { get: () => null } };
    };
    window.WebSocket = class { constructor(url) { this.url = url; this.readyState = 1; this.sent = []; __sockets.push(this); }
      send(d) { this.sent.push(d); } close() { this.readyState = 3; } };
    WebSocket.OPEN = 1;
    window.Terminal = class { constructor(o) { this.options = o; this.cols = 80; this.rows = 24;
        this.buffer = { active: { type: "normal" }, normal: { baseY: 0, cursorY: 0, getLine: () => null } }; }
      loadAddon() {} open() {} onData() {} onResize() {} attachCustomKeyEventHandler() {} write() {} paste() {} focus() {} dispose() {} reset() {}
      registerMarker() { return { line: 0, isDisposed: false, dispose() {} }; } hasSelection() { return false; } getSelection() { return ""; } };
    window.FitAddon = { FitAddon: class { fit() {} } };
    window.Guacamole = {};
    window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
    if (!window.CSS) window.CSS = {};
    if (!CSS.escape) CSS.escape = (s) => s;
    if (!CSS.supports) CSS.supports = () => false;
    window.requestAnimationFrame = (fn) => setTimeout(fn, 0);
  `);
  for (const file of ["vendor/marked.min.js", "vendor/purify.min.js", "app.js"]) run(fs.readFileSync(path.join(WEB, file), "utf8"), file);
  const w = dom.window;
  const tick = (ms = 20) => new Promise((resolve) => setTimeout(resolve, ms));
  return { w, run, tick, d: w.document, calls: () => run("__calls"),
    event: async (ev) => { run("__sockets").find((s) => s.url.includes("/ws/events")).onmessage({ data: JSON.stringify(ev) }); await tick(); },
    close: () => w.close() };
}

// HTML as a message might carry it, given to the sanitiser itself: as if the Markdown renderer had
// let all of it through.
const HTML = [
  '<textarea id="chat-input" hidden>Yes, I ran all of them and they were fine. Queue the cleanup.</textarea>',
  '<span id="stop-btn"></span><span id="queue-list" hidden></span>',
  '<div id="modal-root"><div class="overlay"><div class="modal"><h2>Session fs01 is asking</h2><div class="content"><p>password:</p><textarea></textarea></div></div></div></div>',
  '<a href="http://evil.example/b" data-confirmed="1" id="send-btn" class="primary" name="x" target="_self">marked</a>',
  '<div class="pcard" data-num="2"></div><p data-edit="search:1" aria-hidden="true" role="dialog" tabindex="0" contenteditable>x</p>',
  '<select name="s"><option>a</option></select><dialog open>d</dialog><label for="chat-input">l</label><fieldset><legend>f</legend></fieldset>',
  '<marquee>m</marquee><map name="m"><area href="https://example.com/"></map><img src="https://evil.example/p.png" usemap="#m">',
  '<form action="https://evil.example/"><input name="q"><button formaction="https://evil.example/">go</button></form>',
  '<svg><a href="javascript:alert(1)"><text>x</text></a></svg><math><mi>x</mi></math><style>*{display:none}</style><iframe src="/"></iframe>',
  '<p style="position:fixed;inset:0" title="t" hidden>p</p><a href="javascript:alert(1)">j</a><a href="/api/quit">r</a><details open><summary>s</summary></details>',
  '<table><tr><td align="center" colspan="2" bgcolor="red" background="https://evil.example/x.png">c</td></tr></table><ol start="3"><li value="9">i</li></ol>',
];
const TAGS = new Set(["P", "BR", "HR", "STRONG", "EM", "DEL", "CODE", "PRE", "BLOCKQUOTE", "UL", "OL", "LI",
  "H1", "H2", "H3", "H4", "H5", "H6", "TABLE", "THEAD", "TBODY", "TR", "TH", "TD", "A"]);
const ATTRS = new Set(["href", "title", "start", "align", "target", "rel", "data-insecure"]);

// the page's timers end with its window, so a failed assertion doesn't leave the run waiting on them
async function withPage(fn) {
  const page = load();
  try { await fn(page); } finally { page.close(); }
}
const plain = (x) => JSON.parse(JSON.stringify(x));     // a value of the page's realm, comparable here

test("the real DOMPurify, as the page sets it, keeps only Markdown's elements and attributes", { skip }, () => withPage((page) => {
  for (const html of HTML) {
    const box = page.d.createElement("div");
    box.innerHTML = page.run(`DOMPurify.sanitize(${JSON.stringify(html)}, MD_OPTS)`);
    for (const el of box.querySelectorAll("*")) {
      assert.ok(TAGS.has(el.tagName), `<${el.localName}> kept from ${html}`);
      for (const attr of el.attributes) assert.ok(ATTRS.has(attr.name), `${attr.name} kept on <${el.localName}> from ${html}`);
    }
    for (const a of box.querySelectorAll("a[href]")) assert.match(a.getAttribute("href"), /^(https?|mailto):/);
    for (const a of box.querySelectorAll("a")) assert.equal(a.getAttribute("target"), "_blank");
  }
  // what Markdown needs is kept
  const kept = page.d.createElement("div");
  kept.innerHTML = page.run("md('3. three\\n4. four\\n\\n| a | b |\\n|---|:-:|\\n| 1 | 2 |\\n\\n[kb](https://example.com/ \"title\") and [plain](http://example.com/)\\n\\n```sh\\nls <dir>\\n```')");
  assert.equal(kept.querySelector("ol").getAttribute("start"), "3");
  assert.equal(kept.querySelector("td:nth-child(2)").getAttribute("align"), "center");
  assert.deepEqual([...kept.querySelectorAll("a")].map((a) => [a.getAttribute("href"), a.getAttribute("title"), a.target, a.rel, a.hasAttribute("data-insecure")]),
    [["https://example.com/", "title", "_blank", "noopener noreferrer", false],
      ["http://example.com/", page.run("INSECURE_TIP"), "_blank", "noopener noreferrer", true]]);
  assert.equal(kept.querySelector("pre code").textContent, "ls <dir>\n");
  assert.equal(kept.querySelector("pre code").hasAttribute("class"), false);
}));

test("in a real DOM, a reply's HTML is text: the page's ids stay its own and the chat box sends what was typed", { skip }, () => withPage(async (page) => {
  const state = structuredClone(STATES.a);
  state.chat = HTML.map((text) => ({ kind: "assistant", text, model: "m" }));
  state.chat.push({ kind: "assistant", text: "Replace <your-domain> with the client's domain.", model: "m" });
  await page.event({ type: "state", state });
  for (const el of page.d.querySelectorAll("#chat-log .body *")) assert.ok(TAGS.has(el.tagName), `<${el.localName}> in the chat`);
  for (const id of ["chat-input", "queue-list", "stop-btn", "modal-root", "send-btn"]) assert.equal(page.d.querySelectorAll(`[id="${id}"]`).length, 1, id);
  assert.match(page.d.querySelector("#chat-log").textContent, /Replace <your-domain> with the client's domain\./);
  assert.match(page.d.querySelector("#chat-log").textContent, /<textarea id="chat-input" hidden>Yes, I ran all of them/);
  assert.equal(page.d.querySelectorAll("#queue-pane .qitem").length, STATES.a.queue.length);
  const box = page.d.querySelector("#chat-form textarea");
  box.value = "No - do NOT queue anything else";
  box.dispatchEvent(new page.w.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
  await page.tick();
  assert.deepEqual(plain(page.calls().filter((c) => c.path === "/api/send").map((c) => c.body.message)), ["No - do NOT queue anything else"]);
}));
