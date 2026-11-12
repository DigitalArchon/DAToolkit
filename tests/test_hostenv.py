"""The environment of the programs DA Toolkit starts for the technician (hostenv.py): what the
AppImage and its AppRun set for DA Toolkit itself stays out of it."""

import asyncio
import re
from pathlib import Path

import pytest

from datoolkit import hostenv
from datoolkit.config import Config, Host
from datoolkit.engine import Engine
from datoolkit.hostenv import host_env

APPRUN = Path(__file__).resolve().parents[1] / "packaging" / "appimage" / "AppRun"
SYSTEM = "/etc/ssl/certs/ca-certificates.crt"


@pytest.fixture
def appimage(monkeypatch):
    """As DA Toolkit finds its environment when started from the AppImage."""
    monkeypatch.setenv("APPDIR", "/tmp/.mount_DAToolkit")
    monkeypatch.setenv("APPIMAGE", "/home/x/DAToolkit.AppImage")
    for name in (*hostenv.CA_VARS, hostenv.APPRUN_SET):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_the_bundles_named_here_are_the_ones_apprun_picks_from():
    text = APPRUN.read_text()
    listed = re.search(r"for bundle in \\\n((?:\s+\S+ \\\n)*\s+\S+); do", text).group(1)
    assert tuple(line.strip(" \\") for line in listed.splitlines()) == hostenv.APPRUN_CA_BUNDLES
    # and it sets only what the environment didn't: that is what makes leaving them out a restoring
    assert 'if [ -z "${SSL_CERT_FILE:-}" ]; then' in text and '[ -z "${REQUESTS_CA_BUNDLE:-}" ]' in text


def test_what_apprun_set_for_tls_stays_out_of_child_programs(appimage):
    """SSL_CERT_FILE and REQUESTS_CA_BUNDLE, set by AppRun for DA Toolkit's own clients, reached
    the technician's shells: a Python tool with certificates of its own was pointed at the
    system's bundle there, and nowhere else."""
    appimage.setenv("SSL_CERT_FILE", SYSTEM)                 # as AppRun leaves them on Debian and its kin
    appimage.setenv("REQUESTS_CA_BUNDLE", SYSTEM)
    env = host_env(TERM="xterm-256color")
    assert "SSL_CERT_FILE" not in env and "REQUESTS_CA_BUNDLE" not in env and "APPDIR" not in env
    assert hostenv.own_tls_env() == {"SSL_CERT_FILE": SYSTEM, "REQUESTS_CA_BUNDLE": SYSTEM}   # DA Toolkit's own, kept
    # each of the places AppRun looks
    for bundle in hostenv.APPRUN_CA_BUNDLES:
        appimage.setenv("SSL_CERT_FILE", bundle)
        appimage.setenv("REQUESTS_CA_BUNDLE", bundle)
        assert not set(hostenv.CA_VARS) & set(host_env())


def test_the_technicians_own_ca_settings_reach_child_programs(appimage):
    # a bundle of their own in both (a company's CA): AppRun changed nothing, and nothing is taken away
    appimage.setenv("SSL_CERT_FILE", "/home/x/corp-ca.pem")
    appimage.setenv("REQUESTS_CA_BUNDLE", "/home/x/corp-ca.pem")
    env = host_env()
    assert (env["SSL_CERT_FILE"], env["REQUESTS_CA_BUNDLE"]) == ("/home/x/corp-ca.pem", "/home/x/corp-ca.pem")
    # their own REQUESTS_CA_BUNDLE beside the SSL_CERT_FILE that AppRun set
    appimage.setenv("SSL_CERT_FILE", SYSTEM)
    appimage.setenv("REQUESTS_CA_BUNDLE", "/home/x/corp-ca.pem")
    env = host_env()
    assert "SSL_CERT_FILE" not in env and env["REQUESTS_CA_BUNDLE"] == "/home/x/corp-ca.pem"
    # and outside an AppImage nothing is AppRun's
    appimage.delenv("APPDIR")
    appimage.delenv("APPIMAGE")
    appimage.setenv("REQUESTS_CA_BUNDLE", SYSTEM)
    env = host_env()
    assert (env["SSL_CERT_FILE"], env["REQUESTS_CA_BUNDLE"]) == (SYSTEM, SYSTEM)


def test_an_apprun_that_says_what_it_set_is_taken_at_its_word(appimage):
    # the technician's own SSL_CERT_FILE, which happens to be the system's bundle; AppRun added the other
    appimage.setenv("SSL_CERT_FILE", SYSTEM)
    appimage.setenv("REQUESTS_CA_BUNDLE", SYSTEM)
    appimage.setenv(hostenv.APPRUN_SET, "REQUESTS_CA_BUNDLE")
    env = host_env()
    assert env["SSL_CERT_FILE"] == SYSTEM and "REQUESTS_CA_BUNDLE" not in env and hostenv.APPRUN_SET not in env
    appimage.setenv(hostenv.APPRUN_SET, "")                  # it set nothing: both are the technician's
    assert set(hostenv.CA_VARS) <= set(host_env())
    appimage.setenv(hostenv.APPRUN_SET, "SSL_CERT_FILE REQUESTS_CA_BUNDLE PATH")     # and only these two are its to name
    env = host_env()
    assert not set(hostenv.CA_VARS) & set(env) and "PATH" in env


async def test_a_terminal_doesnt_get_appruns_ca_variables_and_the_winrm_console_does(appimage, tmp_path):
    appimage.setenv("SSL_CERT_FILE", SYSTEM)
    appimage.setenv("REQUESTS_CA_BUNDLE", SYSTEM)
    engine = Engine(Config(hosts=[Host("win", "winrm", "192.168.250.7", user="bob", auth="ntlm", winrm_ssl=False)]),
                    lambda event: None, tmp_path / "rt", save_config=lambda c: None)
    await engine.start()
    try:
        engine.new_case("env", "open")
        show = 'echo "cert=[${SSL_CERT_FILE:-}] requests=[${REQUESTS_CA_BUNDLE:-}] appdir=[${APPDIR:-}]"; sleep 0.3'
        spawn = engine.sessions.spawn
        shell = spawn("shell", ["/bin/sh", "-c", show], {}, name="shell", kind="local")
        # the console is DA Toolkit's own program, with its Python: it checks WinRM's certificates against the host's CAs
        appimage.setattr(engine.sessions, "spawn", lambda sid, argv, env, **meta: spawn(sid, ["/bin/sh", "-c", show], env, **meta))
        engine.open_session("winrm", "win")
        console = engine.sessions.sessions["win"]
        for _ in range(200):
            if b"appdir=" in shell.backlog and b"appdir=" in console.backlog:
                break
            await asyncio.sleep(0.02)
        assert b"cert=[] requests=[] appdir=[]" in bytes(shell.backlog)
        assert f"cert=[{SYSTEM}] requests=[{SYSTEM}] appdir=[]".encode() in bytes(console.backlog)
    finally:
        await engine.stop()
