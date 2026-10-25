"""Routes (llm/routing.py): which of NanoGPT's or OpenRouter's hosts serves a model, what the
request carries for it, and whether the bill says it held."""


import pytest

from datoolkit.config import Config, Provider
from datoolkit.engine import Engine, UserError
from datoolkit.llm import routing
from datoolkit.llm.client import StreamAccumulator, _split_params

NANO = "https://nano-gpt.com/api/v1"


def engine_with(tmp_path, *provs):
    return Engine(Config(providers=list(provs)), lambda e: None, tmp_path / "rt", save_config=lambda c: None)


def test_route_bodies():
    assert routing.body("nanogpt", {"priority": "latency", "fp8": True}) == {
        "provider": {"sort": "latency", "min_quantization": "fp8"}}
    assert routing.body("nanogpt", {"priority": "host", "host": "deepinfra", "fp8": False}) == {
        "provider": {"order": ["deepinfra"]}}
    assert routing.body("openrouter", {"priority": "speed", "fp8": True}) == {
        "provider": {"sort": "throughput", "quantizations": ["fp8", "fp16", "bf16", "fp32"]}}
    assert routing.body("nanogpt", None) == {} and routing.body("openai-compatible", {"priority": "price"}) == {}
    with pytest.raises(ValueError):
        routing.validate({"priority": "host", "host": "x"}, "openrouter")      # OpenRouter: priorities only
    assert routing.label("nanogpt", None).startswith("NanoGPT's own routing")
    assert routing.label("nanogpt", {"priority": "host", "host": "x", "host_name": "DeepInfra", "fp8": True}) == "DeepInfra · FP8+"


def test_routes_never_apply_to_private_or_tee_models():
    assert routing.routable("nanogpt", "zai/glm-5.3", "standard")
    assert not routing.routable("nanogpt", "private/glm-5-3", "e2ee")
    assert not routing.routable("nanogpt", "TEE/glm-5.3", "tee")
    assert not routing.routable("anthropic", "claude-opus-5-5", "standard")


def test_the_route_goes_with_every_request_for_that_model(tmp_path):
    engine = engine_with(tmp_path, Provider("NanoGPT", NANO))
    engine.set_route("NanoGPT", "zai/glm-5.3", {"priority": "price", "fp8": True})
    params = engine._params(engine.cfg.provider("NanoGPT"), "zai/glm-5.3")
    assert params["provider"] == {"sort": "price", "min_quantization": "fp8"}
    assert _split_params(params)["extra_body"]["provider"] == params["provider"]
    assert "provider" not in engine._params(engine.cfg.provider("NanoGPT"), "moonshotai/kimi-k3")
    with pytest.raises(UserError, match="never to private"):
        engine.set_route("NanoGPT", "private/glm-5-3", {"priority": "price"})
    engine.set_route("NanoGPT", "zai/glm-5.3", None)
    assert engine.cfg.provider("NanoGPT").routes == {}


def test_the_bill_says_whether_the_route_held():
    route = {"priority": "host", "host": "h", "fp8": True, "price": {"input": 1.0, "output": 2.0}}
    assert routing.check("nanogpt", route, {"cost": 0})["met"] is False
    assert routing.check("nanogpt", route, {})["met"] is None
    ok = routing.check("nanogpt", route, {"cost": 0.003, "prompt_tokens": 1000, "completion_tokens": 1000})
    assert ok["met"] is True
    other = routing.check("nanogpt", route, {"cost": 0.03, "prompt_tokens": 1000, "completion_tokens": 1000})
    assert other["met"] is False and "another host" in other["why"]
    assert routing.check("nanogpt", None, {"cost": 1}) is None


def test_nanogpts_pricing_record_fills_a_missing_cost():
    from openai.types.chat import ChatCompletionChunk
    acc = StreamAccumulator()
    acc.feed(ChatCompletionChunk.model_validate({"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
                                                 "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                                                          "total_tokens": 2},
                                                 "x_nanogpt_pricing": {"cost": 0.0042}}))
    assert acc.finish().usage["cost"] == 0.0042


def test_hosts_listing_is_parsed():
    data = {"supportsProviderSelection": True, "autoTps": 50, "autoTtftMs": 900, "officialBaseline": {"provider": "zai"},
            "providers": [{"provider": "zai", "displayName": "Z.ai", "quantization": "fp8", "tps": 60, "ttftMs": 700,
                           "privacy": {"classification": "zdr"}, "supportsPromptCaching": True,
                           "pricing": {"inputPer1kTokens": 0.0006, "outputPer1kTokens": 0.0022}},
                          {"provider": "cheap", "quantization": "fp4", "available": False}, "junk"]}
    hosts = routing.parse_hosts("zai/glm-5.3", data).to_dict()
    assert hosts["official"] == "zai" and hosts["supported"] is True
    z, cheap = hosts["hosts"]
    assert z["fp8_or_better"] and z["privacy_label"] == "No retention" and z["input_price"] == pytest.approx(0.6)
    assert not cheap["fp8_or_better"] and cheap["available"] is False
    assert routing.hosts_url(NANO, "zai/glm-5.3") == "https://nano-gpt.com/api/models/zai%2Fglm-5.3/providers"
