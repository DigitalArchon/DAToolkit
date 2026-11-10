"""Certificate prompts against what Windows reports, and a changed RDP certificate.

CHANGES THE VM: it deletes the Remote Desktop certificate and restarts TermService, so Windows
makes a new one (and any RDP session there is cut)."""

import asyncio, re
from bidi import Firefox
from lab import PASS, SCRATCH, USER, WIN, App
from rdp_test import win_ps, open_rdp

def win_thumbs():
    out = win_ps("(Get-ChildItem 'Cert:\\LocalMachine\\Remote Desktop' | Select-Object -First 1).Thumbprint; "
                 "(Get-ChildItem 'Cert:\\LocalMachine\\Remote Desktop' | Select-Object -First 1).GetCertHashString('SHA256')")
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    return lines[-2], lines[-1]

async def main():
    app = App("cert", port=48605)
    app.start()
    listener = asyncio.create_task(app.listen())
    app.rules = [(r"First connection to", "yes"), (r"RDP password for", PASS), (r"Password for .*\(WinRM\)", PASS)]
    try:
        await asyncio.sleep(0.5)
        app.api("POST", "/api/settings", {"companion_port": 48606})
        app.api("POST", "/api/case", {"name": "cert", "sensitivity": "open"})
        app.api("POST", "/api/hosts", {"host": {"name": "LabW11-RDP", "kind": "rdp", "host": WIN, "user": USER, "rdp_security": "any"}})
        app.api("POST", "/api/hosts", {"host": {"name": "LabW11-RDP-BadPass", "kind": "rdp", "host": WIN, "user": USER, "rdp_security": "nla"}})
        app.api("POST", "/api/hosts", {"host": {"name": "LabW11-HTTPS-Pin", "kind": "winrm", "host": WIN, "user": USER, "auth": "ntlm",
                                                "winrm_ssl": True, "winrm_cert_validation": False}})
        # WinRM listener: the prompt against `winrm enumerate`
        r = await app.aapi("POST", "/api/sessions", {"kind": "winrm", "host": "LabW11-HTTPS-Pin"})
        p = [x["text"] for x in app.prompts if "5986" in x["text"]][-1]
        enum = win_ps("winrm enumerate winrm/config/listener | Select-String CertificateThumbprint")
        wt = re.search(r"CertificateThumbprint = ([0-9A-F]{40})", enum).group(1)
        print("WinRM prompt:\n   " + p.replace("\n", "\n   "))
        print(("PASS" if wt in p else "FAIL") + f" WinRM prompt shows the thumbprint `winrm enumerate` shows ({wt})")
        app.api("DELETE", f"/api/sessions/{r['id']}")

        sha1, sha256 = win_thumbs()
        r = await app.aapi("POST", "/api/sessions", {"kind": "rdp", "host": "LabW11-RDP"})
        p = [x["text"] for x in app.prompts if "3389" in x["text"]][-1]
        print("RDP prompt:\n   " + p.replace("\n", "\n   "))
        print(("PASS" if sha1 in p and sha256 == p.split("SHA-256: ")[1].split("\n")[0].replace(":", "") else "FAIL")
              + f" RDP prompt matches the Cert: drive ({sha1}; SHA-256 {sha256[:16]}…)")
        app.api("DELETE", f"/api/sessions/{r['id']}")

        print("regenerating the Remote Desktop certificate on Windows...")
        print(win_ps("Remove-Item 'Cert:\\LocalMachine\\Remote Desktop\\*'; Restart-Service TermService -Force; Start-Sleep 5; "
                     "(Get-ChildItem 'Cert:\\LocalMachine\\Remote Desktop').Thumbprint").strip()[-60:])
        sha1b, _ = win_thumbs()
        r = await app.aapi("POST", "/api/sessions", {"kind": "rdp", "host": "LabW11-RDP"})
        print(("PASS" if r.get("_error") and "CHANGED" in r["detail"] else "FAIL") + " changed certificate refused:\n   " + str(r.get("detail"))[:600])
        f = app.api("POST", "/api/hosts/LabW11-RDP/forget-pin")
        n = len(app.prompts)
        async with Firefox(SCRATCH / "ffprof-cert", port=48607) as ff:
            await ff.viewport(1600, 1000)
            await ff.goto(f"{app.base}/#t={app.token}")
            await ff.wait_js("return !!(S.state && S.state.case)", 20)
            r, st = await open_rdp(app, ff, "LabW11-RDP")
            p = [x["text"] for x in app.prompts[n:] if "3389" in x["text"]]
            print(("PASS" if f.get("removed") and p and sha1b in p[0] and st and st["state"] == 3 else "FAIL")
                  + f" after Forget pin: asked again with the new thumbprint ({sha1b}), connected: {st}")
            app.api("DELETE", f"/api/sessions/{r['id']}")
            await asyncio.sleep(2)
            app.rules.insert(0, (r"RDP password for", "wrong-" + PASS[::-1]))
            r, st = await open_rdp(app, ff, "LabW11-RDP-BadPass", timeout=45)
            await asyncio.sleep(14)          # past the 12 s toast
            status = await ff.js(f"return S.rdps[{r['id']!r}].status.textContent")
            print(("PASS" if "Authentication failure" in status else "FAIL") + f" status bar after the toast is gone: {status!r}")
            app.api("DELETE", f"/api/sessions/{r['id']}")
    finally:
        listener.cancel()
        app.quit()

asyncio.run(main())
