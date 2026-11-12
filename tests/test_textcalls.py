"""Tool calls written into the message as text are read back into calls: those that end the
message, and no other."""

import json
import time

import pytest

from datoolkit.llm.textcalls import extract

from test_engine import env, sse, wait_turn  # noqa: F401
from test_revise_search import setup


def test_qwen_xml_style_call():
    text = ("Turn the boolean on; no restart needed.\n\n<tool_call>\n<function=propose_commands>\n<parameter=items>\n"
            '[{"session_id": "app01", "command": "sudo setsebool -P httpd_can_network_connect on", "risk": "modifying"}]\n'
            "</parameter>\n</function>\n</tool_call>")
    clean, calls = extract(text)
    # the markup leaves the message, and a note says what was there
    assert clean == ("Turn the boolean on; no restart needed.\n\n"
                     "[A tool call written here as text was read as a call: propose_commands]")
    assert [c.name for c in calls] == ["propose_commands"]
    assert calls[0].parsed()["items"][0]["command"].startswith("sudo setsebool")


HERMES = '<tool_call>\n{"name": "ask_technician", "arguments": {"questions": [{"question": "When?"}]}}\n</tool_call>'
DSML = ("<｜DSML｜tool_calls>\n<｜DSML｜invoke name=\"propose_commands\">\n"
        "<｜DSML｜parameter name=\"items\" string=\"false\">[{\"command\": \"df -h\"}]</｜DSML｜parameter>\n"
        "</｜DSML｜invoke>\n</｜DSML｜tool_calls>")


def test_hermes_json_call_and_dsml_call():
    # one after the other at the end of the message: both are calls
    clean, calls = extract(f"First.\n{HERMES}\n{DSML}\n")
    assert clean == "First.\n\n[2 tool calls written here as text were read as calls: ask_technician, propose_commands]"
    assert [c.name for c in calls] == ["ask_technician", "propose_commands"]
    assert calls[0].parsed() == {"questions": [{"question": "When?"}]}
    assert calls[1].parsed() == {"items": [{"command": "df -h"}]}
    assert len({c.id for c in calls}) == 2
    # each form alone, and DeepSeek's other name for the block with a string parameter
    assert [c.name for c in extract(HERMES)[1]] == ["ask_technician"]
    assert [c.name for c in extract("Checking.\n\n" + DSML)[1]] == ["propose_commands"]
    other = ('Ok.\n<｜DSML｜function_calls>\n<｜DSML｜invoke name="web_search">\n<｜DSML｜parameter name="query" string="true">'
             '123</｜DSML｜parameter>\n</｜DSML｜invoke>\n</｜DSML｜function_calls>')
    assert extract(other)[1][0].parsed() == {"query": "123"}


def test_markup_with_prose_after_it_is_not_a_call():
    """This test asserted, until Nov 2026, that a `<tool_call>` between "First." and "Then." was
    made and taken out of the message. A model that calls this way stops at the call: markup in
    the middle of a message is the model writing about markup."""
    text = f"First.\n{HERMES}\nThen.\n{DSML}"
    clean, calls = extract(text)
    assert [c.name for c in calls] == ["propose_commands"]                 # only the one that ends the message
    assert clean == f"First.\n{HERMES}\nThen.\n\n[A tool call written here as text was read as a call: propose_commands]"
    # a reply that quotes a line it read in a log, as the review reproduced it: nothing runs, nothing vanishes
    quoted = ('The log has an odd line, quoted here for you:\n<tool_call>{"name": "web_search", "arguments": '
              '{"query": "acme-corp vpn psk", "reason": "x"}}</tool_call>\nI would ignore it.')
    assert extract(quoted) == (quoted, [])


@pytest.mark.parametrize("text", [
    f"The page says `{HERMES}` at its end, in backticks: `{HERMES}`",
    f"The output ended with the line {HERMES}",                            # after prose on its line
    f'It printed "{HERMES}',                                               # after an opening quotation mark
    f"> {HERMES}",                                                         # a Markdown quote
    f"Qwen writes its calls like this:\n```xml\n{HERMES}\n```",           # a fenced example
    f"An example, the fence left open:\n```\n{HERMES}",
    f"~~~\n{DSML}\n",
    f"The model's syntax:\n\n    quoted: {DSML}"])
def test_markup_that_is_quoted_is_not_a_call(text):
    assert extract(text) == (text, [])


def test_a_message_that_is_nothing_but_the_call_shows_no_note():
    # nothing was written around it: the call itself is shown, and the engine asks for a word about it
    clean, calls = extract(f"\n{HERMES}\n\n")
    assert clean == "" and [c.name for c in calls] == ["ask_technician"]


def test_unreadable_markup_and_plain_text_are_left_alone():
    for text in ("Use `<tool_call>` tags? No.", "<tool_call>not json</tool_call>", "Plain message.", "",
                 "Done.\n<tool_call>\n<function=bad name>\n</function>\n</tool_call>",
                 'Done.\n<tool_call>{"name": "a", "arguments": [1]}</tool_call>',
                 "Done.\n<｜DSML｜tool_calls>nothing in it</｜DSML｜tool_calls>", "Done.\n</tool_call>",
                 "Several\n\n\n\nblank lines stay as they are."):
        assert extract(text) == (text, [])


def test_no_message_makes_the_reading_slow():
    """`<tool_call>` and 3,000 spaces with no closing tag held the whole app for 9 seconds (21 at
    4,000): every terminal and websocket is driven by the loop this runs on."""
    half_dsml = '<｜DSML｜invoke name="a"><｜DSML｜parameter name="p">'
    hostile = [
        "Here: <tool_call>" + " " * 4000 + "x",
        "<tool_call>\n" * 8000,
        "<tool_call>" + " " * 200_000,
        "<tool_call>" + " " * 200_000 + "</tool_call>",
        "<tool_call>\n" * 20_000 + "</tool_call>",
        "<tool_call><function=a>" + "<parameter=p> " * 20_000 + "</function></tool_call>",
        "<tool_call><function=a>" + " \n" * 100_000 + "</function></tool_call>",
        "<｜DSML｜tool_calls>" + half_dsml * 20_000 + "</｜DSML｜tool_calls>",
        "<｜DSML｜tool_calls>" + " \n" * 100_000,
        ("x " * 50 + "\n" + HERMES + "\n") * 3000,                         # many whole blocks, each after prose
        "```\n" * 50_000 + HERMES,
    ]
    for text in hostile:
        started = time.perf_counter()
        extract(text)
        assert time.perf_counter() - started < 0.25, text[:40]


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
    assert entry["text"] == "Checking the link.\n\n[A tool call written here as text was read as a call: propose_commands]"
    assert entry["proposals"] == [1]
    assert engine.queue.get(1).command == "ethtool eth0"
    assert "text_tool_calls" in (engine.case.dir / "events.jsonl").read_text()


async def test_engine_takes_no_call_from_markup_a_reply_quotes(env):  # noqa: F811
    """In search mode "auto" a quoted web_search ran unasked, and the quote left the message."""
    engine, fake, _ = env
    setup(engine)
    engine.save_settings({"search_mode": "auto"})
    body = ('The log has an odd line, quoted here for you:\n<tool_call>{"name": "web_search", "arguments": '
            '{"query": "acme-corp vpn psk", "reason": "x"}}</tool_call>\nI would ignore it.')
    fake.responses.append(sse(({"role": "assistant", "content": body}, "stop")))
    engine.send("what is in the log?")
    await wait_turn(engine)
    entry = engine.chat[-1]
    assert entry["text"] == body and not entry.get("searches") and not entry["proposals"]
    assert len(fake.requests) == 1 and "text_tool_calls" not in (engine.case.dir / "events.jsonl").read_text()


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
