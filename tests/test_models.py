"""Model capabilities (vision, reasoning), generation settings, the vision helper, and retry."""

import asyncio
import base64
from io import BytesIO

import pytest
from PIL import Image

from datoolkit.config import Provider
from datoolkit.engine import UserError
from datoolkit.llm import capabilities, params
from datoolkit.llm.private_mode import shape_body

from test_engine import env, sse, wait_turn  # noqa: F401

DETAILED = [
    {"id": "moonshotai/kimi-k3", "capabilities": {"vision": True, "reasoning": True, "tool_calling": True},
     "reasoning_efforts": ["low", "high", "max"], "context_length": 1048576, "max_output_tokens": None},
    {"id": "TEE/kimi-k3", "capabilities": {"vision": True, "reasoning": True}, "reasoning_efforts": ["low", "high", "max"]},
    {"id": "z-ai/glm-5.3", "capabilities": {"vision": False, "reasoning": True}, "reasoning_efforts": ["low", "high", "max"]},
    {"id": "anthropic/claude-opus-5.5", "capabilities": {"vision": True, "reasoning": True},
     "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"], "max_output_tokens": 128000},
]


def _png() -> str:
    buf = BytesIO()
    Image.new("RGB", (20, 10), (255, 255, 255)).save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------- capabilities & params

def test_private_models_borrow_their_public_twins_capabilities():
    caps = capabilities.parse(DETAILED)
    assert capabilities.lookup(caps, "private/kimi-k3")["vision"] is True
    assert capabilities.lookup(caps, "private/glm-5-3")["vision"] is False         # 5-3 matches 5.3
    assert capabilities.lookup(caps, "private/glm-5-3")["efforts"] == ["low", "high", "max"]
    assert capabilities.lookup(caps, "some/unknown") is None


def test_generation_settings_validate_and_shape_per_model():
    assert params.validate({"temperature": "0.3", "top_p": "", "max_tokens": "4000", "reasoning_effort": "low"}) == \
        {"temperature": 0.3, "max_tokens": 4000, "reasoning_effort": "low"}
    for bad in ({"temperature": 3}, {"reasoning_effort": "turbo"}, {"seed": "x"}):
        with pytest.raises(ValueError):
            params.validate(bad)
    assert params.nearest_effort("medium", ["low", "high", "max"]) == "low"     # tie: the cheaper one
    assert params.nearest_effort("xhigh", ["low", "high", "max"]) == "high"
    kimi = capabilities.lookup(capabilities.parse(DETAILED), "private/kimi-k3")
    assert params.for_model({"temperature": 0.3, "reasoning_effort": "medium"}, kimi) == {"temperature": 0.3, "reasoning_effort": "low"}
    # unknown capabilities: sampling only, never an effort a model might reject
    assert params.for_model({"temperature": 0.3, "reasoning_effort": "low"}, None) == {"temperature": 0.3}
    opus = capabilities.lookup(capabilities.parse(DETAILED), "anthropic/claude-opus-5.5")
    assert params.for_model({"max_tokens": 500000}, opus) == {"max_tokens": 128000}


def test_private_mode_body_carries_settings_and_glm_thinking_follows_effort():
    body = shape_body("private/glm-5-3", "glm-5-3", [{"role": "user", "content": "x"}], None, "s",
                      {"temperature": 0.3, "reasoning_effort": "none", "seed": 7})
    assert body["temperature"] == 0.3 and body["seed"] == 7 and body["chat_template_kwargs"] == {"thinking": False}
    body = shape_body("private/glm-5-3", "glm-5-3", [], None, "s", {"reasoning_effort": "low"})
    assert body["chat_template_kwargs"] == {"thinking": True} and body["reasoning_effort"] == "low"


async def test_requests_carry_the_generation_settings(env):  # noqa: F811
    engine, fake, _ = env
    engine.new_case("g", "open")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    engine._caps["Fake"] = capabilities.parse(DETAILED)
    engine.save_settings({"generation": {"temperature": 0.3, "reasoning_effort": "medium", "max_tokens": ""}})
    assert engine.cfg.settings.generation == {"temperature": 0.3, "reasoning_effort": "medium"}
    fake.responses.append(sse(({"role": "assistant", "content": "ok"}, "stop")))
    engine.send("hi")
    await wait_turn(engine)
    req = fake.requests[-1]
    assert req["temperature"] == 0.3 and req["reasoning_effort"] == "medium"
    with pytest.raises(UserError):
        engine.save_settings({"generation": {"temperature": 9}})


# ---------------------------------------------------------------- vision

def _text_model_with_helper(engine):
    """Chat with a text-only model; a second provider's model is the vision helper."""
    engine.cfg.providers[0].vision_overrides = {"text/model": "no", "anthropic/claude-opus-5.5": "yes"}
    engine.new_case("v", "open")
    engine.select_model("Fake", "text/model")


async def test_no_vision_and_no_helper_refuses_images(env):  # noqa: F811
    engine, fake, _ = env
    _text_model_with_helper(engine)
    v = engine.vision_status()
    assert v["mode"] == "none" and "can't read images" in v["why"] and "No vision helper" in v["why"]
    with pytest.raises(UserError, match="Images can't be sent"):
        engine.send("look", images=[_png()])
    engine.cfg.providers[0].vision_overrides.pop("text/model")
    assert "isn't known" in engine.vision_status()["why"]            # unknown is treated as no


async def test_vision_helper_describes_images_once_for_a_text_model(env):  # noqa: F811
    engine, fake, events = env
    _text_model_with_helper(engine)
    engine.save_settings({"vision_model": "Fake|anthropic/claude-opus-5.5"})
    v = engine.vision_status()
    assert v["mode"] == "helper" and v["helper"] == "anthropic/claude-opus-5.5"
    fake.completions.append("Menu: 0) Logout  8) Shell. Prompt: 'Enter an option:'")
    fake.responses.append(sse(({"role": "assistant", "content": "Press 8."}, "stop")))
    engine.send("what is this", images=[_png()])
    await wait_turn(engine)

    helper_req, chat_req = fake.requests[-2], fake.requests[-1]
    assert helper_req["model"] == "anthropic/claude-opus-5.5" and not helper_req.get("stream")
    assert helper_req["messages"][1]["content"][1]["type"] == "image_url"          # the helper sees the image
    user = chat_req["messages"][-1]["content"]
    assert chat_req["model"] == "text/model"
    assert all(p["type"] == "text" for p in user)                                  # the text model never gets it
    assert "8) Shell" in user[1]["text"] and "vision model (anthropic/claude-opus-5.5)" in user[1]["text"]
    assert engine.chat[-2]["image_notes"]["img-1.png"]["model"] == "anthropic/claude-opus-5.5"
    assert any(e["type"] == "delta" and e.get("name") == "describe_image" for e in events)
    # next turn: the description is reused, no second helper request
    n = len(fake.requests)
    fake.responses.append(sse(({"role": "assistant", "content": "ok"}, "stop")))
    engine.send("thanks")
    await wait_turn(engine)
    assert len(fake.requests) == n + 1
    assert "image_described" in (engine.case.dir / "events.jsonl").read_text()
    assert (engine.case.dir / "image-descriptions.json").exists()


async def test_helper_must_be_allowed_by_the_case(env):  # noqa: F811
    engine, fake, _ = env
    engine.cfg.providers.append(Provider("Local", "http://localhost:11434/v1", vision_overrides={"llama": "no"}))
    engine.cfg.settings.vision_model = "Fake|anthropic/claude-opus-5.5"      # a standard-tier helper
    engine.new_case("secret", "sovereign")
    engine.select_model("Local", "llama")
    v = engine.vision_status()
    assert v["mode"] == "none" and "isn't allowed here" in v["why"]


# ---------------------------------------------------------------- retry

async def test_retry_resends_the_stranded_message_with_the_new_model(env):  # noqa: F811
    engine, fake, _ = env
    engine.new_case("r", "open")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    assert not engine.can_retry
    engine.send("VPN is down")                     # no reply queued: the request fails
    await wait_turn(engine)
    assert engine.can_retry and engine.snapshot()["can_retry"]
    assert engine.chat[-1]["retry"] is True
    engine.select_model("Fake", "TEE/glm-5.3")     # the technician switches model
    fake.responses.append(sse(({"role": "assistant", "content": "Let's look."}, "stop")))
    engine.retry()
    await wait_turn(engine)
    req = fake.requests[-1]
    assert req["model"] == "TEE/glm-5.3"
    users = [m for m in req["messages"] if m["role"] == "user"]
    assert users == [{"role": "user", "content": "VPN is down"}]            # sent once, not twice
    assert engine.chat[-1]["text"] == "Let's look." and not engine.can_retry
    with pytest.raises(UserError, match="nothing to retry"):
        engine.retry()
    assert "Retrying the last message with TEE/glm-5.3" in [e.get("text") for e in engine.chat if e["kind"] == "note"][-1]


async def test_retry_after_stop_drops_the_partial_reply(env):  # noqa: F811
    engine, fake, _ = env
    engine.new_case("s", "open")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    engine.conv = [{"role": "user", "content": "hello"},
                   {"role": "assistant", "content": "Half an answ\n[response stopped by technician]"}]
    engine._last_turn_error = "stopped"
    assert engine.can_retry
    fake.responses.append(sse(({"role": "assistant", "content": "Full answer."}, "stop")))
    engine.retry()
    await wait_turn(engine)
    assert [m["role"] for m in fake.requests[-1]["messages"]] == ["system", "user"]
    await asyncio.sleep(0)
