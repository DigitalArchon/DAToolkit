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

test("every element app.js looks up by id is on the page or made by app.js", () => {
  const html = fs.readFileSync(path.join(WEB, "index.html"), "utf8");
  const js = fs.readFileSync(path.join(WEB, "app.js"), "utf8");
  const have = new Set([...html.matchAll(/\bid="([^"]+)"/g), ...js.matchAll(/\bid: "([\w-]+)"/g),
    ...js.matchAll(/\.id = "([\w-]+)"/g)].map((m) => m[1]));
  const wanted = new Set([...js.matchAll(/(?:\$|querySelector(?:All)?)\(["'`]#([\w-]+)/g),
    ...js.matchAll(/getElementById\(["'`]([\w-]+)/g)].map((m) => m[1]));
  assert.deepEqual([...wanted].filter((id) => !have.has(id)), []);
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

function fakeRdp(app, sid = "rdp1") {
  const log = [];
  app.run(`S.rdps[${JSON.stringify(sid)}] = { id: ${JSON.stringify(sid)}, state: 3, host: h("div"), view: h("div", { class: "rdp-view", tabindex: 0 }) };`);
  const r = app.run(`S.rdps[${JSON.stringify(sid)}]`);
  r.client = { sendKeyEvent: (down, key) => log.push([down, key]) };
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

// ------------------------------------------------------------------ the top bar

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
