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


def _chunk(*tool_deltas, content=None):
    from types import SimpleNamespace as N
    calls = [N(index=i, id=cid, function=N(name=name, arguments=args)) for i, cid, name, args in tool_deltas]
    return N(id="r1", usage=None, model_extra={}, choices=[N(finish_reason=None, delta=N(content=content, tool_calls=calls, model_extra={}))])


def test_the_stream_keeps_calls_apart_however_a_route_numbers_them():
    from datoolkit.llm.client import StreamAccumulator
    acc = StreamAccumulator()
    acc.feed(_chunk((0, "a", "propose_commands", '{"items": []}')))
    acc.feed(_chunk((0, "b", "update_hypotheses", '{"items": []}')))     # a second call, numbered 0 again
    acc.feed(_chunk((None, None, None, "")))                               # no index: continues the last one
    calls = acc.finish().tool_calls
    assert [(c.id, c.name, c.arguments) for c in calls] == [("a", "propose_commands", '{"items": []}'),
                                                            ("b", "update_hypotheses", '{"items": []}')]
    acc = StreamAccumulator()
    acc.feed(_chunk((0, None, "propose_commands", '{"it')))
    acc.feed(_chunk((0, None, "propose_commands", 'ems": []}')))           # the whole name in every piece
    (call,) = acc.finish().tool_calls
    assert call.name == "propose_commands" and call.arguments == '{"items": []}'
    assert call.id.startswith("call_")


def test_ids_made_up_for_calls_never_repeat():
    from datoolkit.llm.client import StreamAccumulator
    from datoolkit.llm.textcalls import _call
    made = set()
    for _ in range(3):
        acc = StreamAccumulator()
        acc.feed(_chunk((0, None, "propose_commands", "{}")))
        made.add(acc.finish().tool_calls[0].id)
        made.add(_call("propose_commands", {}, 0).id)
    assert len(made) == 6
