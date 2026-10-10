"""Tool calls written into the message as text are read back into calls."""

import json

from datoolkit.llm.textcalls import extract

from test_engine import env, sse, wait_turn  # noqa: F401
from test_revise_search import setup


def test_qwen_xml_style_call():
    text = ("Turn the boolean on; no restart needed.\n\n<tool_call>\n<function=propose_commands>\n<parameter=items>\n"
            '[{"session_id": "app01", "command": "sudo setsebool -P httpd_can_network_connect on", "risk": "modifying"}]\n'
            "</parameter>\n</function>\n</tool_call>")
    clean, calls = extract(text)
    assert clean == "Turn the boolean on; no restart needed."
    assert [c.name for c in calls] == ["propose_commands"]
    assert calls[0].parsed()["items"][0]["command"].startswith("sudo setsebool")


def test_hermes_json_call_and_dsml_call():
    text = ('First.\n<tool_call>\n{"name": "ask_technician", "arguments": {"questions": [{"question": "When?"}]}}\n</tool_call>\n'
            "Then.\n<｜DSML｜tool_calls>\n<｜DSML｜invoke name=\"propose_commands\">\n"
            "<｜DSML｜parameter name=\"items\" string=\"false\">[{\"command\": \"df -h\"}]</｜DSML｜parameter>\n"
            "</｜DSML｜invoke>\n</｜DSML｜tool_calls>")
    clean, calls = extract(text)
    assert clean == "First.\n\nThen."
    assert [c.name for c in calls] == ["ask_technician", "propose_commands"]
    assert calls[1].parsed() == {"items": [{"command": "df -h"}]}
    assert len({c.id for c in calls}) == 2


def test_unreadable_markup_and_plain_text_are_left_alone():
    for text in ("Use `<tool_call>` tags? No.", "<tool_call>not json</tool_call>", "Plain message."):
        assert extract(text) == (text, [])


async def test_engine_queues_a_call_written_as_text(env):  # noqa: F811
    engine, fake, _ = env
    sid = setup(engine)
    body = ("Checking the link.\n<tool_call>\n<function=propose_commands>\n<parameter=items>\n"
            + json.dumps([{"session_id": sid, "command": "ethtool eth0", "purpose": "speed", "risk": "read_only"}])
            + "\n</parameter>\n</function>\n</tool_call>")
    fake.responses.append(sse(({"role": "assistant", "content": body}, "stop")))
    engine.send("go")
    await wait_turn(engine)
    entry = engine.chat[-1]
    assert entry["text"] == "Checking the link." and entry["proposals"] == [1]
    assert engine.queue.get(1).command == "ethtool eth0"
    assert "text_tool_calls" in (engine.case.dir / "events.jsonl").read_text()
