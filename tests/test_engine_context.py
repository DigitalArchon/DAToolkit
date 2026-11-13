"""What the engine reports and records about a conversation: a photo's size in the context view,
which message a compaction stops before, what the request log holds, when a case looks for
similar ones, and whose words a write-up takes each message for."""

import base64
import json
import os

from datoolkit.engine import IMAGE_TOKENS, PROMISE_NUDGE
from datoolkit.export import full_transcript
from datoolkit.llm import prompts

from test_engine import env, wait_turn  # noqa: F401
from test_models import _png, _text_model_with_helper
from test_research import say
from test_revise_search import setup


async def test_a_photo_counts_as_a_fixed_number_of_tokens_not_by_its_file_size(env):  # noqa: F811
    engine, _, _ = env
    setup(engine)
    url = "data:image/jpeg;base64," + base64.b64encode(os.urandom(300_000)).decode()
    engine.conv = [{"role": "user", "content": [{"type": "text", "text": "what does this screen say?"},
                                                {"type": "image_url", "image_url": {"url": url}}]},
                   {"role": "assistant", "content": "It says the disk is full."}]
    group = engine.context_view()["groups"][0]
    assert IMAGE_TOKENS < group["tokens"] < IMAGE_TOKENS + 100           # it showed as 100,000
    engine.conv[0]["content"].append({"type": "image_url", "image_url": {"url": url}})
    assert 2 * IMAGE_TOKENS < engine.context_view()["groups"][0]["tokens"] < 2 * IMAGE_TOKENS + 100


async def test_compaction_names_its_boundary_by_position_as_well_as_by_how_it_begins(env):  # noqa: F811
    engine, fake, _ = env
    setup(engine)
    for text in ("web01 is slow", "ok", "it was the backup job", "ok", "thanks"):
        fake.responses.append(say("noted"))
        engine.send(text)
        await wait_turn(engine)
    fake.responses.append(say("Summary."))
    await engine.compact_preview(2)                      # exchanges 1 to 3; the 4th begins "ok", as the 2nd does
    instruction = fake.requests[-1]["messages"][-1]["content"]
    assert "before message 4 of the 5 that came to you from the technician's side" in instruction
    assert 'the one that begins "ok"' in instruction


async def test_the_request_log_holds_the_description_a_text_only_model_was_sent(env):  # noqa: F811
    engine, fake, _ = env
    _text_model_with_helper(engine)
    engine.save_settings({"vision_model": "Fake|anthropic/claude-opus-5.5"})
    fake.completions.append("Menu: 0) Logout  8) Shell.")
    fake.responses.append(say("Press 8."))
    engine.send("what is this", images=[_png()])
    await wait_turn(engine)
    logged = [json.loads(line) for line in (engine.case.dir / "requests.jsonl").read_text().splitlines()]
    chat = next(r for r in logged if r["purpose"] == "chat")
    parts = chat["messages"][0]["content"]
    assert all(p["type"] == "text" for p in parts)                       # it was logged as the image the model never got
    assert "8) Shell" in parts[1]["text"] and "vision model (anthropic/claude-opus-5.5)" in parts[1]["text"]
    helper = next(r for r in logged if r["purpose"] == "describe_image")
    assert helper["messages"][0]["content"][1] == {"type": "image", "file": "img-1.png"}      # the helper did get it


async def test_a_case_begun_without_words_looks_for_similar_cases_at_its_first_words(env):  # noqa: F811
    engine, fake, _ = env
    engine.cfg.clients = ["Acme"]
    engine.new_case("Acme NAS slow SMB transfers", "open", client="Acme")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    fake.responses.append(say("ok"))
    engine.send("NAS transfers over SMB are slow since the switch swap")
    await wait_turn(engine)
    (engine.case.dir / "runbook.md").write_text("## Symptoms\nSlow SMB\n## Fix\nSet MTU 1500 on the NAS uplink\n")
    engine.new_case("Acme NAS slow again", "open", client="Acme", history=True)
    fake.responses.append(say("I see a NAS."))
    engine.send("", images=[_png()])                                     # a photo alone: nothing to search with
    await wait_turn(engine)
    assert engine._similar == [] and "MTU 1500" not in fake.requests[-1]["messages"][0]["content"]
    fake.responses.append(say("ok"))
    engine.send("NAS slow SMB transfers after a switch swap")            # the first words
    await wait_turn(engine)
    assert "MTU 1500" in fake.requests[-1]["messages"][0]["content"] and engine._similar
    assert any("Similar past cases of Acme" in e["text"] for e in engine.chat if e["kind"] == "note")
    searched = len([e for e in engine.chat if e["kind"] == "note" and "Similar past cases" in e["text"]])
    fake.responses.append(say("ok"))
    engine.send("NAS slow SMB transfers, still")                         # later messages don't look again
    await wait_turn(engine)
    assert len([e for e in engine.chat if e["kind"] == "note" and "Similar past cases" in e["text"]]) == searched


async def test_a_write_up_is_told_which_messages_are_the_apps_and_that_exchanges_are_missing(env):  # noqa: F811
    engine, fake, _ = env
    setup(engine)
    for text in ("web01 is slow", "load average is 12", "it's the backup job"):
        fake.responses.append(say("noted"))
        engine.send(text)
        await wait_turn(engine)
    whole = engine._transcript_for_model()
    assert whole.startswith("TECHNICIAN:\nweb01 is slow") and "DA TOOLKIT" not in whole
    engine.drop_context([1])
    engine.conv.append({"role": "user", "content": PROMISE_NUDGE})       # as a turn leaves it
    text = engine._transcript_for_model()
    assert text.startswith("NOTE FROM DA TOOLKIT: this transcript is not the whole case: 1 exchange(s) were removed")
    assert "TECHNICIAN:\n" + prompts.DROPPED_NOTE not in text and "TECHNICIAN:\n[DAToolkit]" not in text
    assert "DA TOOLKIT (a note to the AI, not the technician's words):\n[DAToolkit] Your message says" in text
    assert "DA TOOLKIT (not the technician): an exchange that stood here was removed" in text
    assert text.count("TECHNICIAN:") == 2
    engine.conv.pop()
    fake.responses.append(say("Problem: web01 slow."))
    r = await engine.compact_preview(0)
    engine.compact_apply(r["summary"])
    text = engine._transcript_for_model()
    assert "its earlier exchanges were replaced by a summary" in text.split("\n\n")[0]
    assert "the exchanges before this point were replaced by the summary below" in text and "Problem: web01 slow." in text
    # and it is what the write-up's model is sent
    fake.completions.append("Ticket.")
    await engine.ticket_summary()
    assert fake.requests[-1]["messages"][1]["content"].startswith("Case: c\n\nNOTE FROM DA TOOLKIT: this transcript is not")


def test_the_full_export_names_the_apps_own_messages():
    conv = [{"role": "user", "content": "web01 is slow"}, {"role": "assistant", "content": "I'll check."},
            {"role": "user", "content": PROMISE_NUDGE}, {"role": "user", "content": prompts.DROPPED_NOTE}]
    md = full_transcript({"name": "c", "id": "c1"}, [], conv, [], "sys")
    assert md.count("**Technician → AI**") == 1 and md.count("**DA Toolkit → AI**") == 2
