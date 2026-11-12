"use strict";
// A TEE reply's mark, and what its tooltip says of the receipt (app.js teeBadge; engine.py _tee_found).
// Run with the other frontend tests (tests/test_frontend.py).

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { loadApp } = require("./fakedom.cjs");

if (!process.env.DAT_STATES) throw new Error("Run through pytest (tests/test_frontend.py), or set DAT_STATES to a file of snapshots.");
const STATES = JSON.parse(fs.readFileSync(process.env.DAT_STATES, "utf8"));

async function page() {
  const app = loadApp();
  await app.event({ type: "state", state: structuredClone(STATES.a) });
  return app;
}

test("a TEE reply verified without a receipt doesn't say one was read", async () => {
  const app = await page();
  const badge = (e) => app.call("teeBadge", { tee_attested: "partially verified: x", tee_headline: "TEE gateway attested", ...e });
  const title = (e) => badge(e).getAttribute("title");

  // with every reply's receipt read and held: as it was
  const read = title({ tee_signature: "verified", tee_receipt: true });
  assert.match(read, /the gateway's signed receipt, read too, says that machine was verified/);
  assert.doesNotMatch(read, /No receipt was read|wasn't kept/);

  // verified by the attested key's signature alone: the mark is the same, and the tooltip says what was read
  const bare = badge({ tee_signature: "verified", tee_receipt: false });
  assert.deepEqual([bare.textContent, bare.getAttribute("class")], ["🛡 TEE · ✔ verified", "tee-badge ok"]);
  const unread = bare.getAttribute("title");
  assert.match(unread, /Reply: Checked against the attested signing key: that key signed this reply's record, and the record is for the exact bytes this app received\. No receipt was read/);
  assert.match(unread, /nothing here says the machine running the model was verified/);
  assert.doesNotMatch(unread, /read too|receipt, read/);
  assert.ok(unread.startsWith("TEE gateway attested before sending: partially verified: x."), unread);
  assert.match(unread, /passes NanoGPT's servers in the clear\. A private\/ model is end-to-end encrypted instead\.$/);

  // signed, the text not comparable, and no receipt either
  const signed = badge({ tee_signature: "signed", tee_receipt: false });
  assert.equal(signed.textContent, "🛡 TEE · signed, text not compared");
  assert.match(signed.getAttribute("title"), /but the reply's text can't be compared with it .* No receipt was read/);
  assert.doesNotMatch(title({ tee_signature: "signed", tee_receipt: true }), /No receipt was read|wasn't kept/);

  // a reply kept by an earlier version doesn't say which: its tooltip says that
  const older = title({ tee_signature: "verified" });
  assert.match(older, /the gateway's word, not checked by DA Toolkit/);
  assert.match(older, /Whether a receipt was read for this reply wasn't kept with it/);

  // the other marks say nothing of it
  for (const result of ["failed", "unchecked", "unsigned", "checking"]) {
    assert.doesNotMatch(title({ tee_signature: result, tee_receipt: false }), /No receipt was read|wasn't kept with it/, result);
  }
});
