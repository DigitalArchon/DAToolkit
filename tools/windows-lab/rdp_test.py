"""RDP to the Windows 11 VM through the real page (headless Firefox) and guacd."""

import asyncio
import base64
import json
import subprocess
import sys
import time

from bidi import Firefox
from lab import PASS, SCRATCH, USER, WIN, App

OUT = SCRATCH / "rdp"
OUT.mkdir(exist_ok=True)
results = []
ONLY = sys.argv[1:]


def check(ok, what, detail=""):
    results.append((bool(ok), what, detail))
    print(("PASS " if ok else "FAIL ") + what + (f"\n     {detail}" if detail and not ok else ""), flush=True)


def win_ps(script: str) -> str:
    """Run a PowerShell script on the VM over ssh (copied over, so no quoting through ssh)."""
    p = SCRATCH / "win" / "tmp.ps1"
    p.write_text(script, encoding="utf-8-sig")
    subprocess.run(["scp", "-q", str(p), "p330-2-win11:tmp.ps1"], check=True)
    r = subprocess.run(["ssh", "p330-2-win11", "powershell -NoProfile -ExecutionPolicy Bypass -File tmp.ps1"],
                       capture_output=True, text=True, timeout=120)
    return r.stdout + r.stderr


def file_text(path: str) -> str:
    out = win_ps(f"$b = [IO.File]::ReadAllBytes('{path}'); [Convert]::ToBase64String($b)")
    try:
        raw = base64.b64decode(out.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        return f"<unreadable: {out[-300:]}>"
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return repr(raw)


STATS = """
const r = S.rdps[%r]; if (!r || r.state !== 3) return {state: r ? r.state : null};
const c = r.client.getDisplay().flatten(); const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
let non = 0, n = 0; const colors = new Set();
for (let i = 0; i < d.length; i += 4 * 61) { n++; const v = (d[i] << 16) | (d[i+1] << 8) | d[i+2]; if (Math.max(d[i], d[i+1], d[i+2]) > 40) non++; colors.add(v); }
return {state: r.state, w: c.width, h: c.height, nonblack: non / n, colors: colors.size, png: %s ? c.toDataURL('image/png') : ''};
"""


async def stats(ff, sid, name=None):
    s = await ff.js(STATS % (sid, "true" if name else "false"))
    if name and s and s.get("png"):
        (OUT / f"{name}.png").write_bytes(base64.b64decode(s["png"].split(",", 1)[1]))
        s["png"] = f"{name}.png"
    return s


async def open_rdp(app, ff, host, timeout=40):
    r = await app.aapi("POST", "/api/sessions", {"kind": "rdp", "host": host})
    if "_error" in r:
        return r, None
    sid = r["id"]
    await ff.wait_js(f"return !!S.rdps[{sid!r}]", 10)
    await ff.js(f"activateTab({sid!r}); return 1")
    st = await ff.wait_js(f"const r = S.rdps[{sid!r}]; return r && (r.state === 3 || r.state === 5 || /error/.test(r.status.textContent)) ? "
                          f"{{state: r.state, status: r.status.textContent}} : null", timeout)
    return r, st


async def keys(ff, sid, ks):
    await ff.js(f"rdpKeys(S.rdps[{sid!r}], {json.dumps(ks)}); return 1")


async def typ(ff, sid, text, enter=True):
    await ff.js(f"await rdpType(S.rdps[{sid!r}], {json.dumps(text)}, {json.dumps(enter)}); return 1")


async def main():
    app = App("rdp", port=48598)
    app.start()
    listener = asyncio.create_task(app.listen())
    app.rules = [(r"First connection to 192\.168\.1\.214:3389", "yes"), (r"RDP password for", PASS),
                 (r"no TLS certificate", "yes"), (r"Password for .*\(WinRM\)", PASS)]
    try:
        await asyncio.sleep(0.5)
        app.api("POST", "/api/settings", {"companion_port": 48599})
        app.api("POST", "/api/case", {"name": "Win11 RDP lab test", "sensitivity": "open"})
        for name, sec in (("LabW11-RDP", "any"), ("LabW11-RDP-NLA", "nla"), ("LabW11-RDP-TLS", "tls"),
                          ("LabW11-RDP-Legacy", "rdp"), ("LabW11-RDP-NLAEXT", "nla-ext"), ("LabW11-RDP-BadPass", "nla")):
            r = app.api("POST", "/api/hosts", {"host": {"name": name, "kind": "rdp", "host": WIN, "user": USER,
                                                        "rdp_security": sec, "rdp_layout": "en-us-qwerty"}})
            check(r.get("ok"), f"host {name} saved", r)
        app.api("POST", "/api/hosts", {"host": {"name": "LabW11-WinRM", "kind": "winrm", "host": WIN, "user": USER,
                                                "auth": "ntlm", "winrm_ssl": False}})

        win_ps("Remove-Item -ErrorAction SilentlyContinue $HOME\\rdp-typed.txt, $HOME\\rdp-clip.txt; "
               "Get-Process powershell -ErrorAction SilentlyContinue | Where-Object SessionId -ne 0 | "
               "Where-Object { $_.MainWindowHandle -ne 0 } | Stop-Process -Force")
        async with Firefox(SCRATCH / "ffprof") as ff:
            await ff.viewport(1700, 1100)
            await ff.goto(f"{app.base}/#t={app.token}")
            ok = await ff.wait_js("return !!(S.state && S.state.case)", 20)
            check(ok, "page loaded with the case")
            await ff.js("window.__toasts = []; const t0 = toast; toast = (text, kind, ms) => { window.__toasts.push([kind, String(text)]); return t0(text, kind, ms); }; return 1")
            await ff.js("window.__rdplog = []; const W = WebSocket; window.WebSocket = function (u, p) { const w = new W(u, p); if (/ws\\/rdp/.test(u)) { const t0 = Date.now(); w.addEventListener('close', (e) => window.__rdplog.push(['close', new Date().toISOString(), e.code, e.reason, e.wasClean])); w.addEventListener('error', () => window.__rdplog.push(['error', new Date().toISOString()])); } return w; }; window.WebSocket.prototype = W.prototype; Object.assign(window.WebSocket, {CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3}); return 1")

            # ---- Negotiate: first connection, certificate, desktop
            t0 = time.monotonic()
            r, st = await open_rdp(app, ff, "LabW11-RDP")
            check(st and st["state"] == 3, f"Negotiate connects ({time.monotonic() - t0:.1f}s)", (r, st))
            cert = [p["text"] for p in app.prompts if "3389" in p["text"]]
            if cert:
                print("     ---- RDP certificate prompt ----\n     " + cert[0].replace("\n", "\n     "))
            if not (st and st["state"] == 3):
                print("     toasts:", app.toasts()[-5:])
                return
            sid = r["id"]
            roster = {s["id"]: s for s in app.api("GET", "/api/state")["sessions"]}[sid]
            print("     roster:", json.dumps({k: roster.get(k) for k in ("connected", "pin_enforced", "target", "os_hint", "device")}))
            await asyncio.sleep(8)
            s = await stats(ff, sid, "01-desktop")
            check(s and s["nonblack"] > 0.02 and s["colors"] > 50, f"desktop drawn ({s and s['w']}x{s and s['h']}, "
                  f"{s and round(s['nonblack'] * 100)}% non-black, {s and s['colors']} colours)", s)
            await ff.shot(OUT / "01-page.png")

            # ---- Win+R, then PowerShell
            await keys(ff, sid, [0xFFEB, 0x72])
            await asyncio.sleep(3)
            await stats(ff, sid, "02-winr")
            await typ(ff, sid, "powershell")
            await asyncio.sleep(10)
            await stats(ff, sid, "03-powershell")
            typed = 'typed {ok} | $x @(1) <a> "q" `t ~ ^ % & * _ + = [ ] ; : , . / ? \\ Grüße € ✓'
            await typ(ff, sid, f"Set-Content -Encoding UTF8 -Path $HOME\\rdp-typed.txt -Value '{typed}'")
            await asyncio.sleep(4)
            got = file_text(r"C:\Users\test\rdp-typed.txt").strip()
            check(got == typed, "typed text arrives exactly (US keys, symbols, Latin-1, €, ✓)", f"{got!r}\n     expected {typed!r}")
            await stats(ff, sid, "04-typed")

            # ---- a queued command through Run (the real button path, with its one-time confirmation)
            nums = app.api("POST", "/api/recipes/dns-reach-windows/queue", {"session_id": sid})["nums"]
            num = nums[-1]
            await ff.wait_js(f"return S.state.queue.some((i) => i.num === {num})", 10)
            await ff.js(f"window.__run = runItem({num}, 'run').then(() => 'done', (e) => 'error: ' + e.message); return 1")
            clicked = await ff.wait_js("const b = [...document.querySelectorAll('#modal-root button')].find((x) => x.textContent === 'Type and press Enter');"
                                       " if (b) { b.click(); return true; } return false;", 10)
            check(clicked, "Run asks once before typing into the remote desktop")
            res = await ff.js("return await window.__run")
            item = {i["num"]: i for i in app.api("GET", "/api/state")["queue"]}[num]
            check(res == "done" and item["status"] == "ran", f"queued item typed and marked ran ({res}, {item['status']})")
            await asyncio.sleep(5)
            await stats(ff, sid, "05-run-item")

            # ---- clipboard both ways
            await ff.js(f"const r = S.rdps[{sid!r}]; const w = new Guacamole.StringWriter(r.client.createClipboardStream('text/plain'));"
                        " w.sendText('clip-to-remote Grüße € ✓'); w.sendEnd(); return 1")
            await asyncio.sleep(1.5)
            await typ(ff, sid, "Get-Clipboard | Set-Content -Encoding UTF8 $HOME\\rdp-clip.txt")
            await asyncio.sleep(4)
            got = file_text(r"C:\Users\test\rdp-clip.txt").strip()
            check(got == "clip-to-remote Grüße € ✓", "clipboard to the remote desktop", repr(got))
            await typ(ff, sid, "Set-Clipboard -Value 'clip-from-remote Grüße € ✓'")
            got = await ff.wait_js(f"return S.rdps[{sid!r}].clipboard", 10)
            check(got == "clip-from-remote Grüße € ✓", "clipboard from the remote desktop (Clipboard → AI source)", repr(got))
            btn = await ff.js(f"return !S.rdps[{sid!r}].clipBtn.disabled")
            check(btn, "Clipboard → AI enabled once text was copied there")

            # ---- Ctrl+Alt+Del and back
            await keys(ff, sid, [0xFFE3, 0xFFE9, 0xFFFF])
            await asyncio.sleep(4)
            cad = await stats(ff, sid, "06-ctrl-alt-del")
            await keys(ff, sid, [0xFF1B])
            await asyncio.sleep(3)
            back = await stats(ff, sid, "07-after-escape")
            print(f"     Ctrl+Alt+Del screen colours {cad and cad['colors']}, after Escape {back and back['colors']} (see the PNGs)")

            # ---- Fit to window (display-update)
            await ff.viewport(1400, 900)
            await asyncio.sleep(2)
            before = await ff.js(f"const r = S.rdps[{sid!r}]; const d = r.client.getDisplay(); return {{w: d.getWidth(), h: d.getHeight(), view: rdpViewSize(r), due: r.fitBtn.classList.contains('due')}}")
            check(before and before["due"], f"Fit to window lights up after the tab shrank ({before})")
            await ff.js(f"rdpFitToWindow(S.rdps[{sid!r}]); return 1")
            after = await ff.wait_js(f"const r = S.rdps[{sid!r}]; return rdpFits(r) ? {{w: r.client.getDisplay().getWidth(), h: r.client.getDisplay().getHeight(), view: rdpViewSize(r), reconnect: !!r.fitByReconnect}} : null", 15)
            check(after and not after["reconnect"], f"resized live by display-update ({after})", after)
            await asyncio.sleep(3)
            s = await stats(ff, sid, "08-fitted")
            check(s and s["nonblack"] > 0.02, "desktop redrawn after resize", s)

            # ---- reconnect resumes the same Windows session
            await ff.js(f"rdpConnect(S.rdps[{sid!r}]); return 1")
            st = await ff.wait_js(f"return S.rdps[{sid!r}].state === 3", 30)
            await asyncio.sleep(6)
            s = await stats(ff, sid, "09-reconnected")
            check(st and s and s["nonblack"] > 0.02, "Reconnect: connected and drawn again", s)
            sess = win_ps("query user 2>&1 | Out-String")
            print("     sessions on Windows after reconnect:\n     " + sess.strip().replace("\n", "\n     "))

            # ---- linked with a WinRM session to the same address
            w = await app.aapi("POST", "/api/sessions", {"kind": "winrm", "host": "LabW11-WinRM"})
            await asyncio.sleep(3)
            ros = {s["id"]: s for s in app.api("GET", "/api/state")["sessions"]}
            check(w.get("id") and ros[w["id"]].get("device") == ros[sid].get("device"),
                  "WinRM session auto-linked to the RDP session (same address)",
                  {k: (v.get("device"), v["kind"]) for k, v in ros.items()})
            app.api("DELETE", f"/api/sessions/{w['id']}")

            app.api("DELETE", f"/api/sessions/{sid}")
            await asyncio.sleep(2)

            # ---- the other security modes
            for host, expect in (("LabW11-RDP-NLA", True), ("LabW11-RDP-NLAEXT", None), ("LabW11-RDP-TLS", False),
                                 ("LabW11-RDP-Legacy", False)):
                n = len(app.prompts)
                t0 = time.monotonic()
                r, st = await open_rdp(app, ff, host, timeout=45)
                asked = [p["text"].split("\n")[0][:110] for p in app.prompts[n:]]
                await asyncio.sleep(1.5)
                toasts = await ff.js("const t = window.__toasts.splice(0); return t")
                status = await ff.js(f"const r = S.rdps[{r.get('id')!r}]; return r ? r.status.textContent : null") if "id" in r else None
                ok = st and st["state"] == 3
                print(f"     {host}: {'connected' if ok else 'not connected'} in {time.monotonic() - t0:.1f}s; "
                      f"status now={status!r}; asked={asked}; api={r.get('detail') if '_error' in r else ''}; toast={toasts}")
                if ok:
                    await asyncio.sleep(5)
                    s = await stats(ff, r["id"], f"10-{host}")
                    ok = s and s["nonblack"] > 0.02
                if expect is not None:
                    check(bool(ok) == expect, f"{host}: {'connects' if expect else 'refused'}", (st, r))
                if "id" in r:
                    app.api("DELETE", f"/api/sessions/{r['id']}")
                    await asyncio.sleep(2)

            # ---- wrong password
            app.rules.insert(0, (r"RDP password for", "wrong-" + PASS[::-1]))
            r, st = await open_rdp(app, ff, "LabW11-RDP-BadPass", timeout=45)
            app.rules.pop(0)
            await asyncio.sleep(1.5)
            toasts = await ff.js("const t = window.__toasts.splice(0); return t")
            status = await ff.js(f"const r = S.rdps[{r.get('id')!r}]; return r ? r.status.textContent : null") if "id" in r else None
            print(f"     wrong password: status now={status!r} toast={toasts}")
            check(st and st["state"] != 3 and any("Authentication" in t[1] or "credential" in t[1].lower() for t in toasts), "wrong password: not connected, and the page says why", (st, toasts, status))
            if "id" in r:
                app.api("DELETE", f"/api/sessions/{r['id']}")
            await ff.shot(OUT / "11-page-end.png")
            print("     rdp websocket closes seen by the page:", await ff.js("return window.__rdplog"))
    finally:
        listener.cancel()
        app.quit()
    print(f"\n{sum(ok for ok, *_ in results)}/{len(results)} passed")


if __name__ == "__main__":
    asyncio.run(main())
