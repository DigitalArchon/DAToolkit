# Windows lab tests

Live tests of DA Toolkit's Windows paths against the lab's Windows 11 VM (`win11` on p330-2,
192.168.1.214; see `~/.local/share/testlab/LAB.md` for its WinRM, RDP and SSH setup). Each
script starts a throwaway DA Toolkit from this checkout (own XDG dirs, ports 4859x–4860x, no AI
providers, host names `LabW11-*` that match nothing in the real keyring) and drives it the way
the page does: prompts answered over `/ws/events`, terminals over `/ws/term` (bracketed paste,
then Enter), RDP through the real page in headless Firefox (WebDriver BiDi).

| Script | What it checks |
|---|---|
| `winrm_test.py` | save-time rules; Negotiate, NTLM, Kerberos, Basic, wrong password; the console (streams, non-ASCII, Ctrl+C, Read-Host); HTTPS against the CA store and pinned |
| `console_test.py` | the console's interactive behaviour: output as it arrives, Ctrl+C over HTTP and HTTPS keeping output, Read-Host, a two-line paste |
| `queue_test.py winrm\|ssh [ids]` | every Windows recipe step run through the queue, the transcript capture, a watch, tool transfers (1 KB to 3.9 MB) |
| `rdp_test.py` | RDP through guacd: certificate prompt, desktop drawn, Win+R, exact typing, Run on a queued item, clipboard both ways, Ctrl+Alt+Del, live resize, reconnect, linking, security modes, wrong password |
| `cert_test.py` | prompts against `winrm enumerate` and the Cert: drive; a regenerated RDP certificate refused, then pinned again (**changes the VM**) |
| `basic_test.py` | Basic over HTTPS and HTTP (**turns Basic and AllowUnencrypted on, then off**) |

Run each under a memory cap, one at a time on the VM's RDP session:

    systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 ../../.venv/bin/python rdp_test.py

Needs: the VM running with WinRM, RDP and SSH as LAB.md describes; `~/.config/testlab/vm-credentials`;
the `p330-2-win11` ssh alias (for checks on the Windows side); guacd on 127.0.0.1:4822; Firefox.
Output (instances, screenshots, reports) goes to `~/.cache/datoolkit-winlab` (`WINLAB_WORK`).

After an RDP test the VM's `test` session is no longer on the console: give it back with `tscon`
(LAB.md), and close the PowerShell windows the test opened there.

Results of the first run (2026-11-10, Windows 11 Enterprise 26H2 build 26300, guacd 1.3):
everything passes on branch `windows-lab-fixes` except what the Windows side refuses by design
(TLS-only and legacy RDP against an NLA-only server, `nla-ext` with guacd 1.3, Kerberos without a
domain, the PowerShell 7 ping sweep on 5.1 before the recipes were fixed).
