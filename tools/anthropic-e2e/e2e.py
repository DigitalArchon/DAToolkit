"""End-to-end check of DA Toolkit's Anthropic path against the live API, through the Engine.

    .venv/bin/python -I tools/anthropic-e2e/e2e.py [core native haiku-search gated vision models claude-<id> ...]

Throwaway config/data dirs (next to this file); the keyring's "Anthropic" key, and "NanoGPT"'s
for the gated search. Strict mode: prefix_mismatch_behavior "error", so any edit to an
append-only history fails the request instead of being dropped quietly. Every scenario is
real spend: about US$1.70 for all of them (Oct 2026), printed at the end."""

import asyncio
import base64
import io
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ["XDG_CONFIG_HOME"] = str(HERE / "xdg" / "config")
os.environ["XDG_DATA_HOME"] = str(HERE / "xdg" / "data")

import anthropic  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from datoolkit import creds  # noqa: E402
from datoolkit.config import Config, Provider  # noqa: E402
from datoolkit.engine import Engine  # noqa: E402
from datoolkit.llm import anthropic_api  # noqa: E402

anthropic_api.PREFIX_MISMATCH = "error"
ONLY = set(sys.argv[1:])

# $ per million tokens: input, output, cache read; a 1-hour cache write is 2x input
PRICES = {"claude-fable-5-1": (10, 50, 0.25), "claude-opus-5-5": (4, 20, 0.20), "claude-opus-5": (5, 25, 0.5),
          "claude-opus-4-8": (5, 25, 0.5), "claude-opus-4-6": (5, 25, 0.5), "claude-opus-4-5": (5, 25, 0.5),
          "claude-sonnet-5-5": (2, 10, 0.2), "claude-sonnet-5": (2, 10, 0.2), "claude-sonnet-4-6": (3, 15, 0.3),
          "claude-haiku-5-5": (0.1, 0.5, 0.01), "claude-haiku-4-5": (1, 5, 0.1)}

wire: list[dict] = []          # every Messages API request: model, status, betas, body
results: list[tuple[str, bool, str]] = []


async def on_request(req):
    if req.url.path.endswith("/v1/messages"):
        wire.append({"body": json.loads(req.content), "betas": req.headers.get("anthropic-beta", ""), "status": None})


async def on_response(resp):
    if resp.request.url.path.endswith("/v1/messages") and wire:
        wire[-1]["status"] = resp.status_code
        if resp.status_code >= 400:
            await resp.aread()
            wire[-1]["error"] = resp.text[:600]


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(': ' + detail) if detail else ''}", flush=True)
    return bool(ok)


def make_engine() -> Engine:
    provs = [Provider("Anthropic", "https://api.anthropic.com"), Provider("NanoGPT", "https://nano-gpt.com/api/v1")]
    cfg = Config(providers=provs)
    cfg.settings.search_mode = "auto"
    cfg.settings.generation = {"temperature": 0.3, "reasoning_effort": "medium"}     # the default since Oct 2026
    eng = Engine(cfg, lambda e: None, HERE / "rt", save_config=lambda c: None)
    eng._anthropic_http = anthropic.DefaultAsyncHttpxClient(event_hooks={"request": [on_request], "response": [on_response]})
    return eng


async def wait_turn(eng, limit=400):
    for _ in range(limit * 10):
        if not eng.busy:
            return
        await asyncio.sleep(0.1)
    raise TimeoutError("turn did not finish")


def last_turn(eng):
    """(the newest assistant entry, error notes after the last user message)."""
    i = max(i for i, e in enumerate(eng.chat) if e["kind"] == "user")
    after = eng.chat[i + 1:]
    entry = next((e for e in reversed(after) if e["kind"] == "assistant"), None)
    errors = [e["text"] for e in after if e["kind"] == "note" and e["text"].startswith("AI request")]
    return entry, errors


async def turn(eng, label, **send):
    n = len(wire)
    t = time.monotonic()
    eng.send(**send)
    await wait_turn(eng)
    entry, errors = last_turn(eng)
    sent = wire[n:]
    bad = [f"HTTP {w['status']}: {w.get('error', '')}" for w in sent if w["status"] and w["status"] >= 400]
    check(f"{label}: answered without errors", entry and not errors and not bad,
          "; ".join(errors + bad)[:500] or f"{time.monotonic() - t:.0f}s, {len(sent)} request(s)")
    return entry, sent


def ran(eng, nums, outputs):
    results_ = []
    for n, out in zip(nums, outputs):
        eng.queue.update(n, status="ran")
        results_.append({"num": n, "text": out})
    return results_


FAKE_OUT = ["Filesystem      Size  Used Avail Use% Mounted on\n/dev/sda2        98G   93G  1.2G  99% /\ntmpfs  3.9G 0 3.9G 0% /dev/shm",
            "41G\t/var/log\n22G\t/var/lib/docker\n9.1G\t/home\n3.0G\t/usr",
            "38G\t/var/log/journal\n2.1G\t/var/log/syslog.1"]


def thinking_blocks(body) -> int:
    return sum(1 for m in body["messages"] if m["role"] == "assistant" for b in m["content"]
               if isinstance(b, dict) and b.get("type") == "thinking" and b.get("signature"))


async def scenario_core(model="claude-opus-5-5", full=True):
    print(f"\n== core tool loop: {model}", flush=True)
    eng = make_engine()
    await eng.start()
    try:
        await eng.list_models("Anthropic")
        eng.new_case(f"e2e {model}", "open")
        eng.save_settings({"search_mode": "off"})
        eng.select_model("Anthropic", model)
        eng.open_session("local")
        entry, sent = await turn(eng, "turn 1", message=(
            "This machine (the local session) has its root filesystem nearly full and services are failing to write. "
            "Find out what is using the space: queue two or three read-only commands now, keep your message short."))
        if not entry:
            return
        body = sent[0]["body"]
        mode = anthropic_api.state_mode(model, True)
        check("turn 1: proposed commands", entry.get("proposals"), f"{len(entry.get('proposals', []))} queued")
        print(f"    info: turn 1 reasoning {len(entry.get('reasoning', ''))} chars (adaptive: may be none at low effort)")
        check(f"turn 1: state sent as {mode}",
              (body["messages"][-1]["role"] == "system" and body["messages"][-1].get("clear_at")) if mode == "clear_at"
              else body["messages"][-1]["role"] == "user")
        stored = next(m for m in eng.conv if m["role"] == "assistant")
        check("turn 1: Anthropic's blocks kept on the turn", stored.get("native", {}).get("content"))
        nums = entry["proposals"][:3]
        if not nums:
            return
        entry, sent = await turn(eng, "turn 2 (results)", message="",
                                 results=ran(eng, nums, FAKE_OUT[:len(nums)]))
        if not entry:
            return
        tb = thinking_blocks(sent[0]["body"])
        print(f"    info: turn 2 sent back {tb} thinking block(s) with signatures")
        u = eng.last_usage or {}
        check("turn 2: read from the prompt cache", u.get("cache_read_input_tokens", 0) > 0,
              f"cache read {u.get('cache_read_input_tokens')} of {u.get('prompt_tokens')} prompt tokens")
        check("turn 2: says what fills the disk", "journal" in entry["text"].lower() or "log" in entry["text"].lower(),
              entry["text"][:160].replace("\n", " "))
        if not full:
            return eng
        # an edited case note changes the system prompt: the earlier thinking must be left out, not refused
        eng.edit_case(eng.case.id, eng.case.name, notes="Production web server for the Perth office.")
        entry, sent = await turn(eng, "turn 3 after editing the case notes", message="Thanks. Which of those is safe to clean now?")
        if entry:
            check("turn 3: stale thinking left out", thinking_blocks(sent[0]["body"]) == 0,
                  f"{thinking_blocks(sent[0]['body'])} block(s) sent")
        entry, sent = await turn(eng, "turn 4 (thinking from turn 3 goes back)", message="One line: how do I cap the journal at 2G?")
        if entry:
            print(f"    info: turn 4 sent back {thinking_blocks(sent[0]['body'])} thinking block(s), accepted in strict mode")
        # second opinion and write-ups use the same client, outside the chat history
        pending = [p for p in eng.queue.items if p.status == "pending"]
        if pending:
            r = await eng.review_command(pending[0].num)
            check("second opinion", r and (r.get("verdict") or r.get("text") or r.get("review")), str(r)[:160])
        doc = await eng.ticket_summary()
        check("ticket summary", doc and len(json.dumps(doc)) > 200, str(doc)[:120].replace("\n", " "))
        # compaction keeps the last exchange verbatim: its thinking must be left out, not refused
        prev = await eng.compact_preview(1)
        check("compaction summary written", prev.get("summary"), f"{prev.get('summary_tokens')} tokens")
        eng.compact_apply(prev["summary"])
        entry, sent = await turn(eng, "turn 5 after compaction", message="Remind me in one line what we found.")
        view = eng.context_view()
        check("context view", view["total_tokens"] > 0 and view.get("limit"), f"{view['total_tokens']} tokens est., limit {view.get('limit')}")
        return eng
    finally:
        await eng.stop()


async def scenario_native_search(model="claude-opus-5-5"):
    print(f"\n== Anthropic's own search: {model}", flush=True)
    eng = make_engine()
    await eng.start()
    try:
        await eng.list_models("Anthropic")
        eng.new_case("e2e search", "open")
        eng.save_settings({"search_engine": "native", "search_mode": "ask"})
        eng.select_model("Anthropic", model)
        st = eng.search_status()
        check("native search active for an Open case", st.get("native") and st.get("research"), json.dumps(st))
        entry, sent = await turn(eng, "search turn", message=(
            "What is the newest stable OPNsense release and when did it come out? Search the web; answer in two "
            "sentences and don't propose commands."))
        if not entry:
            return
        tools = sent[0]["body"]["tools"]
        check("server web search tool sent", any(t.get("type", "").startswith("web_search_") for t in tools),
              ", ".join(t.get("type", t.get("name")) for t in tools))
        done = [s for s in entry.get("searches", []) if s["status"] == "done"]
        check("searches ran and returned results", done and all(s["results"] for s in done),
              "; ".join(f"{s['query']!r}: {len(s['results'])}" for s in entry.get("searches", [])))
        dynamic = anthropic_api._model_is(model, *anthropic_api.DYNAMIC_SEARCH_MODELS)
        check("sources cited" + (" (dynamic filtering: Anthropic sends none, results shown)" if dynamic else ""),
              entry.get("citations") or (dynamic and done), ", ".join(c["url"] for c in entry.get("citations", [])[:3]))
        logged = [json.loads(x) for f in (HERE / "xdg" / "data").rglob("requests.jsonl") if eng.case.id in str(f)
                  for x in f.read_text().splitlines()]
        n = sum(((r.get("response") or {}).get("usage") or {}).get("web_search_requests", 0) for r in logged)
        check("usage counts the searches", n > 0, f"{n} search request(s) billed")
        check("no promise nudge after the search", len(sent) == 1, f"{len(sent)} request(s)")
        print("    answer:", entry["text"][:300].replace("\n", " "))
        entry, sent = await turn(eng, "follow-up replays the search blocks", message="And the one before it?")
        eng.edit_case(eng.case.id, eng.case.name, sensitivity="confidential")
        st = eng.search_status()
        check("Confidential case: Anthropic's search off", not st.get("native"), json.dumps(st))
    finally:
        await eng.stop()


async def scenario_gated_search(model="claude-opus-5-5"):
    print(f"\n== gated NanoGPT search with {model}", flush=True)
    eng = make_engine()
    await eng.start()
    try:
        await eng.list_models("Anthropic")
        eng.new_case("e2e gated", "open")
        eng.save_settings({"search_engine": "nanogpt", "search_mode": "auto"})
        eng.select_model("Anthropic", model)
        entry, sent = await turn(eng, "gated search turn", message=(
            "Use web_search once to find the latest stable Proxmox VE version, then answer in one sentence. No commands."))
        if not entry:
            return
        names = [t.get("name") for t in sent[0]["body"]["tools"]]
        check("function web_search offered, not the server tool", "web_search" in names and
              not any(t.get("type", "").startswith("web_search_") for t in sent[0]["body"]["tools"]))
        s = entry.get("searches", [])
        check("search ran through NanoGPT", s and s[0]["status"] == "done", "; ".join(f"{x['provider']} {x['status']} {x.get('error', '')}" for x in s))
        check("tool loop continued after the search", len(sent) >= 2, f"{len(sent)} requests")
    finally:
        await eng.stop()


async def scenario_vision(model="claude-opus-5-5"):
    print(f"\n== image: {model}", flush=True)
    img = Image.new("RGB", (640, 200), "white")
    d = ImageDraw.Draw(img)
    d.text((20, 40), "Windows Update failed", fill="black", font_size=32)
    d.text((20, 110), "Error 0x80070005", fill="red", font_size=40)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    eng = make_engine()
    await eng.start()
    try:
        await eng.list_models("Anthropic")
        eng.new_case("e2e vision", "open")
        eng.save_settings({"search_mode": "off"})
        eng.select_model("Anthropic", model)
        check("vision detected from Anthropic's list", eng.vision_status()["mode"] == "native")
        cleaned = Engine.clean_images([url])
        entry, sent = await turn(eng, "image turn", message="What error code is on this screenshot? One line, no commands.",
                                 images=cleaned)
        if entry:
            check("reads the code from the image", "80070005" in entry["text"], entry["text"][:120])
    finally:
        await eng.stop()


MODELS = ["claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1", "claude-opus-5", "claude-opus-4-8",
          "claude-sonnet-5", "claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001", "claude-opus-4-5-20251101"]


def cost() -> float:
    total = 0.0
    for f in (HERE / "xdg" / "data").rglob("requests.jsonl"):
        for line in f.read_text().splitlines():
            r = json.loads(line)
            u = (r.get("response") or {}).get("usage") or {}
            key = next((k for k in PRICES if r["model"].startswith(k)), None)
            if not key or not u:
                continue
            i, o, cr = PRICES[key]
            read, write = u.get("cache_read_input_tokens", 0), u.get("cache_creation_input_tokens", 0)
            fresh = u.get("prompt_tokens", 0) - read - write
            total += (fresh * i + write * i * 2 + read * cr + u.get("completion_tokens", 0) * o) / 1e6
            total += u.get("web_search_requests", 0) * 0.01
    return total


async def main():
    assert creds.get_secret("provider", "Anthropic"), "no Anthropic key in the keyring"
    if not ONLY or "core" in ONLY:
        await scenario_core()
    if not ONLY or "native" in ONLY:
        await scenario_native_search()
    if not ONLY or "native" in ONLY or "haiku-search" in ONLY:
        await scenario_native_search("claude-haiku-5-5")
    if not ONLY or "gated" in ONLY:
        await scenario_gated_search()
    if not ONLY or "vision" in ONLY:
        await scenario_vision()
    if not ONLY or "models" in ONLY:
        for m in MODELS:
            await scenario_core(m, full=False)
    for m in [a for a in ONLY if a.startswith("claude-")]:
        await scenario_core(m, full=False)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed; {len(wire)} Messages API requests; "
          f"about ${cost():.2f} (prices approximate)")
    for name, _, detail in failed:
        print("  FAILED:", name, "-", detail)


if __name__ == "__main__":
    asyncio.run(main())
