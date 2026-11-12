"""tools_cache.transfer_command: a tool's file name is data in every command made for it, and the
check after an scp compares the hash itself."""

import hashlib
import shlex
import shutil
import subprocess

import pytest

from datoolkit import tools_cache
from datoolkit.config import Host

WEB = Host("web01", "ssh", "web01.lan", user="bob")
ODD = ["My Tool (v2).bin", "it's.bin", "price$(touch pwned).bin", "tick`touch pwned`.bin", 'say "hi".bin', "a;b&c|d.bin",
       "-rf.bin", "‘curly’.bin", "[x].bin", "100%.bin"]


def _tool(tmp_path, name, os_="linux"):
    src = tmp_path / "src" / name
    src.parent.mkdir(exist_ok=True)
    src.write_bytes(b"tool bytes of " + name.encode())
    return tools_cache.add(src, "T", os_, root=tmp_path / "tools")


def test_a_plain_name_is_copied_as_before_and_checked_by_sha256sum(tmp_path):
    t = _tool(tmp_path, "probe-1.2_x64+a.bin")
    spec = tools_cache.transfer_command(t, WEB, "ssh", tmp_path / "tools")
    assert spec["command"] == f"scp -- {tmp_path}/tools/probe-1.2_x64+a.bin 'bob@web01.lan:~/probe-1.2_x64+a.bin'"
    assert "SFTP" not in spec["purpose"]
    assert spec["verify"] == f"printf '%s  %s\\n' {t.sha256} \"$HOME\"/probe-1.2_x64+a.bin | sha256sum -c -"


@pytest.mark.parametrize("name", ODD)
def test_the_sh_check_takes_any_file_name_as_data_and_says_ok_or_failed(tmp_path, name):
    """Run for real in sh, with HOME a scratch folder: the copy is found by its odd name, compared, and
    nothing in the name is carried out."""
    if not shutil.which("sha256sum"):
        pytest.skip("no sha256sum here")
    t = _tool(tmp_path, name)
    spec = tools_cache.transfer_command(t, WEB, "ssh", tmp_path / "tools")
    home = tmp_path / "home"
    home.mkdir()
    (home / name).write_bytes((tmp_path / "tools" / name).read_bytes())          # as scp would have left it
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    done = subprocess.run(["sh", "-c", spec["verify"]], cwd=home, env=env, capture_output=True, text=True, timeout=20)
    assert done.returncode == 0 and done.stdout.strip().endswith(": OK"), done
    (home / name).write_bytes(b"something else")
    done = subprocess.run(["sh", "-c", spec["verify"]], cwd=home, env=env, capture_output=True, text=True, timeout=20)
    assert done.returncode != 0 and "FAILED" in done.stdout, done
    assert sorted(p.name for p in home.iterdir()) == [name]                       # no `pwned` made on the way


@pytest.mark.parametrize("name", ODD)
def test_scp_gets_an_odd_name_as_one_argument_and_over_sftp(tmp_path, name):
    t = _tool(tmp_path, name)
    spec = tools_cache.transfer_command(t, WEB, "ssh", tmp_path / "tools")
    argv = shlex.split(spec["command"])                                          # as the local shell reads it
    assert argv[:2] == ["scp", "-s"] and argv[-3:] == ["--", f"{tmp_path}/tools/{name}", f"bob@web01.lan:~/{name}"]
    assert "over SFTP" in spec["purpose"]


def _ps_literals(command: str) -> list[str]:
    """The single-quoted strings of a PowerShell command, as PowerShell reads them: ended by any of
    its quote characters, a doubled one standing for itself."""
    quotes, out, i = tools_cache._PS_QUOTES, [], 0
    while i < len(command):
        if command[i] not in quotes:
            i += 1
            continue
        i, text = i + 1, ""
        while i < len(command):
            if command[i] in quotes:
                if i + 1 < len(command) and command[i + 1] in quotes:
                    text, i = text + command[i], i + 2
                    continue
                break
            text, i = text + command[i], i + 1
        out.append(text)
        i += 1
    return out


@pytest.mark.parametrize("name", ODD)
def test_the_winrm_command_holds_the_name_in_a_single_quoted_string(tmp_path, name):
    t = _tool(tmp_path, name, "windows")
    cmd = tools_cache.transfer_command(t, None, "winrm", tmp_path / "tools")["command"]
    assert cmd.startswith("$f = Join-Path $env:TEMP '")
    literals = _ps_literals(cmd)
    assert literals[0] == name and literals[-1] == t.sha256 and len(literals) == 3      # the name, the payload, the hash
    assert "Get-FileHash -LiteralPath $f" in cmd and '"' not in cmd.replace(name, "")
    assert hashlib.sha256((tmp_path / "tools" / name).read_bytes()).hexdigest() == t.sha256


def test_the_windows_check_over_ssh_compares_the_hash_and_quotes_the_name(tmp_path):
    t = _tool(tmp_path, "My Tool's (v2) [x].exe", "windows")
    check = tools_cache.transfer_command(t, WEB, "ssh", tmp_path / "tools")["verify"]
    assert check.startswith('powershell -NoProfile -Command "if ((Get-FileHash -LiteralPath ') and check.endswith('"')
    assert check.count('"') == 2                                                  # one string for whichever shell reads it
    literals = _ps_literals(check[check.index('"') + 1:-1])
    assert literals == ["~\\My Tool's (v2) [x].exe", t.sha256, tools_cache.OK, tools_cache.MISMATCH]


@pytest.mark.parametrize("name", ['say "hi".exe', "price$(calc).exe", "tick`calc`.exe", "100%TEMP%.exe", "wow!.exe"])
def test_a_name_a_windows_shell_over_ssh_would_read_into_is_refused(tmp_path, name):
    t = _tool(tmp_path, name, "windows")
    with pytest.raises(ValueError, match="rename the file"):
        tools_cache.transfer_command(t, WEB, "ssh", tmp_path / "tools")
    assert tools_cache.transfer_command(t, None, "winrm", tmp_path / "tools")["command"]      # WinRM takes it as data


def test_a_name_with_a_line_break_is_refused(tmp_path):
    t = _tool(tmp_path, "two\nlines.bin")
    for host, kind in ((WEB, "ssh"), (None, "winrm")):
        with pytest.raises(ValueError, match="control character"):
            tools_cache.transfer_command(t, host, kind, tmp_path / "tools")
