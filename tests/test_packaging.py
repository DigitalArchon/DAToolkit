"""The AppImage build's scripts (packaging/): what they take from the machine they run on, and
what they let in. Neither script is run whole here (one builds with Podman, the other runs inside
its container), but the parts of them that read the machine or fetch are. From DA Assistant's
tests/test_appimage_packaging.py and test_build_scripts.py (Nov 2026)."""

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging" / "appimage"
WRAPPER = (ROOT / "packaging" / "build-appimage.sh").read_text()
BUILD = (PACKAGING / "build.sh").read_text()
APPRUN = (PACKAGING / "AppRun").read_text()


def _function(script: str, name: str) -> str:
    found = re.search(rf"^{name}\(\) \{{.*?^\}}\n", script, re.S | re.M)
    assert found, f"{name} is not where this test looks for it"
    return found.group(0)


# ---------------------------------------------------------------- the source, as committed

def test_the_commit_is_built_as_committed_whatever_gits_settings(tmp_path):
    """A verifier's core.autocrlf, a smudge filter their attributes name: git archive hands each
    file through them, so the same commit made a different AppImage. The wrapper reads the files
    from git's objects instead, and sets their modes itself whatever the umask."""
    if not shutil.which("git"):
        pytest.skip("no git")
    assert 'EPOCH="$(git log -1 --format=%ct HEAD)"' in WRAPPER
    assert not re.search(r"(?m)^[^#\n]*git archive", WRAPPER)              # (its comment says why not)
    export = _function(WRAPPER, "export_head")
    (tmp_path / "attributes").write_text("*.txt filter=pear\n")
    (tmp_path / "gitconfig").write_text(f"[core]\n\tautocrlf = true\n\tattributesFile = {tmp_path}/attributes\n"
                                        "[filter \"pear\"]\n\tsmudge = sed s/apple/pear/\n\tclean = cat\n"
                                        "[user]\n\tname = T\n\temail = t@example.org\n")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig")}
    repo = tmp_path / "repo"
    committed = {"notes.txt": b"apple\nbanana\n", "tools/run.sh": b"#!/bin/sh\necho a\n"}

    def git(*args, cwd=repo):
        return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True).stdout

    git("init", "-q", str(repo), cwd=tmp_path)
    for name, data in committed.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_bytes(data)
    os.chmod(repo / "tools/run.sh", 0o755)
    git("add", "-A")
    git("commit", "-q", "-m", "files")

    def unpack(tar: Path):
        out = tmp_path / f"out-{tar.parent.name}-{tar.name}"
        out.mkdir()
        subprocess.run(["tar", "-xf", str(tar), "-C", str(out)], check=True)
        return ({n: (out / n).read_bytes() for n in committed},
                {n: os.stat(out / n).st_mode & 0o777 for n in committed})

    (tmp_path / "archive.tar").write_bytes(git("archive", "--format=tar", "HEAD"))
    assert unpack(tmp_path / "archive.tar")[0] != committed            # what git archive would have built

    work = tmp_path / "work"
    work.mkdir()
    run_export = ["bash", "-c", f"set -euo pipefail\numask 077\nWORK={shlex.quote(str(work))}\n"
                                f'EPOCH="$(git log -1 --format=%ct HEAD)"\n{export}\nexport_head']
    subprocess.run(run_export, cwd=repo, env=env, check=True, capture_output=True)
    files, modes = unpack(work / "src.tar")
    assert files == committed and modes == {"notes.txt": 0o644, "tools/run.sh": 0o755}

    os.symlink("notes.txt", repo / "link")
    git("add", "-A")
    git("commit", "-q", "-m", "a link")
    shutil.rmtree(work)
    work.mkdir()
    refused = subprocess.run(run_export, cwd=repo, env=env, capture_output=True, text=True)
    assert refused.returncode != 0 and "link is a blob of mode 120000" in refused.stderr


# ---------------------------------------------------------------- the host's CA bundle, for the builder

def _lookup(*candidates):
    return subprocess.run(["bash", "-c", _function(WRAPPER, "host_ca_bundle") + 'host_ca_bundle "$@"',
                           "bash", *candidates], capture_output=True, text=True, timeout=30)


def test_apprun_and_the_build_look_for_the_hosts_ca_bundle_in_the_same_five_places():
    """AppRun looked in three: on openSUSE (/etc/ssl/ca-bundle.pem), or a host with only
    /etc/pki/tls/cert.pem, httpx and requests went to the bundled certifi, not the host's store, so
    the host's distrust decisions didn't hold."""
    listed = re.search(r"^CA_BUNDLES=\((.*?)\)", WRAPPER, re.S | re.M).group(1).split()
    looked = re.search(r"for bundle in(.*?); do", APPRUN, re.S).group(1)
    apprun = re.findall(r"/etc/[^\s\\]+", looked)
    assert apprun == listed == ["/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt",
                                "/etc/ssl/ca-bundle.pem", "/etc/pki/tls/cert.pem", "/etc/ssl/cert.pem"]
    assert subprocess.run(["sh", "-n", str(PACKAGING / "AppRun")]).returncode == 0


def test_apprun_takes_the_first_bundle_there_and_leaves_ones_chosen_alone(tmp_path):
    """AppRun's lookup, run with its paths moved under a folder of the test's own."""
    root = tmp_path / "root"
    (root / "etc/ssl").mkdir(parents=True)
    (root / "etc/ssl/ca-bundle.pem").write_text("pem")
    script = APPRUN.replace("/etc/", f"{root}/etc/").replace('exec "$HERE/usr/python/bin/python3"', 'exit 0 #')
    script += '\necho "$SSL_CERT_FILE|$REQUESTS_CA_BUNDLE"\n'
    run = tmp_path / "AppRun"
    run.write_text(script.replace("exit 0 #", ": #"))
    env = {"PATH": os.environ.get("PATH", "")}
    out = subprocess.run(["sh", str(run)], env=env, capture_output=True, text=True).stdout.strip().splitlines()[-1]
    assert out == f"{root}/etc/ssl/ca-bundle.pem|{root}/etc/ssl/ca-bundle.pem"
    chosen = subprocess.run(["sh", str(run)], env={**env, "SSL_CERT_FILE": "/mine.pem"}, capture_output=True,
                            text=True).stdout.strip().splitlines()[-1]
    assert chosen == "/mine.pem|/mine.pem"


def test_the_first_bundle_there_is_used(tmp_path):
    bundle = tmp_path / "ca-bundle.pem"
    bundle.write_text("certificates")
    done = _lookup(str(tmp_path / "missing.crt"), str(bundle), str(tmp_path / "also-missing.pem"))
    assert (done.returncode, done.stdout) == (0, f"{bundle}\n")


def test_none_there_is_said_plainly():
    """It was left naming the last place looked, and install failed: "cannot stat
    '/etc/ssl/cert.pem'" (openSUSE keeps it at /etc/ssl/ca-bundle.pem)."""
    done = _lookup("/nonexistent/one.crt", "/nonexistent/two.pem")
    assert done.returncode == 1 and done.stdout == ""
    assert "No CA bundle on this machine" in done.stderr and "/nonexistent/one.crt /nonexistent/two.pem" in done.stderr
    assert "ca-certificates" in done.stderr


def test_it_is_looked_for_only_to_make_the_builder_and_goes_in_as_a_copy():
    made = WRAPPER.index('if ! podman image exists "$TAG"; then')
    assert WRAPPER.index('ca="$(host_ca_bundle "${CA_BUNDLES[@]}")"') > made
    assert not re.search(r"(?m)^for ca in ", WRAPPER)
    # no mount that needs relabelling: rootless Podman on an SELinux host can't relabel a file root owns
    assert not re.search(r"podman build[^\n]*-v ", WRAPPER)
    assert 'install -m 0644 "$ca" "$WORK/context/hostca.crt"' in WRAPPER
    containerfile = (PACKAGING / "Containerfile").read_text()
    assert "COPY hostca.crt /hostca.crt" in containerfile and re.search(r"rm [^\n]*/hostca\.crt", containerfile)


def test_the_results_go_where_dist_says():
    assert 'DIST="$(mkdir -p "${DIST:-dist}" && cd "${DIST:-dist}" && pwd)"' in WRAPPER
    assert 'run "$DIST" build' in WRAPPER and '"$PWD/dist"' not in WRAPPER


# ---------------------------------------------------------------- downloads and certificates

def _commands(text: str) -> list[str]:
    """A script's commands, one to a line: a command continued over several lines is one."""
    return re.sub(r"\\\n\s*", " ", text).splitlines()


def test_every_download_is_https_only():
    """A hash proves what arrived, not that nobody saw it asked for: no download may fall back to
    http, through a redirect either."""
    curls = []
    for path in sorted((ROOT / "packaging").rglob("*.sh")) + sorted((ROOT / "tools").rglob("*.sh")):
        for line in _commands(path.read_text()):
            if re.search(r"\bcurl\s+-", line) and not line.lstrip().startswith("#"):
                curls.append(path.name)
                assert "--proto '=https'" in line and "--tlsv1.2" in line, f"{path.name}: {line.strip()}"
    assert "build.sh" in curls
    # the source archives are fetched by Python, whose urllib follows a redirect to http
    assert "class HttpsOnly(urllib.request.HTTPRedirectHandler)" in BUILD
    assert "urllib.request.urlopen(" not in BUILD


def test_no_certificate_check_is_switched_off():
    """Not in the build: no curl -k, no pip trusted host. Nor in the app, but where a certificate is
    pinned instead of checked against a CA: an RDP server's (rdpcert.py, trust on first use) and the
    companion's own, to which client_context trusts exactly that one certificate."""
    for path in sorted((ROOT / "packaging").rglob("*")):
        if path.is_file() and path.suffix in ("", ".sh", ".py", ".in") and not path.name.endswith(".png"):
            text = path.read_text(errors="replace")
            for forbidden in ("--insecure", "--trusted-host", "PIP_TRUSTED_HOST", "verify=False", "CERT_NONE",
                              "_create_unverified_context"):
                assert forbidden not in text, f"{path.name}: {forbidden}"
            assert not re.search(r"\bcurl\b[^\n]*\s-k\b", text), path.name
    allowed = {"sessions/rdpcert.py": 4, "companion.py": 1}
    found = {}
    for path in sorted((ROOT / "src" / "datoolkit").rglob("*.py")):
        text = path.read_text()
        hits = sum(text.count(f) for f in ("verify=False", "verify = False", "CERT_NONE", "check_hostname = False",
                                           "_create_unverified_context", "disable_warnings"))
        if hits:
            found[str(path.relative_to(ROOT / "src" / "datoolkit"))] = hits
    assert found == allowed
