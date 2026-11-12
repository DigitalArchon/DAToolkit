"""The AppImage build's scripts (packaging/): what they take from the machine they run on, and
what they let in. No build is run here (it takes Podman and the network, and build.sh runs inside
the container), but the parts of the scripts that read the machine or fetch are; the wrapper is
run whole against a Podman that only notes what it is asked. From DA Assistant's
tests/test_appimage_packaging.py and test_build_scripts.py (Nov 2026)."""

import base64
import csv
import hashlib
import io
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging" / "appimage"
WRAPPER = (ROOT / "packaging" / "build-appimage.sh").read_text()
BUILD = (PACKAGING / "build.sh").read_text()
APPRUN = (PACKAGING / "AppRun").read_text()
README = (ROOT / "packaging" / "README.md").read_text()


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


# ---------------------------------------------------------------- no build path in the image

def test_nothing_of_the_build_machine_gets_in():
    assert "-root-owned -no-xattrs" in BUILD and "PYTHONHASHSEED=0" in BUILD
    assert "--invalidation-mode unchecked-hash" in BUILD and '-ffile-prefix-map=$W/=' in BUILD
    assert "direct_url.json -delete" in BUILD and 'grep -rlaF "$W/AppDir" "$W/AppDir"' in BUILD


def test_bytecode_names_its_source_as_it_is_inside_the_image(tmp_path):
    """compileall as build.sh runs it, on a folder laid out as the AppDir is: each .pyc
    unchecked-hash, naming /usr/python/..., not the folder the build ran in."""
    import marshal
    import sys

    command = next(line for line in _commands(BUILD) if "-m compileall" in line)
    assert '"$PY" -B -m compileall' in command          # else the modules compileall itself imports go in dated
    lib = tmp_path / "AppDir" / "usr" / "python" / "lib" / "python3.12"
    (lib / "site-packages" / "datoolkit").mkdir(parents=True)
    for folder in (lib, lib / "site-packages" / "datoolkit"):
        (folder / "mod.py").write_text("def f():\n    return 1\n")
    subprocess.run(["bash", "-c", command], check=True,
                   env={"PATH": os.environ.get("PATH", ""), "PY": sys.executable, "W": str(tmp_path)})
    names = []
    for pyc in tmp_path.rglob("*.pyc"):
        data = pyc.read_bytes()
        assert int.from_bytes(data[4:8], "little") == 1, pyc        # unchecked-hash
        code = marshal.loads(data[16:])
        names += [code.co_filename, code.co_consts[0].co_filename]
    assert sorted(set(names)) == ["/usr/python/lib/python3.12/mod.py",
                                  "/usr/python/lib/python3.12/site-packages/datoolkit/mod.py"]


def test_console_scripts_that_name_the_build_python_are_left_out(tmp_path):
    start = BUILD.index('for script in "$W"/AppDir/usr/python/bin/*; do')
    loop = BUILD[start:BUILD.index("\ndone\n", start) + 6]
    bin_dir = tmp_path / "AppDir" / "usr" / "python" / "bin"
    bin_dir.mkdir(parents=True)
    scripts = {"datoolkit": f"#!{tmp_path}/AppDir/usr/python/bin/python3\nimport datoolkit\n",
               "uvicorn": f"#!{tmp_path}/AppDir/usr/python/bin/python3\nimport uvicorn\n",
               "pydoc3.12": "#!/bin/sh\n'''exec' \"$(dirname -- \"$0\")/python3.12\"\n",
               "python3.12": "\x7fELF\x02\x01\x01\0" + "\0" * 64}
    for name, text in scripts.items():
        (bin_dir / name).write_text(text, encoding="latin-1")
    os.symlink("python3.12", bin_dir / "python3")
    subprocess.run(["bash", "-c", f"set -euo pipefail\n{loop}"], check=True,
                   env={"PATH": os.environ.get("PATH", ""), "W": str(tmp_path)})
    assert sorted(os.listdir(bin_dir)) == ["pydoc3.12", "python3", "python3.12"]



# ---------------------------------------------------------------- what is installed, and from where

def test_nothing_is_installed_but_from_a_lock_with_its_hashes_or_built_here():
    """The lock run installed "pip==26.2.2" "pip-tools==7.6.1": versions, not files, and what they
    pull in (build, click, packaging, pyproject-hooks, setuptools, wheel) as it came on the day."""
    installs = 0
    for line in _commands(BUILD):
        command = line.split(" #", 1)[0].strip()
        if command.startswith("#") or not re.search(r'pip"? install ', command):
            continue
        installs += 1
        if re.search(r' -r "[^"]+\.lock"', command):
            assert "--require-hashes" in command, command
        else:
            assert re.search(r'pip"? install( -[-\w]+)* "\$W/wheels"/\S+\.whl$', command), command
    assert installs >= 5
    assert '--require-hashes --no-deps -r "$P/lock-requirements.lock"' in BUILD
    assert re.search(r"for name in [^;]*\block-requirements\b[^;]*; do", BUILD)


def test_every_locked_package_carries_a_hash_and_the_lock_tools_are_named_exactly():
    for name in ("requirements", "build-requirements", "sdists", "lock-requirements"):
        entries = re.findall(r"^([\w.-]+)==([^\s]+)((?:\s*\\\s*--hash=sha256:\w+)*)",
                             (PACKAGING / f"{name}.lock").read_text(), re.M)
        assert entries, name
        for package, version, hashes in entries:
            assert "--hash=sha256:" in hashes, f"{name}.lock: {package}=={version}"
    wanted = (PACKAGING / "lock-requirements.in").read_text()
    locked = (PACKAGING / "lock-requirements.lock").read_text()
    for pin in ("pip==26.2.2", "pip-tools==7.6.1"):
        assert re.search(rf"(?m)^{re.escape(pin)}$", wanted) and re.search(rf"(?m)^{re.escape(pin)} ", locked), pin


def test_the_lock_meets_every_floor_pyproject_sets():
    """A relock keeps what the lock already pins, so a floor raised for an advisory (certifi's
    root, brotli's out-of-bounds write, pyasn1's and dnspython's denials of service) is only as
    good as the relock that followed it. (DA Assistant's, 9b1d6b8.)"""
    import tomllib

    def version(text):
        return tuple(int(part) for part in re.findall(r"\d+", text))

    def key(name):
        return re.sub(r"[-_.]+", "-", name).lower()

    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    floors = [(found.group(1), found.group(2)) for dep in deps
              if (found := re.match(r"([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?>=([0-9.]+)", dep))]
    assert ("pyasn1", "0.6.5") in floors and ("dnspython", "2.9.0") in floors
    locked = {key(name): pin for name, pin in
              re.findall(r"(?m)^([\w.-]+)(?:\[[^\]]*\])?==(\S+) \\$", (PACKAGING / "requirements.lock").read_text())}
    for name, floor in floors:
        assert key(name) in locked, f"{name} is not in the AppImage lock"
        assert version(locked[key(name)]) >= version(floor), f"{name}=={locked[key(name)]}, under its floor {floor}"


# ---------------------------------------------------------------- the build's definition, as committed

# Stands in for Podman: the builder image "exists", and a run is only noted. A lock run leaves
# a lock nobody has committed yet in its /out.
_PODMAN = """#!/bin/sh
printf '%s\\n' "$*" >> "$PODMAN_LOG"
if [ "$1" = run ]; then
    for arg in "$@"; do
        case "$arg" in *:/out:Z) out="${arg%:/out:Z}" ;; esac
        mode="$arg"
    done
    if [ "$mode" = lock ]; then echo "pytest==9.9.9" > "$out/test-requirements.lock"; fi
fi
exit 0
"""


def _stub(folder: Path, name: str, script: str) -> None:
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(script if script.startswith("#!") else f"#!/bin/sh\n{script}\n")
    os.chmod(folder / name, 0o755)


def _git_env(tmp_path: Path, bin_dir: Path) -> dict:
    """git with no settings of this machine's, the stand-ins first on the path, and the build's
    cache and results under the test's own folder."""
    (tmp_path / "gitconfig").write_text("[user]\n\tname = T\n\temail = t@example.org\n")
    return {"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}", "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"), "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "DIST": str(tmp_path / "dist"), "PODMAN_LOG": str(tmp_path / "podman.log")}


def _commit_all(repo: Path, env: dict) -> None:
    for args in (("init", "-q"), ("add", "-A"), ("commit", "-q", "-m", "files")):
        subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


@pytest.fixture
def build_repo(tmp_path):
    """A repository with the wrapper and a build definition committed (the real pins and
    Containerfile), and the Podman above. Gives the repository, and a function that runs the
    wrapper there and returns how it ended and what Podman was asked."""
    if not shutil.which("git"):
        pytest.skip("no git")
    repo = tmp_path / "repo"
    (repo / "packaging" / "appimage").mkdir(parents=True)
    shutil.copy(ROOT / "packaging" / "build-appimage.sh", repo / "packaging")
    for name in ("pins.sh", "Containerfile", "build.sh", "sdists.lock"):
        shutil.copy(PACKAGING / name, repo / "packaging" / "appimage")
    (repo / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.2.3"\n')
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('a')\n")
    _stub(tmp_path / "bin", "podman", _PODMAN)
    env = _git_env(tmp_path, tmp_path / "bin")
    _commit_all(repo, env)

    def build(mode, script="packaging/build-appimage.sh"):
        log = tmp_path / "podman.log"
        log.unlink(missing_ok=True)
        done = subprocess.run(["bash", script, mode], cwd=repo, env=env, capture_output=True, text=True, timeout=120)
        return done, (log.read_text().splitlines() if log.exists() else [])

    return repo, build


def _named(stderr: str) -> list[str]:
    return [line.strip() for line in stderr.splitlines() if line.startswith("  ")]


def test_a_commit_is_built_by_its_own_definition_in_the_builder_made_here(build_repo):
    repo, build = build_repo
    done, podman = build("build")
    assert done.returncode == 0, done.stderr
    assert "Nothing built" not in done.stderr and "Note:" not in done.stderr
    assert len(podman) == 2 and re.fullmatch(r"image exists datoolkit-appimage-builder:[0-9a-f]{12}", podman[0])
    # the image of that name on this machine and no other: Podman is never to ask a registry for it
    assert podman[1].startswith("run --rm --pull=never ") and podman[1].endswith(" build")
    assert f" {podman[0].split()[-1]} bash -c " in podman[1] and " -e VERSION=1.2.3 " in podman[1]
    # a source changed and not committed is not built, which the note says; nor is a file git doesn't
    # track any of the definition
    (repo / "src" / "app.py").write_text("print('b')\n")
    (repo / "packaging" / "appimage" / "notes.txt").write_text("to do\n")
    done, podman = build("build")
    assert done.returncode == 0 and len(podman) == 2
    assert "Note: uncommitted changes are not built" in done.stderr and "Nothing built" not in done.stderr


@pytest.mark.parametrize("mode", ["build", "check"])
def test_a_build_definition_that_is_not_the_commits_is_refused(build_repo, mode):
    """The wrapper read pins.sh (the base image's digest, the apt snapshot) and the Containerfile
    from the working tree to make the builder, and said "uncommitted changes are not built"."""
    repo, build = build_repo
    appimage = repo / "packaging" / "appimage"
    with open(appimage / "pins.sh", "a") as f:
        f.write('APT_SNAPSHOT="20270101T000000Z"\n')
    with open(appimage / "Containerfile", "a") as f:
        f.write("RUN true\n")
    (appimage / "sdists.lock").unlink()
    done, podman = build(mode)
    assert done.returncode == 1 and podman == [] and done.stdout == ""
    assert done.stderr.startswith("Nothing built.") and "HEAD (" in done.stderr and "git stash" in done.stderr
    assert _named(done.stderr) == ["packaging/appimage/Containerfile", "packaging/appimage/pins.sh",
                                   "packaging/appimage/sdists.lock"]
    assert not (repo.parent / "dist").exists() or not os.listdir(repo.parent / "dist")


def test_the_script_that_runs_must_be_the_commits_too(build_repo, tmp_path):
    repo, build = build_repo
    committed = (repo / "packaging" / "build-appimage.sh").read_text()
    elsewhere = tmp_path / "another-checkout.sh"
    elsewhere.write_text(committed.replace('mode="${1:-build}"', 'mode="${1:-check}"'))
    assert elsewhere.read_text() != committed
    done, podman = build("build", script=str(elsewhere))       # run from this repository, which is clean
    assert done.returncode == 1 and podman == [] and _named(done.stderr) == ["packaging/build-appimage.sh"]
    (repo / "packaging" / "build-appimage.sh").write_text(committed + "# changed\n")
    done, podman = build("build")
    assert done.returncode == 1 and podman == [] and _named(done.stderr) == ["packaging/build-appimage.sh"]


def test_an_unknown_mode_is_said_before_anything_is_made(build_repo):
    _, build = build_repo
    done, podman = build("--help")
    assert done.returncode == 2 and "usage: " in done.stderr and podman == []


def test_a_machine_without_what_the_wrapper_needs_is_told_which(tmp_path):
    """git, Podman, GNU tar and a python3 with tomllib: without git the first line failed with
    "command not found", and a python3 before 3.11 with a traceback after the builder was made."""
    bash = shutil.which("bash")
    tools = tmp_path / "bin"
    tools.mkdir()
    done = subprocess.run([bash, str(ROOT / "packaging" / "build-appimage.sh"), "build"], cwd=tmp_path,
                          env={"PATH": str(tools)}, capture_output=True, text=True, timeout=30)
    assert done.returncode == 1 and done.stdout == ""
    said = done.stderr.splitlines()
    assert said[0].startswith("Nothing built.") and "this machine lacks" in said[0]
    assert [line.split()[0] for line in said[1:]] == ["git", "Podman,", "GNU", "python3"]
    assert "3.11" in said[4] and "tomllib" in said[4]

    def lacking(**stubs):
        for name, script in stubs.items():
            _stub(tools, name, script)
        script = "set -euo pipefail\n" + _function(WRAPPER, "missing_host_tools") + "missing_host_tools"
        found = subprocess.run([bash, "-c", script], env={"PATH": str(tools)}, capture_output=True, text=True,
                               timeout=30)
        assert found.returncode == 0 and found.stderr == ""
        return [line.split()[0] for line in found.stdout.splitlines()]

    # another tar, and a python3 that has no tomllib to import
    assert lacking(git="exit 0", podman="exit 0", tar='echo "bsdtar 3.7.2 - libarchive 3.7.2"',
                   python3="exit 1") == ["GNU", "python3"]
    assert lacking(tar='echo "tar (GNU tar) 1.35"', python3="exit 0") == []
    for needed in ("Podman", "git", "GNU tar", "python3 3.11"):
        assert needed in README.split("## Building")[1].split("```")[0], needed


# ---------------------------------------------------------------- RECORD, of what is in the image

RECORDS = PACKAGING / "records.py"


def _record_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def _installed(site: Path, name: str, files: dict[str, bytes]) -> Path:
    """A package as pip leaves it: its files, and a RECORD of them as pip writes one (the csv
    module's default dialect, sorted, lines ending \\r\\n, its own line last and bare)."""
    info = f"{name}-1.0.dist-info"
    rows = []
    for path, data in files.items():
        (site / path).parent.mkdir(parents=True, exist_ok=True)
        (site / path).write_bytes(data)
        rows.append((path, _record_hash(data), str(len(data))))
    out = io.StringIO(newline="")
    csv.writer(out).writerows(sorted(rows) + [(f"{info}/RECORD", "", "")])
    (site / info / "RECORD").write_bytes(out.getvalue().encode())
    return site / info / "RECORD"


def test_a_record_lists_what_is_in_the_image_and_nothing_of_the_wheel_it_came_from(tmp_path):
    """Six RECORDs still listed direct_url.json, with the hash of a file that held the hash of a
    wheel archive made in the build; others listed console scripts and Pillow libraries that were
    left out, and stripped extensions by the hashes they had before."""
    site = tmp_path / "usr" / "python" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    built = _installed(site, "built", {
        "built/__init__.py": b"x = 1\n",
        "built/_ext.abi3.so": b"\x7fELFcode" + b"debug information" * 40,
        "built/odd, name.txt": b"a name with a comma is quoted\n",
        "built-1.0.dist-info/METADATA": b"Name: built\n",
        "built-1.0.dist-info/direct_url.json":
            b'{"archive_info": {"hash": "sha256=0d1e"}, "url": "file:///work/wheels/built-1.0-py3-none-any.whl"}',
        "../../../bin/built": b"#!/work/AppDir/usr/python/bin/python3\nimport built\n"})
    wheel = _installed(site, "wheel", {"wheel.py": b"y = 2\n", "wheel-1.0.dist-info/METADATA": b"Name: wheel\n"})
    before, untouched = built.read_bytes(), (wheel.read_bytes(), os.stat(wheel).st_mtime_ns)
    assert before.count(b"\r\n") == 7 and b'"built/odd, name.txt",sha256=' in before
    # what build.sh does to them once pip is done
    (site / "built-1.0.dist-info" / "direct_url.json").unlink()
    (tmp_path / "usr" / "python" / "bin" / "built").unlink()
    (site / "built" / "_ext.abi3.so").write_bytes(b"\x7fELFcode")              # stripped

    clean = [sys.executable, "-I", str(RECORDS), str(site)]
    done = subprocess.run(clean, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "2 lines of files left out dropped, 1 brought up to date, in 2 packages" in done.stdout
    expected = [line for line in before.split(b"\r\n")
                if not line.startswith((b"../../../bin/built,", b"built-1.0.dist-info/direct_url.json,"))]
    at = next(i for i, line in enumerate(expected) if line.startswith(b"built/_ext.abi3.so,"))
    expected[at] = b"built/_ext.abi3.so," + _record_hash(b"\x7fELFcode").encode() + b",8"
    after = built.read_bytes()
    assert after == b"\r\n".join(expected)                  # every other line as it was, in its place
    assert b"direct_url" not in after and b"/bin/" not in after
    # a RECORD with nothing to change is not written, and a second run finds nothing more to do
    assert (wheel.read_bytes(), os.stat(wheel).st_mtime_ns) == untouched
    again = subprocess.run(clean, capture_output=True, text=True, timeout=60)
    assert "0 lines of files left out dropped, 0 brought up to date" in again.stdout and built.read_bytes() == after

    empty = tmp_path / "empty"
    empty.mkdir()
    nothing = subprocess.run([sys.executable, "-I", str(RECORDS), str(empty)], capture_output=True, text=True)
    assert nothing.returncode != 0 and "no package's RECORD" in nothing.stderr


def test_records_are_brought_up_to_date_once_everything_is_left_out_and_before_the_bytecode():
    at = BUILD.index('"$W/buildpy/bin/python3" "$P/records.py" "$W"/AppDir/usr/python/lib/python3.*/site-packages')
    for earlier in ("-m pip uninstall -q -y pip", "-exec strip --strip-unneeded", 'rm "$script"',
                    'rm -f "$site"/PIL/_imagingft', "-name direct_url.json -delete",
                    "-name __pycache__ -type d -prune"):
        assert BUILD.index(earlier) < at, earlier
    assert at < BUILD.index("-m compileall") < BUILD.index("mksquashfs")
