"""WinRM Basic authentication over HTTPS and plain HTTP.

CHANGES THE VM for the test: turns on Basic, then AllowUnencrypted, and turns both off again
at the end (Windows' defaults)."""

import asyncio, re
from lab import PASS, USER, WIN, App, Term
from rdp_test import win_ps
PROMPT = r"\[192\.168\.1\.214\]: PS [^\n]*> $"

async def try_host(app, name):
    r = await app.aapi("POST", "/api/sessions", {"kind": "winrm", "host": name})
    async with Term(app, r["id"]) as t:
        out = await t.wait(PROMPT + r"|Connection failed[^\n]*\n", 0, 60)
        if out and re.search(PROMPT, out):
            m = await t.paste("\"basic-ok $([Security.Principal.WindowsIdentity]::GetCurrent().AuthenticationType)\"")
            o = await t.wait(PROMPT, m, 30)
            await t.send("exit\r"); await asyncio.sleep(1)
            return "connected: " + (o or "").split("\n")[1]
        return (out or t.text())[-300:].strip()

async def main():
    print(win_ps("winrm get winrm/config/service | Select-String 'AllowUnencrypted|Basic'"))
    app = App("basic", port=48608)
    app.start()
    listener = asyncio.create_task(app.listen())
    app.rules = [(r"Password for .*\(WinRM\)", PASS), (r"First connection to", "yes")]
    try:
        await asyncio.sleep(0.5)
        app.api("POST", "/api/settings", {"companion_port": 48609})
        app.api("POST", "/api/case", {"name": "basic", "sensitivity": "open"})
        app.api("POST", "/api/hosts", {"host": {"name": "LabW11-Basic-HTTP", "kind": "winrm", "host": WIN, "user": USER, "auth": "basic",
                                                "winrm_ssl": False, "winrm_plain_basic": True}})
        app.api("POST", "/api/hosts", {"host": {"name": "LabW11-Basic-HTTPS", "kind": "winrm", "host": WIN, "user": USER, "auth": "basic",
                                                "winrm_ssl": True, "winrm_cert_validation": False}})
        print("server Basic on (AllowUnencrypted still off):")
        win_ps("Set-Item WSMan:\\localhost\\Service\\Auth\\Basic $true")
        print("  HTTPS:", await try_host(app, "LabW11-Basic-HTTPS"))
        print("  HTTP :", await try_host(app, "LabW11-Basic-HTTP"))
        print("server Basic on, AllowUnencrypted on:")
        win_ps("Set-Item WSMan:\\localhost\\Service\\AllowUnencrypted $true")
        print("  HTTP :", await try_host(app, "LabW11-Basic-HTTP"))
    finally:
        print(win_ps("Set-Item WSMan:\\localhost\\Service\\AllowUnencrypted $false; Set-Item WSMan:\\localhost\\Service\\Auth\\Basic $false; "
                     "winrm get winrm/config/service | Select-String 'AllowUnencrypted|Basic'"))
        listener.cancel()
        app.quit()

asyncio.run(main())
