"""The frontend's own tests (tests/js/*.test.cjs), run in Node against the real app.js and
index.html with a stand-in DOM (tests/js/fakedom.cjs). The page is fed snapshots of a real
Engine, as the events socket sends them, so a change in what the server sends reaches these
tests too. Skipped where Node isn't installed.

The stand-in DOM can't run DOMPurify, so the page's Markdown is the real marked's and the
sanitiser's settings are checked as given. tests/js/sanitize.test.cjs runs the real DOMPurify with
them in jsdom, which is not one of this repository's packages: those tests skip themselves unless a
jsdom is on Node's path (NODE_PATH=<a node_modules that has jsdom>).

To run them on their own, with the same caps:
DAT_STATES=<a file of snapshots> node --max-old-space-size=512 --test --test-isolation=none tests/js/"""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from test_engine import env  # noqa: F401

JS = Path(__file__).parent / "js"
NODE = shutil.which("node")


def snapshots(engine) -> dict:
    """No case; case A with a terminal and four queued commands; cases B and C."""
    states = {"none": engine.snapshot()}
    engine.new_case("Case A", "open")
    engine.open_session("local")
    sid = engine.sessions.roster()[0]["id"]
    engine.queue.add("c1", [{"session_id": sid, "command": c, "purpose": p, "risk": r} for c, p, r in (
        ("uptime", "load", "read_only"), ("systemctl reload nginx", "apply", "modifying"),
        ("df -h", "disk", "read_only"), ("cat /etc/hostname", "name", "read_only"))])
    states["a"] = engine.snapshot()
    for key in ("b", "c"):
        engine.new_case(f"Case {key.upper()}", "open")
        states[key] = engine.snapshot()
    return states


@pytest.mark.skipif(not NODE, reason="Node isn't installed")
async def test_the_frontend(env, tmp_path):  # noqa: F811
    engine, _, _ = env
    path = tmp_path / "states.json"
    path.write_text(json.dumps(snapshots(engine)), encoding="utf-8")
    # one process, so the timeout's kill ends all of it; a capped heap, so a page that runs away
    # fails here instead of taking the machine's memory
    run = await asyncio.to_thread(
        subprocess.run, [NODE, "--max-old-space-size=512", "--test", "--test-isolation=none", "--test-timeout=30000",
                         "--test-reporter=spec", *sorted(map(str, JS.glob("*.test.cjs")))],
        env={**os.environ, "DAT_STATES": str(path)}, capture_output=True, text=True, timeout=180, check=False)
    assert run.returncode == 0, run.stdout[-20000:] + run.stderr[-5000:]
