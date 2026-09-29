import asyncio
import os
import subprocess

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
