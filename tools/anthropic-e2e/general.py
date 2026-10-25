"""General end-to-end check of DA Toolkit's features on NanoGPT and Anthropic, through the Engine.

    .venv/bin/python -I tools/anthropic-e2e/general.py [scenario ...]

Scenarios: nano-claude route e2ee tee search-ask research vision switch stop review training.
Reasoning effort medium throughout. Real spend on both accounts (printed at the end, roughly);
the Anthropic side runs in strict mode (e2e.py)."""

import asyncio
import base64
import io
import json
import sys
import time

from pathlib import Path

import openai

sys.path.insert(0, str(Path(__file__).resolve().parent))      # e2e.py beside it (-I leaves it off the path)
import e2e  # noqa: E402
from e2e import HERE, FAKE_OUT, check, last_turn, make_engine, ran, thinking_blocks, wait_turn
from datoolkit.engine import Engine
from datoolkit.llm.client import LLMClient
from datoolkit import creds

ONLY = set(sys.argv[1:])
nano_wire: list[dict] = []
GLM = "z-ai/glm-5.3"


async def _nano_request(req):
    if req.url.path.endswith("/chat/completions"):
        nano_wire.append({"body": json.loads(req.content)})


def engine(**settings) -> Engine:
    eng = make_engine()
    eng.cfg.settings.generation = {"temperature": 0.3, "reasoning_effort": "medium"}
    for k, v in settings.items():
        setattr(eng.cfg.settings, k, v)
    real = eng._client

    def wired(prov, model=""):
        c = real(prov, model)
        if type(c) is LLMClient:     # NanoGPT's plain requests, seen on the wire (not Private Mode's, sealed)
            c = LLMClient(prov.base_url, creds.get_secret("provider", prov.name),
                          http_client=openai.DefaultAsyncHttpxClient(event_hooks={"request": [_nano_request]}))
        return c
    eng._client = wired
    return eng


async def turn(eng, label, timeout=400, **send):
    n, a = len(nano_wire), len(e2e.wire)
    t = time.monotonic()
    eng.send(**send)
    await wait_turn(eng, timeout)
    entry, errors = last_turn(eng)
    bad = [f"HTTP {w['status']}: {w.get('error', '')}" for w in e2e.wire[a:] if w["status"] and w["status"] >= 400]
    check(f"{label}: answered", entry and not errors and not bad,
          "; ".join(errors + bad)[:500] or f"{time.monotonic() - t:.0f}s")
    return entry, nano_wire[n:], e2e.wire[a:]


async def start(eng, prov, model, sensitivity="open", name="general"):
    await eng.list_models(prov)
    eng.new_case(name, sensitivity)
    eng.select_model(prov, model)
    eng.open_session("local")


async def attested(eng, what, limit=120):
    for _ in range(limit * 2):
        st = (eng.attestation or {}).get("status")
        if st and st != "checking":
            break
        await asyncio.sleep(0.5)
    att = eng.attestation or {}
    check(f"{what}: enclave attested", att.get("status") == "verified", att.get("summary") or att.get("error") or str(att))
    return att


DISK = ("This machine (the local session) has its root filesystem nearly full. Queue two read-only checks to "
        "find what is using the space; keep your message short.")


async def s_nano_claude():
    print("\n== Claude Opus 5.5 through NanoGPT: tool loop, NanoGPT's prompt caching, effort medium", flush=True)
    eng = engine(search_mode="off")
    await eng.start()
    try:
        await start(eng, "NanoGPT", "anthropic/claude-opus-5.5")
        entry, sent, _ = await turn(eng, "turn 1", message=DISK)
        if not entry or not entry.get("proposals"):
            return check("turn 1: proposed commands", False)
        body = sent[0]["body"]
        check("effort medium sent", body.get("reasoning_effort") == "medium", str(body.get("reasoning_effort")))
        check("NanoGPT prompt caching asked for", (body.get("prompt_caching") or {}).get("ttl") == "1h")
        nums = entry["proposals"][:2]
        entry, sent, _ = await turn(eng, "turn 2 (results)", message="", results=ran(eng, nums, FAKE_OUT[:len(nums)]))
        u = eng.last_usage or {}
        cached = u.get("cache_read_input_tokens") or (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        check("turn 2 read from NanoGPT's cache", cached > 0, f"{cached} of {u.get('prompt_tokens')} prompt tokens")
    finally:
        await eng.stop()


async def s_route():
    print(f"\n== routes on NanoGPT: {GLM}", flush=True)
    eng = engine(search_mode="off")
    await eng.start()
    try:
        await start(eng, "NanoGPT", GLM)
        eng.set_route("NanoGPT", GLM, {"priority": "latency", "fp8": True})
        entry, sent, _ = await turn(eng, "fastest first word", message="In one sentence: what does `df -i` show? No commands.")
        if entry:
            check("route sent", sent and sent[0]["body"].get("provider") == {"sort": "latency", "min_quantization": "fp8"},
                  json.dumps(sent[0]["body"].get("provider")) if sent else "")
            r = entry.get("route") or {}
            check("bill checked against the route", r.get("met") is not None, f"{r.get('label')}: {r.get('why')}")
        hosts = await eng.model_hosts("NanoGPT", GLM)
        fast = sorted([h for h in hosts["hosts"] if h["available"] and h["fp8_or_better"] and h["first_token_ms"]],
                      key=lambda h: h["first_token_ms"])
        check("NanoGPT lists hosts for it", hosts["supported"] and fast, f"{len(hosts['hosts'])} hosts")
        if fast:
            h = fast[0]
            eng.set_route("NanoGPT", GLM, {"priority": "host", "host": h["id"], "host_name": h["name"], "fp8": True,
                                            "price": {"input": h["input_price"], "output": h["output_price"],
                                                      "cache_read": h["cache_read_price"]}})
            entry, sent, _ = await turn(eng, f"host {h['name']}", message="And `df -h`? One sentence, no commands.")
            if entry:
                check("host preference sent", sent[0]["body"].get("provider", {}).get("order") == [h["id"]])
                r = entry.get("route") or {}
                print(f"    info: {r.get('label')}: met={r.get('met')} — {r.get('why')}")
        eng.set_route("NanoGPT", GLM, None)
    finally:
        await eng.stop()


async def s_e2ee():
    print("\n== end-to-end encrypted: private/glm-5-3 in a Confidential case", flush=True)
    eng = engine(search_mode="off")
    await eng.start()
    try:
        await start(eng, "NanoGPT", "private/glm-5-3", "confidential")
        await attested(eng, "private/glm-5-3")
        entry, _, _ = await turn(eng, "sealed turn", message=DISK)
        if entry:
            check("reply was sealed", entry.get("sealed"), entry.get("sealed", "")[:120])
            check("proposed commands", entry.get("proposals"))
        st = eng.search_status()
        check("Confidential: searches only with Linkup, asked", st["mode"] in ("ask", "off") and st.get("provider") in (None, "linkup"),
              json.dumps(st))
    finally:
        await eng.stop()


async def s_tee():
    print("\n== TEE: TEE/glm-5.3", flush=True)
    eng = engine(search_mode="off")
    await eng.start()
    try:
        await start(eng, "NanoGPT", "TEE/glm-5.3")
        att = await attested(eng, "TEE/glm-5.3")
        if att.get("status") != "verified":
            return
        entry, _, _ = await turn(eng, "TEE turn", message="One sentence: what is an inode? No commands.")
        for _ in range(60):
            if entry and entry.get("tee_signature") not in (None, "checking"):
                break
            await asyncio.sleep(1)
        check("reply signature checked", entry and entry.get("tee_signature") in ("signed", "unsigned", "unchecked"),
              str(entry and entry.get("tee_signature")))
    finally:
        await eng.stop()


async def s_search_ask():
    print("\n== gated search, ask mode: approve an edited query, then skip one (Opus 5.5 on Anthropic)", flush=True)
    eng = engine(search_mode="ask", search_engine="nanogpt")
    await eng.start()
    try:
        await start(eng, "Anthropic", "claude-opus-5-5")
        seen = []

        async def approver():
            while True:
                for sid, (fut, rec) in list(eng._search_reqs.items()):
                    if not fut.done():
                        seen.append(rec["query"])
                        if len(seen) == 1:
                            eng.answer_search(sid, True, "current Proxmox VE version release notes")
                        else:
                            eng.answer_search(sid, False)
                await asyncio.sleep(0.3)
        task = asyncio.create_task(approver())
        entry, _, _ = await turn(eng, "search turn", message=(
            "Use web_search to find the newest stable Proxmox VE version, answer in one sentence, no commands."))
        if entry:
            s = entry.get("searches") or []
            check("the AI asked first, and ran the edited query", seen and s and s[0]["status"] == "done" and s[0].get("edited_by_technician"),
                  f"asked {seen}; " + "; ".join(f"{x['query']} {x['status']}" for x in s))
        entry, _, _ = await turn(eng, "declined search turn", message=(
            "Now use web_search to check the newest Proxmox Backup Server version. If I skip the search, say so in one line."))
        if entry:
            s = entry.get("searches") or []
            check("a skipped search is reported to the AI", any(x["status"] == "declined" for x in s),
                  "; ".join(f"{x['query']} {x['status']}" for x in s))
        task.cancel()
    finally:
        await eng.stop()


async def s_research():
    for label, research_model in (("default research agent (Sonnet 5.5 on NanoGPT)", ""),
                                  ("research agent on Anthropic (Sonnet 5.5)", "Anthropic|claude-sonnet-5-5")):
        print(f"\n== research: {label}; chat Opus 5.5 on Anthropic", flush=True)
        eng = engine(search_mode="auto", search_engine="nanogpt", research_model=research_model)
        await eng.start()
        try:
            await start(eng, "Anthropic", "claude-opus-5-5")
            entry, _, _ = await turn(eng, "research turn", timeout=700, message=(
                "Use the research tool (not web_search) to find, in the official OPNsense documentation, the exact menu "
                "path to enable the SSH service on OPNsense 26.7. Then answer in two lines, no commands."))
            if entry:
                r = (entry.get("research") or [{}])[0]
                check("research ran to a report with sources", r.get("status") in ("done", "cached") and r.get("sources"),
                      f"{r.get('status')} by {r.get('model')}: {r.get('searches')} search(es), {r.get('pages')} page(s), "
                      f"${r.get('cost')}; {r.get('error', '')}")
                print("    answer:", entry["text"][:240].replace("\n", " "))
        finally:
            await eng.stop()


def _image() -> str:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (640, 200), "white")
    d = ImageDraw.Draw(img)
    d.text((20, 40), "Windows Update failed", fill="black", font_size=32)
    d.text((20, 110), "Error 0x800F0922", fill="red", font_size=40)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


async def s_vision():
    print(f"\n== vision helper: text-only {GLM} with Haiku 5.5 on Anthropic describing", flush=True)
    eng = engine(search_mode="off", vision_model="Anthropic|claude-haiku-5-5")
    await eng.start()
    try:
        await start(eng, "NanoGPT", GLM)
        v = eng.vision_status()
        check("helper mode for a text-only model", v["mode"] == "helper", json.dumps(v))
        entry, _, _ = await turn(eng, "image turn", message="What error code is on this screenshot? One line, no commands.",
                                 images=Engine.clean_images([_image()]))
        user = next(e for e in reversed(eng.chat) if e["kind"] == "user")
        desc = next(iter((user.get("image_notes") or {}).values()), {})
        check("helper described the image", "800F0922" in desc.get("text", "").upper(), desc.get("text", "")[:120])
        check("chat model answered from the description", entry and "800F0922" in entry["text"].upper(), entry and entry["text"][:120])
    finally:
        await eng.stop()


async def s_switch():
    print("\n== one case, three models: Opus 5.5 (Anthropic) → GLM 5.3 (NanoGPT) → Opus 5.5 again (strict)", flush=True)
    eng = engine(search_mode="off")
    await eng.start()
    try:
        await start(eng, "Anthropic", "claude-opus-5-5")
        entry, _, _ = await turn(eng, "Opus turn", message=DISK)
        nums = (entry or {}).get("proposals", [])[:2]
        await eng.list_models("NanoGPT")
        eng.select_model("NanoGPT", GLM)
        entry, sent, _ = await turn(eng, "GLM turn (results)", message="", results=ran(eng, nums, FAKE_OUT[:len(nums)]))
        if sent:
            leaked = [k for m in sent[0]["body"]["messages"] for k in m if k in ("native", "state_before", "_state")]
            check("NanoGPT never sees Anthropic's own fields", not leaked, ", ".join(leaked))
        eng.select_model("Anthropic", "claude-opus-5-5")
        entry, _, wire = await turn(eng, "Opus again", message="Summarise in two lines what we found and what to do.")
        if wire:
            print(f"    info: {thinking_blocks(wire[0]['body'])} thinking block(s) sent back after the switch")
    finally:
        await eng.stop()


async def s_stop():
    print("\n== stopped mid-reply, then carry on (Opus 5.5 on Anthropic, strict)", flush=True)
    eng = engine(search_mode="off")
    streamed = {"n": 0}
    eng._emit = lambda ev: streamed.__setitem__("n", streamed["n"] + len(ev.get("text", ""))) if ev.get("type") == "delta" else None
    await eng.start()
    try:
        await start(eng, "Anthropic", "claude-opus-5-5")
        eng.send("Explain in detail, in about 600 words, how ext4 journaling works. No commands.")
        for _ in range(1200):
            await asyncio.sleep(0.1)
            if streamed["n"] > 300 or not eng.busy:
                break
        eng.stop_turn()
        await wait_turn(eng)
        check("stopped mid-reply", eng.conv and "[response stopped by technician]" in str(eng.conv[-1].get("content")),
              f"{streamed['n']} characters streamed first")
        await turn(eng, "next message after the stop", message="Never mind. One line: what is a journal commit interval?")
    finally:
        await eng.stop()


async def s_review():
    print(f"\n== second opinion from another provider: chat {GLM} (NanoGPT), reviewer Haiku 5.5 (Anthropic)", flush=True)
    eng = engine(search_mode="off", review_model="Anthropic|claude-haiku-5-5")
    await eng.start()
    try:
        await start(eng, "NanoGPT", GLM)
        entry, _, _ = await turn(eng, "turn", message=DISK + " Also queue the command that would clear old journal files, "
                                 "for later, with its rollback note.")
        pend = [p for p in eng.queue.items if p.status == "pending"]
        if pend:
            r = await eng.review_command(pend[-1].num)
            check("reviewed by the other provider's model", r.get("model") == "claude-haiku-5-5" and r.get("text"),
                  f"{r.get('model')}: {str(r.get('text'))[:120]}")
    finally:
        await eng.stop()


async def s_training():
    print("\n== training provider", flush=True)
    eng = engine(search_mode="off")
    eng.cfg.providers.append(e2e.Provider("Training", "training://disk-full"))
    await eng.start()
    try:
        await start(eng, "Training", "training/disk-full")
        entry, _, _ = await turn(eng, "scripted turn", message="The disk is full.")
        check("scripted proposals", entry and entry.get("proposals"))
    finally:
        await eng.stop()


SCENARIOS = {"nano-claude": s_nano_claude, "route": s_route, "e2ee": s_e2ee, "tee": s_tee, "search-ask": s_search_ask,
             "research": s_research, "vision": s_vision, "switch": s_switch, "stop": s_stop, "review": s_review,
             "training": s_training}


async def main():
    for name, fn in SCENARIOS.items():
        if ONLY and name not in ONLY:
            continue
        try:
            await fn()
        except Exception as ex:  # noqa: BLE001 - one scenario's crash is a failed check, not the end of the run
            check(f"{name}: ran to the end", False, f"{type(ex).__name__}: {ex}"[:400])
    failed = [r for r in e2e.results if not r[1]]
    print(f"\n{len(e2e.results) - len(failed)}/{len(e2e.results)} checks passed; {len(e2e.wire)} Anthropic and "
          f"{len(nano_wire)} NanoGPT plain requests; Anthropic about ${e2e.cost():.2f}")
    for name, _, detail in failed:
        print("  FAILED:", name, "-", detail)


if __name__ == "__main__":
    asyncio.run(main())
