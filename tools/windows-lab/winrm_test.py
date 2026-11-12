"""WinRM against the Windows 11 VM: auth modes, HTTPS pinning, the console's behaviour."""

import asyncio
import json
import re
import sys
import time

from lab import PASS, USER, WIN, App, Term

PROMPT = r"\[192\.168\.1\.214\]: PS [^\n]*> $"
results = []


def check(ok, what, detail=""):
    results.append((bool(ok), what, detail))
    print(("PASS " if ok else "FAIL ") + what + (f"\n     {detail}" if detail and not ok else ""), flush=True)


def host(name, **kw):
    return {"name": name, "kind": "winrm", "host": WIN, "user": USER, "auth": "negotiate", "winrm_ssl": False, **kw}


async def run(t: Term, cmd: str, timeout=90):
    mark = await t.paste(cmd)
    out = await t.wait(PROMPT, mark, timeout)
    if out is None:
        return None
    # drop the echoed command line and the trailing prompt
    lines = out.split("\n")
    return "\n".join(lines[1:-1]).strip("\n")


async def open_console(app, name, expect_ok=True, timeout=60):
    r = await app.aapi("POST", "/api/sessions", {"kind": "winrm", "host": name})
    if "_error" in r:
        return r, None, ""
    t = Term(app, r["id"])
    await t.__aenter__()
    out = await t.wait(PROMPT + "|Connection failed[^\n]*\n|No password provided|No pinned certificate[^\n]*", 0, timeout)
    return r, t, out or t.text()


async def main():
    app = App("winrm")
    app.start()
    listener = asyncio.create_task(app.listen())
    app.rules = [(r"Password for .*\(WinRM\)", PASS)]
    try:
        await asyncio.sleep(0.5)
        app.api("POST", "/api/settings", {"companion_port": 48593})
        app.api("POST", "/api/case", {"name": "Win11 WinRM lab test", "sensitivity": "open"})

        # ---- save-time rules
        r = app.api("POST", "/api/hosts", {"host": host("LabW11-Basic-HTTP", auth="basic")})
        check(r.get("_error") and "Allow Basic over plain HTTP" in r["detail"], "Basic over HTTP refused without the tick", r)
        # Kerberos to an address can never work (the domain issues tickets by name), so the host
        # is refused when saved, with what to use instead. Until Nov 2026 it was saved and failed
        # on connecting; Kerberos that works is kerberos_test.py's, against the lab's domain.
        r = app.api("POST", "/api/hosts", {"host": host("LabW11-Kerb", auth="kerberos")})
        check(r.get("_error") == 400 and "Host not saved. Kerberos needs the computer's DNS name" in str(r.get("detail"))
              and "not an IP address" in str(r.get("detail")) and "Negotiate" in str(r.get("detail")),
              "Kerberos to an IP address refused when saved, naming the DNS name and Negotiate", r)
        saved = [h["name"] for h in app.api("GET", "/api/state")["config"]["hosts"]]
        check("LabW11-Kerb" not in saved, "the refused host is not among the saved ones", saved)
        for h in (host("LabW11-HTTP"), host("LabW11-NTLM", auth="ntlm"),
                  host("LabW11-Basic-HTTP", auth="basic", winrm_plain_basic=True),
                  host("LabW11-HTTPS-CA", winrm_ssl=True),
                  host("LabW11-HTTPS-Pin", winrm_ssl=True, winrm_cert_validation=False),
                  host("LabW11-HTTPS-Basic", winrm_ssl=True, winrm_cert_validation=False, auth="basic"),
                  host("LabW11-BadPass", auth="ntlm")):
            r = app.api("POST", "/api/hosts", {"host": h})
            check(r.get("ok"), f"host {h['name']} saved", r)

        # ---- Negotiate over HTTP: the console itself
        t0 = time.monotonic()
        r, t, out = await open_console(app, "LabW11-HTTP")
        check(t and re.search(PROMPT, out), f"Negotiate/HTTP connects ({time.monotonic() - t0:.1f}s)", out[-500:])
        if t and re.search(PROMPT, out):
            o = await run(t, "$x = 42")
            o = await run(t, "$x * 2")
            check(o is not None and o.strip() == "84", "variables persist between commands", repr(o))
            o = await run(t, "Write-Host 'hello-host'; Write-Output 'hello-out'")
            check(o and "hello-host" in o and "hello-out" in o, "Write-Host and output both shown", repr(o))
            o = await run(t, "Write-Warning 'w-one'; Write-Error 'e-one'; Write-Verbose 'v-one' -Verbose; 'after'")
            check(o and "WARNING: w-one" in o and "e-one" in o and "VERBOSE: v-one" in o and "after" in o,
                  "warning, error and verbose streams shown", repr(o))
            o = await run(t, "Get-NoSuchCmdlet")
            check(o and "not recognized" in o, "unknown cmdlet gives PowerShell's error", repr(o))
            o = await run(t, "\"Grüße – € ✓ 日本\"")
            check(o and "Grüße – € ✓ 日本" in o, "non-ASCII text round-trips", repr(o))
            o = await run(t, "Set-Location C:\\Windows\\System32")
            check(t.text().rstrip().endswith("PS C:\\Windows\\System32>"), "prompt follows Set-Location", t.text()[-200:])
            o = await run(t, "whoami; whoami /groups | Select-String 'Mandatory Label'")
            check(o and "win11test\\test" in o.lower() and "High Mandatory Level" in o,
                  "remote token is the full admin one (LocalAccountTokenFilterPolicy)", repr(o))
            o = await run(t, "ipconfig | Select-String IPv4")
            check(o and WIN in o, "native exe output (ipconfig)", repr(o))
            o = await run(t, "cmd /c 'echo from-cmd & ver'")
            check(o and "from-cmd" in o and "Version 10.0.26300" in o, "cmd /c works, build 26300", repr(o))
            o = await run(t, "Get-Service WinRM,TermService | Format-Table Name,Status -AutoSize")
            check(o and "WinRM" in o and "Running" in o, "formatted table output", repr(o))
            o = await run(t, "'x' * 300")
            check(o and len(o.replace("\n", "")) >= 300, "a 300-character line is not cut", f"{len(o or '')}")
            t0 = time.monotonic()
            o = await run(t, "1..20000", timeout=180)
            dt = time.monotonic() - t0
            check(o and o.strip().endswith("20000"), f"20000 lines of output ({dt:.1f}s)", (o or "")[-200:])
            # Ctrl+C while a command runs
            mark = await t.paste("Start-Sleep 60; 'not-cancelled'")
            await asyncio.sleep(3)
            t0 = time.monotonic()
            await t.send("\x03")
            out = await t.wait(PROMPT, mark, 30)
            dt = time.monotonic() - t0
            after = out.split("\n", 1)[1] if out and "\n" in out else ""
            check(out and "(stopped)" in after and "not-cancelled" not in after and dt < 5, f"Ctrl+C stops a running command ({dt:.1f}s)",
                  repr((out or t.text(mark))[-300:]))
            o = await run(t, "'still-alive'")
            check(o and "still-alive" in o, "console usable after Ctrl+C", repr(o))
            # Read-Host: documented as unsupported; must not hang the console for good
            mark = await t.paste("Read-Host 'Name'")
            out = await t.wait(PROMPT, mark, 30)
            check(out is not None, "Read-Host returns (error) instead of hanging", repr((out or t.text(mark))[-400:]))
            if out is None:
                await t.send("\x03")
                await t.wait(PROMPT, mark, 20)
            print("     Read-Host gave:", repr((out or "")[-300:]))
            # multi-line paste: the console is single-line
            mark = await t.paste("'line-one'\n'line-two'")
            await asyncio.sleep(6)
            print("     two-line paste gave:", repr(t.text(mark)[-400:]))
            mark = len(t.buf)
            await t.send("exit\r")
            out = await t.wait(r"Disconnected\.", mark, 20)
            check(out, "exit disconnects cleanly", repr(t.text(mark)))
            await t.__aexit__()

        # ---- NTLM explicitly
        r, t, out = await open_console(app, "LabW11-NTLM")
        check(t and re.search(PROMPT, out), "NTLM/HTTP connects", out[-400:])
        if t:
            o = await run(t, "$PSVersionTable.PSVersion.ToString(); [Environment]::OSVersion.Version.Build")
            check(o and "26300" in o, "PS version and build over NTLM", repr(o))
            await t.send("exit\r")
            await asyncio.sleep(1)
            await t.__aexit__()

        # ---- wrong password
        app.rules.insert(0, (r"Password for .*\(WinRM\)", "wrong-" + PASS[::-1]))
        r, t, out = await open_console(app, "LabW11-BadPass")
        check(t and "Connection failed" in out and "Traceback" not in out, "wrong password: one clear error line",
              out[-600:])
        print("     wrong password gave:", repr(out.strip()[-400:]))
        app.rules.pop(0)
        if t:
            await t.__aexit__()

        # ---- Basic over plain HTTP: the server refuses (Basic and AllowUnencrypted are off by default)
        r, t, out = await open_console(app, "LabW11-Basic-HTTP", timeout=60)
        print("     Basic/HTTP against a default server gave:", repr(out.strip()[-500:]))
        check(t and "Traceback" not in out and "Connection failed" in out, "Basic/HTTP refused by default server, clear error", out[-500:])
        if t:
            await t.__aexit__()

        # ---- HTTPS validated against the CA store: self-signed listener fails closed
        r, t, out = await open_console(app, "LabW11-HTTPS-CA", timeout=60)
        print("     HTTPS + CA check against self-signed gave:", repr(out.strip()[-500:]))
        check(t and "Connection failed" in out and re.search(r"certificate|SSL", out, re.I), "self-signed listener refused under CA validation", out[-500:])
        if t:
            await t.__aexit__()

        # ---- HTTPS pinned: first use asks, shows the certificate
        app.rules.insert(0, (r"First connection to .*:5986", "yes"))
        r, t, out = await open_console(app, "LabW11-HTTPS-Pin")
        cert_prompts = [p for p in app.prompts if "5986" in p["text"]]
        check(cert_prompts, "first HTTPS connection asks to trust the certificate")
        if cert_prompts:
            print("     ---- certificate prompt ----\n     " + cert_prompts[-1]["text"].replace("\n", "\n     "))
        check(t and re.search(PROMPT, out), "pinned HTTPS connects after trusting", out[-400:])
        if t:
            o = await run(t, "'over-https'")
            check(o and "over-https" in o, "commands over pinned HTTPS", repr(o))
            await t.send("exit\r")
            await asyncio.sleep(1)
            await t.__aexit__()
        n = len(app.prompts)
        r, t, out = await open_console(app, "LabW11-HTTPS-Pin")
        check(len(app.prompts) - n == 1 and re.search(PROMPT, out), "second connection: only the password is asked",
              [p["text"][:60] for p in app.prompts[n:]])
        if t:
            await t.send("exit\r")
            await asyncio.sleep(1)
            await t.__aexit__()
        app.rules.pop(0)
        pins = json.loads((app.root / "config/datoolkit/rdp_pins.json").read_text()) if (app.root / "config/datoolkit/rdp_pins.json").exists() else {}
        print("     pins file:", json.dumps(pins)[:300])
    finally:
        state = app.api("GET", "/api/state")
        sessions = state.get("sessions", [])
        print("     sessions at the end:", [(s["id"], s["kind"], s.get("exited"), s.get("device")) for s in sessions])
        listener.cancel()
        app.quit()
    print(f"\n{sum(ok for ok, *_ in results)}/{len(results)} passed")
    sys.exit(0 if all(ok for ok, *_ in results) else 1)


asyncio.run(main())
