"""The WinRM console's interactive behaviour on fresh sessions: Ctrl+C (HTTP and HTTPS),
streaming of slow output, Read-Host, multi-line paste. Run before and after a fix."""

import asyncio
import re
import sys
import time

from lab import PASS, USER, WIN, App, Term

PROMPT = r"\[192\.168\.1\.214\]: PS [^\n]*> $"
results = []


def check(ok, what, detail=""):
    results.append((bool(ok), what, detail))
    print(("PASS " if ok else "FAIL ") + what + (f"\n     {detail}" if detail and not ok else ""), flush=True)


async def console(app, name):
    r = await app.aapi("POST", "/api/sessions", {"kind": "winrm", "host": name})
    t = Term(app, r["id"])
    await t.__aenter__()
    out = await t.wait(PROMPT, 0, 60)
    return t, out


async def run(t, cmd, timeout=90):
    mark = await t.paste(cmd)
    out = await t.wait(PROMPT, mark, timeout)
    return None if out is None else "\n".join(out.split("\n")[1:-1])


async def first_line_time(t, cmd, needle, total=60):
    """Seconds until `needle` first shows after the echo, and until the prompt is back."""
    t0 = time.monotonic()
    mark = await t.paste(cmd)
    first = None
    while time.monotonic() - t0 < total:
        body = t.text(mark).split("\n", 1)[1] if "\n" in t.text(mark) else ""
        if first is None and needle in body:
            first = time.monotonic() - t0
        if re.search(PROMPT, body):
            return first, time.monotonic() - t0, body
        await asyncio.sleep(0.1)
    return first, None, t.text(mark)


async def ctrl_c(t, label):
    mark = await t.paste("1..3 | ForEach-Object { \"tick $_\"; Start-Sleep 2 }; Start-Sleep 60; 'not-cancelled'")
    await asyncio.sleep(8)
    t0 = time.monotonic()
    await t.send("\x03")
    out = await t.wait(PROMPT, mark, 30)
    dt = time.monotonic() - t0
    body = out or t.text(mark)
    after = body.split("\n", 1)[1] if "\n" in body else ""
    check(out and "not-cancelled" not in after and dt < 5, f"{label}: Ctrl+C stops the command ({dt:.1f}s)", repr(body[-300:]))
    check(out and "tick 3" in after, f"{label}: output printed before Ctrl+C is kept", repr(body[-300:]))
    print(f"     {label} after Ctrl+C:", repr(body[-200:]))
    o = await run(t, "'still-alive'")
    check(o and "still-alive" in o, f"{label}: console usable after Ctrl+C", repr(o))


async def main():
    app = App("console", port=48601)
    app.start()
    listener = asyncio.create_task(app.listen())
    app.rules = [(r"Password for .*\(WinRM\)", PASS), (r"First connection to .*:5986", "yes")]
    try:
        await asyncio.sleep(0.5)
        app.api("POST", "/api/settings", {"companion_port": 48602})
        app.api("POST", "/api/case", {"name": "Win11 console test", "sensitivity": "open"})
        app.api("POST", "/api/hosts", {"host": {"name": "LabW11-C-HTTP", "kind": "winrm", "host": WIN, "user": USER,
                                                "auth": "negotiate", "winrm_ssl": False}})
        app.api("POST", "/api/hosts", {"host": {"name": "LabW11-C-HTTPS", "kind": "winrm", "host": WIN, "user": USER,
                                                "auth": "ntlm", "winrm_ssl": True, "winrm_cert_validation": False}})
        t, out = await console(app, "LabW11-C-HTTP")
        check(out, "HTTP console up")

        first, done, body = await first_line_time(t, "1..4 | ForEach-Object { \"line $_\"; Start-Sleep 2 }", "line 1")
        check(first is not None and first < 3, f"slow output streams: first line after {first and round(first, 1)}s, done after "
              f"{done and round(done, 1)}s", repr(body[-200:]))
        first, done, body = await first_line_time(t, "Write-Host 'host-1'; Start-Sleep 3; Write-Warning 'warn-2'; Start-Sleep 3; 'out-3'",
                                                  "host-1")
        check(first is not None and first < 3, f"Write-Host streams: after {first and round(first, 1)}s (done {done and round(done, 1)}s)",
              repr(body[-200:]))
        o = await run(t, "Get-Service WinRM, Spooler")
        check(o and re.search(r"Status\s+Name\s+DisplayName", o) and "Spooler" in o, "default table formatting intact", repr(o))
        o = await run(t, "Get-Item C:\\Windows | Format-List Name, FullName")
        check(o and "FullName : C:\\Windows" in o, "Format-List output intact", repr(o))
        o = await run(t, "$PSVersionTable.PSVersion.Major; Get-Date -Year 2001 -Month 2 -Day 3 -Format 'yyyy-MM-dd'")
        check(o and "5" in o and "2001-02-03" in o, "mixed outputs in one line", repr(o))
        o = await run(t, "Write-Error 'first-error'; 'after-error'")
        check(o and "first-error" in o and "after-error" in o, "non-terminating error, then output", repr(o))
        o = await run(t, "throw 'boom'")
        check(o and "boom" in o, "a throw is shown", repr(o))
        o = await run(t, "'x' * 5000")
        check(o and o.count("x") == 5000, "long line kept whole", f"{o and o.count('x')}")

        await ctrl_c(t, "HTTP/Negotiate")

        mark = await t.paste("Read-Host 'Name'")
        out = await t.wait(PROMPT, mark, 30)
        check(out is not None, "Read-Host returns (unsupported) instead of hanging", repr((out or t.text(mark))[-300:]))
        print("     Read-Host gave:", repr((out or t.text(mark))[-300:]))
        if out is None:
            await t.send("\x03")
            await t.wait(PROMPT, mark, 30)
        o = await run(t, "'after-read-host'")
        check(o and "after-read-host" in o, "console fine after Read-Host", repr(o))

        mark = await t.paste("'line-one'\n'line-two'")
        await asyncio.sleep(6)
        two = t.text(mark)
        print("     two-line paste gave:", repr(two[-300:]))
        check("line-one" in two and "line-two" in two and "Error" not in two, "a two-line paste runs both lines", repr(two[-300:]))

        await t.send("exit\r")
        await asyncio.sleep(1)
        await t.__aexit__()

        t, out = await console(app, "LabW11-C-HTTPS")
        check(out, "HTTPS console up")
        await ctrl_c(t, "HTTPS/NTLM")
        await t.send("exit\r")
        await asyncio.sleep(1)
        await t.__aexit__()
    finally:
        listener.cancel()
        app.quit()
    print(f"\n{sum(ok for ok, *_ in results)}/{len(results)} passed")
    sys.exit(0 if all(ok for ok, *_ in results) else 1)


asyncio.run(main())
