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
  const opened = [];
  app.window.document.addEventListener("click", (ev) => { if (ev.target?.dataset?.confirmed) opened.push(ev.target.getAttribute("href")); });
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
