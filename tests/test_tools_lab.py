"""The developer tools under tools/: that they run from wherever the repository is checked out,
and that what the Windows lab scripts expect of the app is what the app does. The lab scripts
themselves need the lab's Windows VM and are not run here; their harness is loaded with a
stand-in for the lab's credentials file, and starts nothing."""

import os
import re
import subprocess
import sys
from pathlib import Path

from datoolkit.config import Host
from datoolkit.sessions import winrm

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / "tools" / "windows-lab"


def test_no_tool_names_the_folder_it_was_written_in():
    """lab.py started the instance from /home/<its author>/Documents/DAToolkit, and the model
    evaluation's brief sent the host agent there."""
    # the files the repository holds: a run's own notes and results beside them (git-ignored) may
    # name where they were made
    listed = subprocess.run(["git", "ls-files", "tools", "packaging"], cwd=ROOT, capture_output=True, text=True)
    paths = ([ROOT / line for line in listed.stdout.splitlines()] if listed.returncode == 0 and listed.stdout.strip()
             else [p for folder in ("tools", "packaging") for p in (ROOT / folder).rglob("*")])
    for path in sorted(paths):
        if path.is_file() and path.suffix in (".py", ".sh", ".md", ".toml", ".in", ""):
            found = re.search(r"/home/\w+|/Users/\w+", path.read_text(errors="replace"))
            assert not found, f"{path.relative_to(ROOT)}: {found.group(0)}"
    assert "repository root" in (ROOT / "tools" / "model-eval" / "HOST_BRIEF.md").read_text()


def test_the_lab_harness_runs_the_checkout_it_is_in(tmp_path):
    (tmp_path / "creds").mkdir()
    (tmp_path / "creds" / "vm-credentials").write_text("VM_USER=test\nVM_PASS='a stand-in'\n")
    script = ("import os, lab\n"
              "app = lab.App('paths')\n"
              "print(lab.REPO)\nprint(app.env()['PYTHONPATH'])\nprint(app.root)\n"
              "os.environ['WINLAB_APP'] = '/nonexistent/AppRun'\n"
              "app.start()\n")
    done = subprocess.run([sys.executable, "-c", script], cwd=LAB, capture_output=True, text=True, timeout=60,
                          env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "PYTHONPATH": "/elsewhere",
                               "WINLAB_CREDS": str(tmp_path / "creds"), "WINLAB_WORK": str(tmp_path / "work")})
    repo, path, root = done.stdout.splitlines()
    assert repo == str(ROOT)
    assert path == f"{ROOT / 'src'}{os.pathsep}/elsewhere"         # this checkout's source, before any other
    assert root == str(tmp_path / "work" / "inst" / "paths")
    # no Python or AppRun to start the instance with is said, not a FileNotFoundError's traceback
    assert done.returncode == 1 and "Traceback" not in done.stderr
    assert "/nonexistent/AppRun is not there" in done.stderr and "WINLAB_PYTHON" in done.stderr


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


def test_a_lab_script_that_counts_its_checks_ends_by_them(tmp_path):
    """console_test.py, queue_test.py and rdp_test.py printed "17/19 passed" and ended with status
    0; queue_test.py without its argument was an IndexError, and its usage named another file."""
    for name in ("winrm_test.py", "console_test.py", "queue_test.py", "rdp_test.py", "kerberos_test.py"):
        script = (LAB / name).read_text()
        assert 'print(("PASS " if ok else "FAIL ")' in script, name
        assert "sys.exit(0 if all(ok for ok, *_ in results) else 1)" in script, name
    # asked for its argument before the lab's credentials are (there are none here)
    done = subprocess.run([sys.executable, str(LAB / "queue_test.py")], cwd=tmp_path, capture_output=True, text=True,
                          timeout=60, env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
                                           "WINLAB_CREDS": str(tmp_path), "WINLAB_WORK": str(tmp_path / "work")})
    assert done.returncode == 1 and "queue_test.py winrm|ssh" in done.stderr and "Traceback" not in done.stderr
    assert not (tmp_path / "work").exists()
    # the domain user's password is offered for the domain user the lab's file names, whoever that is
    assert r'rf"Password for {re.escape(DUSER)}@.*\(WinRM\)"' in (LAB / "kerberos_test.py").read_text()
