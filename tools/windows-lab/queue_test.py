"""Queue items in Windows sessions: recipes, watch, tool transfer, capture; WinRM and SSH.

    queue_test.py winrm|ssh [recipe ids, "watch", "tools"...]"""

import asyncio
import hashlib
import json
import os
import re
import sys
import time

if len(sys.argv) < 2 or sys.argv[1] not in ("winrm", "ssh"):
    sys.exit(__doc__)

from lab import PASS, SCRATCH, USER, WIN, App, Term  # noqa: E402

KIND = sys.argv[1]
ONLY = sys.argv[2:]
WINRM_PROMPT = r"\[192\.168\.1\.214\]: PS [^\n]*> $"
SSH_PROMPT = r"PS C:\\[^\n]*>\s*$"
PROMPT = WINRM_PROMPT if KIND == "winrm" else SSH_PROMPT
ERR = re.compile(r"CategoryInfo|FullyQualifiedErrorId|is not recognized|Exception|Cannot find|Access is denied|"
                 r"not supported|ParameterBinding|cannot be|Cannot bind|WinRMTransportError|InvalidOperation|"
                 r"A parameter cannot", re.I)
results = []
report = []


def check(ok, what, detail=""):
    results.append((bool(ok), what, detail))
    print(("PASS " if ok else "FAIL ") + what + (f"\n     {detail}" if detail and not ok else ""), flush=True)


async def run_item(app, t, num, timeout=900):
    item = (await app.aapi("POST", f"/api/queue/{num}", {"status": "ran"}))["item"]
    t0 = time.monotonic()
    mark = await t.paste(item["command"])
    out = await t.wait(PROMPT, mark, timeout)
    dt = time.monotonic() - t0
    if out is None:                    # stuck: stop it so the next item has a prompt
        await t.send("\x03")
        await t.wait(PROMPT, mark, 60)
    cap = await app.aapi("GET", f"/api/queue/{num}/capture")
    raw = t.buf[mark:]
    return item, out, cap.get("text", ""), dt, raw


async def main():
    app = App(f"queue-{KIND}", port=48594 if KIND == "winrm" else 48595)
    app.start()
    listener = asyncio.create_task(app.listen())
    app.rules = [(r"Password for .*\(WinRM\)", PASS), (r"password", PASS)]
    try:
        await asyncio.sleep(0.5)
        app.api("POST", "/api/settings", {"companion_port": 48596 if KIND == "winrm" else 48597})
        app.api("POST", "/api/case", {"name": f"Win11 queue test {KIND}", "sensitivity": "open"})
        if KIND == "winrm":
            h = {"name": "LabW11-Q", "kind": "winrm", "host": WIN, "user": USER, "auth": "ntlm", "winrm_ssl": False}
        else:
            h = {"name": "LabW11-SSH", "kind": "ssh", "host": WIN, "user": USER, "auth": "key",
                 "key_file": os.path.expanduser("~/.ssh/p330_vms"), "os_hint": "Windows 11",
                 "ssh_options": ["HostKeyAlias=p330-2-win11", "StrictHostKeyChecking=yes", "IdentitiesOnly=yes"]}
        check(app.api("POST", "/api/hosts", {"host": h}).get("ok"), "host saved")
        sess = await app.aapi("POST", "/api/sessions", {"kind": KIND, "host": h["name"]})
        print("     session roster:", json.dumps({k: sess.get(k) for k in ("id", "kind", "shell", "os_hint", "target")}))
        sid = sess["id"]
        async with Term(app, sid) as t:
            out = await t.wait(PROMPT, 0, 60)
            check(out, "session prompt", t.text()[-500:])
            if KIND == "ssh":
                print("     ssh first screen (raw, 400 chars):", repr(t.buf[:400]))

            # ---- recipes
            recipes = [r for r in app.api("GET", "/api/recipes")["recipes"] if r["os"] == "windows"]
            for rec in recipes:
                if ONLY and rec["id"] not in ONLY:
                    continue
                if rec["id"] == "watch-memory-windows":
                    continue
                nums = app.api("POST", f"/api/recipes/{rec['id']}/queue", {"session_id": sid})["nums"]
                for num in nums:
                    item, out, cap, dt, raw = await run_item(app, t, num)
                    body = cap
                    errs = sorted({m.group(0) for m in ERR.finditer(body)})
                    red = b"\x1b[31m" in raw or b"\x1b[91m" in raw
                    ok = out is not None and not errs
                    report.append({"recipe": rec["id"], "num": num, "purpose": item["purpose"], "secs": round(dt, 1),
                                   "ok": ok, "errors": errs, "red": red, "capture": body[-1500:]})
                    check(ok, f"{rec['id']} #{num} {item['purpose'][:60]} ({dt:.1f}s)",
                          ("TIMEOUT " if out is None else "") + "; ".join(errs) + "\n     " + body[-700:].replace("\n", "\n     "))

            # ---- watch: when does output appear?
            if not ONLY or "watch" in ONLY:
                nums = app.api("POST", "/api/recipes/watch-memory-windows/queue", {"session_id": sid})["nums"]
                w = app.api("POST", f"/api/queue/{nums[0]}/watch", {"interval": 3, "count": 4})
                print("     watch command:", w.get("command", w)[:300])
                item = (await app.aapi("POST", f"/api/queue/{nums[0]}", {"status": "ran"}))["item"]
                t0 = time.monotonic()
                mark = await t.paste(item["command"])
                seen = []
                while time.monotonic() - t0 < 120:
                    txt = t.text(mark)
                    n = len(re.findall(r"^=== WATCH \d+/4 ", txt.split("\n", 1)[-1] if "\n" in txt else "", re.M))
                    if not seen or n != seen[-1][1]:
                        seen.append((round(time.monotonic() - t0, 1), n))
                    if re.search(PROMPT, txt.split("\n", 1)[-1] if "\n" in txt else ""):
                        break
                    await asyncio.sleep(0.25)
                print("     watch stamps seen over time (s, count):", seen)
                check(seen[-1][1] == 4, "watch ran 4 iterations", t.text(mark)[-800:])
                first = next((s for s, n in seen if n >= 1), None)
                check(first is not None and first < 3, f"first watch iteration shows while the watch runs (at {first}s)")
                prev = app.api("POST", "/api/preview", {"texts": [(app.api("GET", f"/api/queue/{nums[0]}/capture")).get("text", "")],
                                                         "nums": [nums[0]]})["items"][0]
                print(f"     watch preview (collapsed {prev['collapsed']}):\n     " + prev["text"][-900:].replace("\n", "\n     "))

            # ---- tool transfer
            if not ONLY or "tools" in ONLY:
                tooldir = SCRATCH / "tools"
                tooldir.mkdir(exist_ok=True)
                for size in (1024, 300 * 1024, 1500 * 1024, 3900 * 1024):
                    p = tooldir / f"tool{size // 1024}k.bin"
                    if not p.exists():
                        p.write_bytes(os.urandom(size))
                    sha = hashlib.sha256(p.read_bytes()).hexdigest()
                    name = f"LabTool{size // 1024}k"
                    r = app.api("POST", "/api/tools", {"path": str(p), "name": name, "os": "windows"})
                    if r.get("_error") and "already" not in str(r):
                        check(False, f"tool {name} added", r)
                        continue
                    if KIND == "ssh":
                        if not any(s["kind"] == "local" and not s.get("exited") for s in app.api("GET", "/api/state")["sessions"]):
                            app.api("POST", "/api/sessions", {"kind": "local"})
                    tr = app.api("POST", f"/api/tools/{name}/transfer", {"session_id": sid})
                    if tr.get("_error"):
                        check(False, f"transfer {name} queued", tr)
                        continue
                    q = {i["num"]: i for i in app.api("GET", "/api/state")["queue"]}
                    for num in tr["nums"]:
                        it = q[num]
                        print(f"     {name}: #{num} in {it['session_id']} risk={it['risk']} len={len(it['command'])}")
                        if it["session_id"] == sid:
                            item, out, cap, dt, raw = await run_item(app, t, num, timeout=600)
                            tail = cap[-300:]
                        else:            # the scp in the local session
                            async with Term(app, it["session_id"]) as lt:
                                await lt.wait(r"[$#] $", 0, 20)
                                await app.aapi("POST", f"/api/queue/{num}", {"status": "ran"})
                                t0 = time.monotonic()
                                m = await lt.paste(it["command"] + "; echo SCP-RC=$?")
                                out = await lt.wait(r"SCP-RC=\d+\s*\n", m, 300)
                                dt = time.monotonic() - t0
                                tail = (out or lt.text(m))[-300:]
                        ok = out is not None and ("True" in tail or sha in tail or "SCP-RC=0" in tail)
                        check(ok, f"{name} step #{num} ({dt:.1f}s)", tail)
    finally:
        (SCRATCH / f"report-{KIND}.json").write_text(json.dumps(report, indent=1))
        listener.cancel()
        app.quit()
    print(f"\n{sum(ok for ok, *_ in results)}/{len(results)} passed")
    sys.exit(0 if all(ok for ok, *_ in results) else 1)


asyncio.run(main())
