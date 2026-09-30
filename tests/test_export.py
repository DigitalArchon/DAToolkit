"""Export: every model request is logged with its prompt, reasoning and reply, and the full
export packs that with the images the model received."""

import base64
import io
import json
import zipfile
from io import BytesIO

from PIL import Image

from test_engine import env, sse, wait_turn  # noqa: F401


def _jpeg() -> str:
    buf = BytesIO()
    Image.new("RGB", (32, 24), (0, 90, 200)).save(buf, "JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def thinking_stream(sid):
    args = json.dumps({"items": [{"session_id": sid, "command": "df -h", "purpose": "disk", "risk": "read_only"}]})
    return sse(({"role": "assistant", "reasoning": "PRIVATE-THOUGHT: disk is probably full."}, None),
               ({"content": "Let's check the disk."}, None),
               ({"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                 "function": {"name": "propose_commands", "arguments": args}}]}, None),
               ({}, "tool_calls"))


async def run_case(engine, fake):
    engine.new_case("Export me", "open")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    engine.open_session("local")
    sid = engine.sessions.roster()[0]["id"]
    fake.responses.append(thinking_stream(sid))
    engine.send("Server is out of space", images=[_jpeg()])
    await wait_turn(engine)
    engine.update_item(1, status="ran")
    fake.responses.append(sse(({"role": "assistant", "content": "Root is 99% full."}, "stop")))
    engine.send("", results=[{"num": 1, "text": "/dev/sda1 99% /"}])
    await wait_turn(engine)
    return sid


async def test_requests_are_logged_with_prompt_reasoning_and_reply(env):  # noqa: F811
    engine, fake, _ = env
    await run_case(engine, fake)
    reqs = [json.loads(line) for line in (engine.case.dir / "requests.jsonl").read_text().splitlines()]
    assert [r["purpose"] for r in reqs] == ["chat", "chat"]
    first, second = reqs
    assert first["system"].startswith("You are a senior systems") and first["conv_index"] == 0
    assert first["response"]["reasoning"] == "PRIVATE-THOUGHT: disk is probably full."
    assert first["response"]["tool_calls"][0]["name"] == "propose_commands"
    # images are referenced by file name, never embedded
    assert first["messages"][0]["content"][1] == {"type": "image", "file": "img-1.jpg"}
    assert "data:image" not in (engine.case.dir / "requests.jsonl").read_text()
    # the second request sends only what is new: the echoed reply, the tool result, the results message
    assert [m["role"] for m in second["messages"]] == ["assistant", "tool", "user"]
    assert "99% /" in second["messages"][-1]["content"]
    assert "system" in second           # the queue changed, so the system prompt did too


async def test_full_export_zip(env):  # noqa: F811
    engine, fake, _ = env
    await run_case(engine, fake)
    name, data = engine.export_full()
    assert name == f"{engine.case.id}-full-export.zip"
    z = zipfile.ZipFile(io.BytesIO(data))
    names = set(z.namelist())
    assert {"README.txt", "full-transcript.md", "images/img-1.jpg", "documents/transcript.md",
            "data/requests.jsonl", "data/conversation.json", "data/events.jsonl", "data/chat.json"} <= names
    assert not any(n.startswith("terminals/") for n in names)
    assert z.read("images/img-1.jpg") == (engine.case.dir / "img-1.jpg").read_bytes()
    md = z.read("full-transcript.md").decode()
    for needle in ("### Request 1 · chat", "### Request 2 · chat", "You are a senior systems",
                   "PRIVATE-THOUGHT: disk is probably full.", "Tool call `propose_commands`", '"command": "df -h"',
                   "![img-1.jpg](images/img-1.jpg)", "**Technician → AI**", "Server is out of space",
                   "**Tool result → AI**", "Root is 99% full.", "(changed)"):
        assert needle in md, needle
    assert "data:image" not in z.read("data/conversation.json").decode()
    assert "exported_full" in (engine.case.dir / "events.jsonl").read_text()
    _, with_terms = engine.export_full(include_terminals=True)
    assert any(n.startswith("terminals/term-") for n in zipfile.ZipFile(io.BytesIO(with_terms)).namelist())
    engine.sessions.close_all()


async def test_older_case_is_reconstructed(env):  # noqa: F811
    engine, fake, _ = env
    await run_case(engine, fake)
    (engine.case.dir / "requests.jsonl").unlink()          # as if the case predates request logging
    md = zipfile.ZipFile(io.BytesIO(engine.export_full()[1])).read("full-transcript.md").decode()
    assert "Conversation before request logging (reconstructed)" in md
    assert "PRIVATE-THOUGHT" in md and "as it would be sent now" in md and "Tool call `propose_commands`" in md
    engine.sessions.close_all()


async def test_failed_request_is_logged(env):  # noqa: F811
    """A request that fails in flight was still sent, so it is logged with its error."""
    engine, fake, _ = env
    engine.new_case("fail", "open")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    assert not fake.responses                   # the fake API has no reply queued: the request errors out
    engine.send("hi")
    await wait_turn(engine)
    reqs = [json.loads(line) for line in (engine.case.dir / "requests.jsonl").read_text().splitlines()]
    assert len(reqs) == 1 and reqs[0]["response"]["error"]
    assert reqs[0]["messages"] == [{"role": "user", "content": "hi"}] and "system" in reqs[0]


async def test_markdown_export_returns_content_and_name(env):  # noqa: F811
    engine, fake, _ = env
    await run_case(engine, fake)
    r = engine.export_markdown()
    assert r["filename"] == f"{engine.case.id}-transcript.md" and "# Export me" in r["content"]
    engine.sessions.close_all()


def test_native_save_dialog_writes_where_chosen(tmp_path):
    from datoolkit.app import Desktop

    class FakeWindow:
        def __init__(self, answer):
            self.answer, self.asked = answer, None

        def create_file_dialog(self, kind, directory="", save_filename=""):
            self.asked = save_filename
            return self.answer

    desktop = Desktop(FakeWindow((str(tmp_path / "chosen.zip"),)))
    assert desktop.save_file("../../etc/case-full-export.zip", b"PK-data") == str(tmp_path / "chosen.zip")
    assert (tmp_path / "chosen.zip").read_bytes() == b"PK-data"
    assert desktop._window.asked == "case-full-export.zip"      # only a name is suggested, never a path
    desktop._window = FakeWindow(None)                           # cancelled
    assert desktop.save_file("x.md", b"x") is None
