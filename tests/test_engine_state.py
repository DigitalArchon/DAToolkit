"""The engine's own state: how much of a transcript a capture reads, and how often a snapshot
reads the pin store."""

from datoolkit.config import Host

from test_engine import env  # noqa: F401


def _queue_one(engine, command="systemctl restart nginx"):
    engine.new_case("queue", "open")
    return engine.queue.add("c1", [{"session_id": "local", "command": command, "risk": "disruptive"}])[0]


# ---------------------------------------------------------------- capture

async def test_capture_reads_a_bounded_part_of_a_long_output_and_says_so(env, monkeypatch):  # noqa: F811
    engine, _, _ = env
    p = _queue_one(engine, "tail -f /var/log/syslog")
    monkeypatch.setattr(type(engine), "CAPTURE_MAX_BYTES", 1000)
    path = engine.case.transcript_path("local")
    path.write_bytes(b"before\n" + b"first line\n" + b"x" * 5000 + b"\nlast line\n")
    p.capture_start = len(b"before\n")
    got = engine.capture(p.num)
    assert got["truncated"] is True and got["skipped_bytes"] == 5022 - 1000
    assert got["text"].startswith("first line\nxxx") and got["text"].endswith("xxx\nlast line")
    assert "\n[... 4,022 bytes of output not read from the transcript ...]\n" in got["text"]
    assert len(got["text"]) < 1100
    # up to the next command's start, when there is one, and whole when it fits
    q = engine.queue.add("c2", [{"session_id": "local", "command": "uptime", "risk": "read_only"}])[0]
    q.capture_start = len(b"before\nfirst line\n")
    assert engine.capture(p.num) == {"text": "first line", "source": "transcript"}
    assert engine.capture(q.num)["truncated"] is True and "first line" not in engine.capture(q.num)["text"]
    monkeypatch.setattr(type(engine), "CAPTURE_MAX_BYTES", 4 * 1024 * 1024)
    assert engine.capture(q.num) == {"text": "x" * 5000 + "\nlast line", "source": "transcript"}


# ---------------------------------------------------------------- the pin store

async def test_a_snapshot_reads_the_pin_store_once(env, monkeypatch):  # noqa: F811
    engine, _, _ = env
    engine.cfg.hosts += [Host(f"rdp{n}", "rdp", f"10.0.0.{n}", auth="password") for n in range(1, 6)]
    engine.pins.pin("10.0.0.3", 3389, {"sha256": "AA:BB", "subject": "CN=rdp3", "issuer": "CN=rdp3", "not_after": "2030-01-01"})
    reads, real = [], type(engine.pins)._load

    def load(self):
        reads.append(1)
        return real(self)

    monkeypatch.setattr(type(engine.pins), "_load", load)
    view = engine._config_view()
    assert reads == [1]
    assert {h["name"]: h.get("pinned") for h in view["hosts"]} == {
        "web01": None, "rdp1": "", "rdp2": "", "rdp3": "AA:BB", "rdp4": "", "rdp5": ""}
    reads.clear()
    engine.cfg.hosts = [h for h in engine.cfg.hosts if h.kind != "rdp"]
    engine._config_view()
    assert reads == []                                   # no host that could have a pin: not read at all
