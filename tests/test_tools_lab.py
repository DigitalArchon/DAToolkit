"""The developer tools under tools/: that what the Windows lab scripts expect of the app is what
the app does. The lab scripts themselves need the lab's Windows VM and are not run here."""

import re
from pathlib import Path

from datoolkit.config import Host
from datoolkit.sessions import winrm

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / "tools" / "windows-lab"


def test_the_winrm_lab_script_expects_what_the_app_says_of_kerberos_to_an_address():
    """winrm_test.py went on saving a Kerberos host by IP address after the app began refusing
    one, and two of its steps failed for that alone."""
    script = (LAB / "winrm_test.py").read_text()
    said = winrm.problem(Host(name="LabW11-Kerb", kind="winrm", host="192.168.1.214", user="test",
                              auth="kerberos", winrm_ssl=False))
    block = script[script.index('host("LabW11-Kerb", auth="kerberos")'):]
    block = block[:block.index("\n        for h in (")]
    expected = re.findall(r'"([^"]+)" in str\(r\.get\("detail"\)\)', block)
    assert len(expected) == 3 and 'r.get("_error") == 400' in block
    for phrase in expected:
        assert phrase in f"Host not saved. {said}", phrase
    # it is refused once, with its reason, and no later step saves it or opens a console on it
    assert script.count("LabW11-Kerb") == 2 and 'open_console(app, "LabW11-Kerb"' not in script
    # what the Kerberos script saves, by name and user@REALM over HTTPS, the app takes
    assert winrm.problem(Host(name="LabK-Kerberos", kind="winrm", host="win11test.lab.test", user="dat-tech@lab.test",
                              auth="kerberos", winrm_ssl=True, winrm_cert_validation=False)) is None
    for refused in (dict(user="LAB\\dat-tech"), dict(host="192.168.1.214"), dict(winrm_ssl=False)):
        fields = {"name": "LabK", "kind": "winrm", "host": "win11test.lab.test", "user": "dat-tech@LAB.TEST",
                  "auth": "kerberos", "winrm_ssl": True, **refused}
        assert winrm.problem(Host(**fields)), refused
