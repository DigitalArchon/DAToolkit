import asyncio
import os
import subprocess

import pytest

from datoolkit.sessions.askpass import AskpassBridge
from datoolkit.sessions.manager import SessionManager, TranscriptWriter


async def test_pty_session_output_and_transcript(tmp_path):
    changes = []
    mgr = SessionManager(on_change=lambda: changes.append(1))
    mgr.set_transcript_paths(lambda sid: tmp_path / f"{sid}.log")
    mgr.spawn("t1", ["/bin/sh", "-c", "printf '\\033[31mhello\\033[0m world\\n'; sleep 0.2"], {},
              name="t1", kind="local")
    backlog, q = mgr.subscribe("t1")
    got = backlog
    for _ in range(50):
        if mgr.sessions["t1"].exited:
            break
        try:
            chunk = await asyncio.wait_for(q.get(), 0.2)
            got += chunk or b""
        except asyncio.TimeoutError:
            pass
    assert b"hello" in got
    assert mgr.sessions["t1"].exited
    log = (tmp_path / "t1.log").read_text()
    assert "hello world" in log and "\x1b" not in log
    mgr.close_all()


async def test_pty_input_roundtrip():
    mgr = SessionManager(on_change=lambda: None)
    mgr.spawn("cat", ["/bin/cat"], {}, name="cat", kind="local")
    _, q = mgr.subscribe("cat")
    mgr.write("cat", b"ping-pong\n")
    got = b""
    for _ in range(20):
        got += await asyncio.wait_for(q.get(), 2)
        if got.count(b"ping-pong") >= 2:  # echo + cat output
            break
    assert got.count(b"ping-pong") >= 2
    mgr.close("cat")
    assert "cat" not in mgr.sessions


def test_transcript_split_escape(tmp_path):
    w = TranscriptWriter()
    w.open(tmp_path / "x.log")
    w.write(b"abc\x1b[3")
    w.write(b"1mred\x1b[0m\r\n")
    w.close()
    assert (tmp_path / "x.log").read_text() == "abcred\n"


async def test_askpass_bridge(tmp_path):
    calls = []

    async def handler(sid, prompt, kind):
        calls.append((sid, prompt, kind))
        return "s3cret" if "password" in prompt else None

    bridge = AskpassBridge(tmp_path / "rt", handler)
    await bridge.start()
    try:
        assert (os.stat(bridge.sock_path).st_mode & 0o777) == 0o600
        env = {**os.environ, **bridge.env_for("web01")}

        def run(prompt):
            return subprocess.run([str(bridge.script_path), prompt], env=env, capture_output=True, text=True)

        ok = await asyncio.to_thread(run, "admin@web01's password:")
        assert ok.returncode == 0 and ok.stdout == "s3cret\n"
        refused = await asyncio.to_thread(run, "Are you sure you want to continue connecting (yes/no)?")
        assert refused.returncode == 1 and refused.stdout == ""
        bad = await asyncio.to_thread(subprocess.run, [str(bridge.script_path), "password"],
                                      env={**env, "DATOOLKIT_TOKEN": "wrong"}, capture_output=True, text=True)
        assert bad.returncode == 1
        assert calls[0] == ("web01", "admin@web01's password:", "askpass")
        assert len(calls) == 2  # wrong token never reaches the handler
    finally:
        await bridge.stop()


async def test_the_askpass_bridge_stops_with_a_prompt_still_up(tmp_path):
    asked = asyncio.Event()

    async def handler(sid, prompt, kind):
        asked.set()
        await asyncio.sleep(3600)          # the technician never answers

    bridge = AskpassBridge(tmp_path / "rt", handler)
    await bridge.start()
    env = {**os.environ, **bridge.env_for("web01")}
    helper = subprocess.Popen([str(bridge.script_path), "admin@web01's password:"], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        await asyncio.wait_for(asked.wait(), 10)
        await asyncio.wait_for(bridge.stop(), 3)        # quitting doesn't wait for an answer
        assert await asyncio.to_thread(helper.wait, 10) == 1
    finally:
        helper.kill()
        helper.wait()
        helper.stdout.close()


def test_sessions_to_a_host_with_a_long_name_keep_their_own_transcripts(tmp_path):
    from datoolkit.case import Case
    case = Case.create("t", "open", root=tmp_path)
    a = "fileserver01-sydney-corp-acme-holdings-com-au"         # 45 characters: names were cut at 40
    b = a + "-2"                                                 # a second session to it
    assert case.transcript_path(a) != case.transcript_path(b)
    old = case.dir / f"term-{a[:40]}.log"                       # what a case from before has
    old.write_text("before")
    assert case.transcript_file(a) == old
    case.transcript_path(a).write_text("now")
    assert case.transcript_file(a) == case.transcript_path(a)


@pytest.mark.parametrize("chunks,written", [
    # a window title (OSC) cut off by the read: none of it is output
    ([b"ok\r\n\x1b]0;bob@web01: ~/src", b"\x07bob@web01:~/src$ "], "ok\nbob@web01:~/src$ "),
    ([b"a\x1b]0;title\x1b", b"\\b"], "ab"),                                  # its ESC \ end split too
    ([b"a\x1b]0;" + b"t" * 300, b"\x07b"], "ab"),                            # longer than 64 characters
    ([b"x\x1b7y\x1b8z\x1bcw"], "xyzw"),                                     # save/restore cursor, reset
    ([b"abc\x1b[3", b"1mred\x1b[0m\r\n"], "abcred\n"),
])
def test_escape_sequences_split_across_reads_stay_out_of_the_transcript(tmp_path, chunks, written):
    w = TranscriptWriter()
    w.open(tmp_path / "x.log")
    for c in chunks:
        w.write(c)
    w.close()
    assert (tmp_path / "x.log").read_text() == written


def test_output_of_nothing_but_string_introducers_doesnt_hold_the_app_up(tmp_path):
    """A program (or a remote host) printing ESC ] over and over: finding the string still open
    took time growing with the square of the read, 24 s of a busy event loop per megabyte, with
    every terminal, websocket and the companion waiting."""
    import random
    import re
    import time

    from datoolkit.sessions.manager import _open_string

    w = TranscriptWriter()
    w.open(tmp_path / "flood.log")
    started = time.perf_counter()
    for _ in range(4):                                   # a 64 KB read each, as the PTY hands them over
        w.write(b"\x1b]" * 32000 + b"\x07x")
    w.write(b"\x1bP" * 32000)
    w.write(b"\x1b]0;" + b"t" * 60000)
    took = time.perf_counter() - started
    w.close()
    assert took < 2, f"{took:.1f} s"                     # it was 10 s and more for the first of these
    assert (tmp_path / "flood.log").read_text().count("x") == 4
    # and it finds what the pattern found
    pattern = re.compile(r"\x1b[\]PX^_](?:[^\x07\x1b]|\x1b(?!\\))*+\Z")
    rng = random.Random(11)
    for _ in range(3000):
        text = "".join(rng.choice(["\x1b", "]", "P", "\\", "\x07", "a", "[", "\n"]) for _ in range(rng.randrange(0, 14)))
        found = pattern.search(text)
        assert _open_string(text) == (found.start() if found else -1), repr(text)


async def test_a_viewer_that_falls_behind_has_output_skipped_not_kept(tmp_path):
    from datoolkit.sessions.manager import OutputQueue
    q = OutputQueue(limit=100)
    for _ in range(9):
        q.put_nowait(b"x" * 30)                 # 270 bytes for a viewer that reads nothing
    got = await q.get()
    assert b"bytes of output skipped here" in got and q._q.qsize() <= 3
    q.put_nowait(None)                           # the session's end still arrives
    while (data := await q.get()) is not None:
        assert len(data) <= 100


async def test_a_security_key_touch_notice_is_shown_not_asked(tmp_path):
    # ssh runs askpass with SSH_ASKPASS_PROMPT=none to say "touch your key": nothing is waited for
    calls = []

    async def handler(sid, prompt, kind):
        calls.append((prompt, kind))
        return "" if kind == "notice" else None

    bridge = AskpassBridge(tmp_path / "rt", handler)
    await bridge.start()
    try:
        env = {**os.environ, **bridge.env_for("web01"), "SSH_ASKPASS_PROMPT": "none"}
        done = await asyncio.to_thread(subprocess.run, [str(bridge.script_path), "Confirm user presence for key ED25519-SK"],
                                       env=env, capture_output=True, text=True, timeout=20)
        assert done.returncode == 0 and calls == [("Confirm user presence for key ED25519-SK", "notice")]
    finally:
        await bridge.stop()


def _stray_modules(folder):
    """A home directory with files named like modules the helpers import: each, if imported,
    leaves a mark and breaks the import."""
    folder.mkdir()
    for name in ("secrets", "json", "signal", "socket", "hmac", "shlex", "tempfile", "readline", "shutil", "datoolkit"):
        (folder / f"{name}.py").write_text(
            f"open({str(folder / 'imported')!r}, 'a').write({name!r} + '\\n')\nraise RuntimeError('stray {name}.py imported')\n")
    return folder


async def test_the_password_helper_imports_nothing_from_the_directory_it_runs_in(tmp_path):
    """ssh runs the helper in the session's directory, the technician's home. Started with plain
    `python -m`, a stray ~/secrets.py or ~/json.py there was imported in place of Python's own,
    by the process that carries the password."""
    home = _stray_modules(tmp_path / "home")

    async def handler(sid, prompt, kind):
        return "s3cret"

    bridge = AskpassBridge(tmp_path / "rt", handler)
    await bridge.start()
    try:
        env = {k: v for k, v in os.environ.items() if k != "SSH_ASKPASS_PROMPT"} | bridge.env_for("web01")
        done = await asyncio.to_thread(subprocess.run, [str(bridge.script_path), "bob@web01.lan's password: "],
                                       env=env, cwd=home, capture_output=True, text=True, timeout=20)
        assert (done.returncode, done.stdout, done.stderr) == (0, "s3cret\n", "")
        assert not (home / "imported").exists()
    finally:
        await bridge.stop()


async def test_the_winrm_console_imports_nothing_from_the_directory_it_runs_in(tmp_path, monkeypatch):
    import sys

    from datoolkit.config import Config, Host
    from datoolkit.engine import Engine

    home = _stray_modules(tmp_path / "home")
    engine = Engine(Config(hosts=[Host("win", "winrm", "192.168.250.7", user="bob", auth="ntlm", winrm_ssl=False)]),
                    lambda event: None, tmp_path / "rt", save_config=lambda c: None)
    await engine.start()
    started = []
    try:
        engine.new_case("console", "open")
        spawn = engine.sessions.spawn
        monkeypatch.setattr(engine.sessions, "spawn", lambda sid, argv, env, **meta: started.append((argv, env)) or spawn(
            sid, ["/bin/cat"], env, **meta))
        engine.open_session("winrm", "win")
    finally:
        await engine.stop()
    argv, env = started[0]
    assert argv == [sys.executable, "-P", "-m", "datoolkit.sessions.psrp_console"]
    # as the session runs it, in a home that holds such files, and with no settings: it gets as far
    # as reading them (every import done) and stops there
    env = {**os.environ, **{k: v for k, v in env.items() if k != "DATOOLKIT_PSRP"}}
    done = await asyncio.to_thread(subprocess.run, argv, env=env, cwd=home, capture_output=True, text=True, timeout=30,
                                   stdin=subprocess.DEVNULL)
    assert "KeyError: 'DATOOLKIT_PSRP'" in done.stderr and "stray" not in done.stderr
    assert not (home / "imported").exists()


async def test_sessions_link_by_themselves_only_when_they_reach_the_same_machine():
    """An address alone doesn't say which machine: a host saved as localhost:3390 is a forwarded
    port, not this computer, and two clients' 192.168.1.1 behind two jump hosts are two routers.
    Linked, the model is told they are one machine and to send one's commands to the other."""
    from datoolkit.sessions.manager import RdpSession, WindowSession, link_key

    mgr = SessionManager(on_change=lambda: None)

    def pty(sid, kind, address, **meta):
        return mgr.spawn(sid, ["/bin/cat"], {}, name=sid, kind=kind, address=address, **meta)

    def rdp(sid, address, port):
        return mgr.add(RdpSession(id=sid, name=sid, params={}, address=address, port=port))

    try:
        local, local2 = pty("local", "local", "localhost"), pty("local-2", "local", "localhost")
        assert local.device == local2.device                              # two shells on this computer
        # saved hosts at a loopback address: never the technician's own shell, and one device per port
        forwarded = rdp("acme-dc", "localhost", 3390)
        other = rdp("other-dc", "127.0.0.1", 3391)
        same_port = pty("acme-dc-winrm", "winrm", "LOCALHOST", port=3390)
        here = pty("me", "ssh", "::1", port=22)
        assert len({local.device, forwarded.device, other.device, here.device}) == 4
        assert same_port.device == forwarded.device
        # the same private address behind different jump hosts, and behind none
        fw_a = pty("fw-a", "ssh", "192.168.1.1", jump="me@bastion.acme.example", port=22)
        fw_b = pty("fw-b", "ssh", "192.168.1.1", jump="me@bastion.other.example", port=22)
        fw_c = pty("fw-c", "ssh", "192.168.1.1", port=22)
        assert len({fw_a.device, fw_b.device, fw_c.device}) == 3
        # the same address by the same way is one machine, whoever logs in to the jump host and on whatever port
        fw_a2 = pty("fw-a2", "ssh", "192.168.1.1.", jump="other@BASTION.acme.example:2222", port=2022)
        fw_c2 = rdp("fw-c-rdp", "192.168.1.1", 3389)
        assert fw_a2.device == fw_a.device and fw_c2.device == fw_c.device
        # a window has no address and links to nothing by itself
        window = mgr.add(WindowSession(id="w", name="w", xid=1))
        assert link_key(window) is None and window.device not in {s.device for s in mgr.sessions.values() if s is not window}
        # by hand, anything links to anything, as before
        mgr.link("acme-dc", "local")
        assert forwarded.device == local.device and forwarded.link == "manual"
        mgr.link("fw-a2", None)
        assert fw_a2.device != fw_a.device and fw_a2.link == "unlinked"
    finally:
        mgr.close_all()
        await mgr.wait_closed()


async def test_a_host_session_carries_its_jump_host_and_port_for_linking(tmp_path, monkeypatch):
    from datoolkit.config import Config, Host
    from datoolkit.engine import Engine

    engine = Engine(Config(hosts=[Host("fw-a", "ssh", "192.168.1.1", user="admin", jump="me@bastion-a"),
                                  Host("fw-b", "ssh", "192.168.1.1", user="admin", jump="me@bastion-b", port=2022),
                                  Host("fwd", "ssh", "localhost", port=2201)]),
                    lambda event: None, tmp_path / "rt", save_config=lambda c: None)
    await engine.start()
    try:
        engine.new_case("link", "open")
        spawn = engine.sessions.spawn
        monkeypatch.setattr(engine.sessions, "spawn", lambda sid, argv, env, **meta: spawn(sid, ["/bin/cat"], env, **meta))
        engine.open_session("local")
        for name in ("fw-a", "fw-b", "fwd"):
            engine.open_session("ssh", name)
        s = engine.sessions.sessions
        assert (s["fw-a"].jump, s["fw-a"].port, s["fw-b"].jump, s["fw-b"].port) == ("me@bastion-a", 22, "me@bastion-b", 2022)
        assert len({x.device for x in s.values()}) == 4               # nothing here is the same machine
    finally:
        await engine.stop()
