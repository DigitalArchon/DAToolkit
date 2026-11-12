"""WinRM with Kerberos against the lab's Samba AD domain (LAB.TEST on dc1, win11 joined): see
LAB.md. Needs a Python with the kerberos extra (WINLAB_PYTHON, or the AppImage's) and this
computer able to find the KDC and resolve win11test.lab.test.

    WINLAB_KRB5_CONF=...  a krb5.conf naming the KDC, for a computer not on the domain's DNS
    WINLAB_HOSTS="win11test.lab.test=192.168.1.214"   names to map inside the instance, where
                          the system resolver can't be pointed at the DC (no root)"""

import asyncio
import os
import re
import shlex
import sys

from lab import CREDS, PASS, SCRATCH, App, Term

FQDN = "win11test.lab.test"
PROMPT = r"\[win11test\.lab\.test\]: PS [^\n]*> $"
results = []
creds = {k: shlex.split(v)[0] for k, v in (line.split("=", 1) for line in
         (CREDS / "dc-credentials").read_text().splitlines() if "=" in line and not line.startswith("#"))}
DUSER, DPASS = creds["DC_USER"], creds["DC_USER_PASS"]


def check(ok, what, detail=""):
    results.append((bool(ok), what, detail))
    print(("PASS " if ok else "FAIL ") + what + (f"\n     {detail}" if detail and not ok else ""), flush=True)


def instance_env() -> dict:
    env = {"KRB5CCNAME": "MEMORY:"}           # never the user's own ticket cache
    if os.environ.get("WINLAB_KRB5_CONF"):
        env["KRB5_CONFIG"] = os.environ["WINLAB_KRB5_CONF"]
    if os.environ.get("WINLAB_HOSTS"):
        site = SCRATCH / "sitecustomize"
        site.mkdir(exist_ok=True)
        names = dict(x.split("=", 1) for x in os.environ["WINLAB_HOSTS"].split(","))
        (site / "sitecustomize.py").write_text(
            "import socket\n_gai = socket.getaddrinfo\n"
            f"_NAMES = {names!r}\n"
            "socket.getaddrinfo = lambda host, *a, **k: _gai(_NAMES.get(str(host).lower(), host), *a, **k)\n")
        env["PYTHONPATH"] = str(site) + (":" + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")
    return env


async def console(app, name):
    r = await app.aapi("POST", "/api/sessions", {"kind": "winrm", "host": name})
    if "_error" in r:
        return r, None, r.get("detail", "")
    t = Term(app, r["id"])
    await t.__aenter__()
    out = await t.wait(PROMPT + r"|Connection failed[^\n]*\n", 0, 60)
    return r, t, out or t.text()


async def main():
    app = App("kerberos", port=48610, extra_env=instance_env())
    app.start()
    listener = asyncio.create_task(app.listen())
    app.rules = [(r"First connection to .*:5986", "yes"), (rf"Password for {re.escape(DUSER)}@.*\(WinRM\)", DPASS),
                 (r"Password for test@.*\(WinRM\)", PASS), (r"Password for bad@.*", "x")]
    try:
        await asyncio.sleep(0.5)
        app.api("POST", "/api/settings", {"companion_port": 48611})
        app.api("POST", "/api/case", {"name": "Kerberos lab test", "sensitivity": "open"})
        base = {"kind": "winrm", "host": FQDN, "winrm_ssl": True, "winrm_cert_validation": False}
        refused = {
            "DOMAIN\\user": {**base, "name": "LabK-Netbios", "user": f"LAB\\{DUSER}", "auth": "kerberos"},
            "an IP address": {**base, "name": "LabK-IP", "host": "192.168.1.214", "user": f"{DUSER}@LAB.TEST", "auth": "kerberos"},
            "plain HTTP to a name": {**base, "name": "LabK-HTTP", "user": f"{DUSER}@LAB.TEST", "auth": "kerberos", "winrm_ssl": False},
        }
        for what, h in refused.items():
            r = app.api("POST", "/api/hosts", {"host": h})
            check(r.get("_error"), f"Kerberos host with {what} is refused when saved", r)
            print("     ", str(r.get("detail"))[:200])
        for h in ({**base, "name": "LabK-Kerberos", "user": f"{DUSER}@lab.test", "auth": "kerberos"},
                  {**base, "name": "LabK-Negotiate", "user": f"{DUSER}@LAB.TEST", "auth": "negotiate"},
                  {**base, "name": "LabK-BadPass", "user": "bad@LAB.TEST", "auth": "kerberos"},
                  {"name": "LabK-Local", "kind": "winrm", "host": "192.168.1.214", "user": "test", "auth": "negotiate",
                   "winrm_ssl": False}):
            check(app.api("POST", "/api/hosts", {"host": h}).get("ok"), f"host {h['name']} saved")

        for name, expect in (("LabK-Kerberos", "Kerberos"), ("LabK-Negotiate", "Kerberos")):
            r, t, out = await console(app, name)
            ok = t and re.search(PROMPT, out)
            check(ok, f"{name} connects (user given as {DUSER}@lab.test)" if name == "LabK-Kerberos" else f"{name} connects",
                  out[-400:])
            if ok:
                m = await t.paste("$i = [Security.Principal.WindowsIdentity]::GetCurrent(); \"$($i.Name) via $($i.AuthenticationType)\"")
                o = await t.wait(PROMPT, m, 30)
                got = (o or "").split("\n")[1] if o else ""
                check(got.strip() == f"LAB\\{DUSER} via {expect}", f"{name}: signed in as LAB\\{DUSER} via {expect}", repr(got))
                await t.send("exit\r")
                await asyncio.sleep(1)
            if t:
                await t.__aexit__()
        r, t, out = await console(app, "LabK-BadPass")
        print("      bad user:", repr(out.strip()[-300:]))
        check(t and "Kerberos:" in out and "Traceback" not in out, "an unknown user is a Kerberos sentence", out[-300:])
        if t:
            await t.__aexit__()
        # a workgroup-style account by address: Negotiate still falls back to NTLM
        r, t, out = await console(app, "LabK-Local")
        ok = t and re.search(r"\[192\.168\.1\.214\]: PS [^\n]*> $", out)
        check(ok, "Negotiate with a local account by IP still connects (NTLM)", out[-300:])
        if t:
            await t.send("exit\r")
            await asyncio.sleep(1)
            await t.__aexit__()
    finally:
        listener.cancel()
        app.quit()
    print(f"\n{sum(ok for ok, *_ in results)}/{len(results)} passed")
    sys.exit(0 if all(ok for ok, *_ in results) else 1)


asyncio.run(main())
