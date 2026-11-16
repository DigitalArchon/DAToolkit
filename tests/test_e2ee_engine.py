"""Tinfoil and Chutes in the engine, the config and Settings: each provider recognised by its address
however it got into the settings (a preset, typed, an older config), the protocol it is spoken in, the
client each model gets (sealed ones never a plain client), the TEE guard kept to NanoGPT's TEE models
(a Chutes -TEE id is never asked of Chutes' address as one), the privacy level on every model list,
reply and the top bar, the move rule on a case's model change, and the Chutes key's three hosts."""

from __future__ import annotations

import asyncio
import json
import re

import httpx
import pytest

from datoolkit import creds
from datoolkit.config import Config, Provider
from datoolkit.engine import ChutesSealed, Engine, UserError
from datoolkit.llm import privacy, providers
from datoolkit.llm.anthropic_api import AnthropicClient
from datoolkit.llm.chutes_client import ChutesE2EEClient, ChutesState
from datoolkit.llm.client import LLMClient, detect_tier
from datoolkit.llm.private_mode import PrivateModeClient
from datoolkit.llm.secure_http import ProviderError
from datoolkit.llm.tinfoil_direct import Listing, TinfoilDirectClient, TinfoilState

from test_chutes_client import CHUTE, MODEL as CHUTES_TEE, FakeChutes, UP_TO_DATE  # noqa: F401
from test_tinfoil_direct import MODELS as TINFOIL_MODELS

TINFOIL = privacy.TINFOIL_BASE_URL
CHUTES = privacy.CHUTES_BASE_URL
CLAUDE_CHUTES = privacy.CHUTES_ANTHROPIC_URL
NANO = "https://nano-gpt.com/api/v1"
ORDINARY = "plain/Model"


def engine_with(tmp_path, *provs: Provider) -> Engine:
    engine = Engine(Config(providers=list(provs)), lambda e: None, tmp_path / "rt", save_config=lambda c: None)
    for p in provs:
        creds.set_secret("provider", p.name, f"key-{p.name.replace(' ', '-')}")
        p.key_origin = privacy.key_origin(p.base_url)
    return engine


# --- recognised by the address, however it got there ---------------------------------------


@pytest.mark.parametrize("url, api", [
    (TINFOIL, ""), (TINFOIL, "openai"), (TINFOIL, "anthropic"), ("https://inference.tinfoil.sh", ""),
    ("https://INFERENCE.tinfoil.sh./v1", "openai"), ("https://router-0.tinfoil.sh/v1", ""),
])
def test_tinfoil_is_recognised_whatever_its_api_field_says(tmp_path, url, api):
    prov = Provider("T Lab", url, api=api, tier_overrides={"glm-5-3": "standard"})
    engine = engine_with(tmp_path, prov)
    assert providers.describe(url, api) == {"kind": "tinfoil", "kind_label": "Tinfoil", "api": "tinfoil"}
    assert engine._tier(prov, "glm-5-3") == "e2ee", "an override never makes a sealed model less private"
    assert engine._sealed(prov, "glm-5-3") and not engine._tee_model(prov, "glm-5-3")
    assert isinstance(engine._client(prov, "glm-5-3"), TinfoilDirectClient)
    assert isinstance(engine._client(prov), TinfoilDirectClient), "its listing too: never a plain client"


@pytest.mark.parametrize("url, plain", [(CHUTES, LLMClient), (CLAUDE_CHUTES, AnthropicClient),
                                        ("https://llm.chutes.ai", LLMClient)])
def test_chutes_tee_models_get_the_sealed_client_and_its_others_the_plain_one(tmp_path, url, plain):
    prov = Provider("C Lab", url)
    engine = engine_with(tmp_path, prov)
    assert providers.describe(url)["kind"] == "chutes"
    for written in (CHUTES_TEE, " test/model-tee", "test/Model-TEE:THINKING"):
        client = engine._client(prov, written)
        assert isinstance(client, ChutesSealed) and client.inner.key_origin == privacy.key_origin(url)
        assert engine._tier(prov, written) == "tee", "partial privacy: as NanoGPT's TEE/ models"
        assert engine._sealed(prov, written) and not engine._tee_model(prov, written)
    assert isinstance(engine._client(prov, ORDINARY), plain)
    assert engine._tier(prov, ORDINARY) == "standard" and not engine._sealed(prov, ORDINARY)


def test_a_chutes_tee_id_is_never_a_nanogpt_tee_model(tmp_path):
    """DA Vibe Manager's trap: the TEE patterns alone read a Chutes -TEE id as NanoGPT's TEE model,
    and its attestation was asked of llm.chutes.ai, with the key."""
    assert detect_tier(CHUTES_TEE, CHUTES) == "tee"
    assert privacy.privacy_at(CHUTES_TEE, CHUTES).service == privacy.CHUTES
    prov = Provider("C Lab", CHUTES)
    engine = engine_with(tmp_path, prov)
    engine._tee_factory = lambda *a, **k: pytest.fail("a TeeClient was made for a Chutes model")
    assert asyncio.run(engine._tee_guard(prov, CHUTES_TEE, "chat")) is None
    nano = Provider("N Lab", NANO)
    assert engine_with(tmp_path, nano)._tee_model(nano, "TEE/glm-5.3"), "NanoGPT's TEE models as before"
    assert detect_tier("private/glm-5-3", NANO) == "e2ee" and detect_tier("TEE/glm-5.3", NANO) == "tee"


def test_claude_chutes_takes_the_key_as_a_bearer_token_and_anthropic_its_own_header(tmp_path):
    prov, anth = Provider("C Lab", CLAUDE_CHUTES), Provider("A Lab", "https://api.anthropic.com")
    engine = engine_with(tmp_path, prov, anth)
    chutes = engine._client(prov, ORDINARY)._client
    assert chutes.auth_token == "key-C-Lab" and chutes.api_key is None
    assert chutes.auth_headers == {"Authorization": "Bearer key-C-Lab"}
    official = engine._client(anth, "claude-opus-5-5")._client
    assert official.api_key == "key-A-Lab" and official.auth_token is None
    assert official.auth_headers == {"X-Api-Key": "key-A-Lab"}


async def test_no_plain_client_sends_a_sealed_model_in_the_clear():
    for client in (LLMClient(CHUTES, "k"), LLMClient(TINFOIL, "k")):
        with pytest.raises(ProviderError, match="nothing was sent"):
            async for _ in client.stream(CHUTES_TEE, [{"role": "user", "content": "x"}]):
                pass
        with pytest.raises(ProviderError, match="nothing was sent"):
            await client.complete(CHUTES_TEE, [{"role": "user", "content": "x"}])
    with pytest.raises(ProviderError, match="end-to-end encrypted endpoint"):
        await LLMClient(TINFOIL, "k").list_models()
    with pytest.raises(ProviderError, match="one of Chutes' TEE models"):
        async for _ in AnthropicClient(CLAUDE_CHUTES, "k").stream(CHUTES_TEE, [{"role": "user", "content": "x"}]):
            pass


def test_saving_a_provider_sets_its_api_by_the_address(tmp_path):
    engine = engine_with(tmp_path)
    engine.save_provider({"name": "T Lab", "base_url": TINFOIL, "api": "openai"}, api_key="tk")
    engine.save_provider({"name": "C Lab", "base_url": CLAUDE_CHUTES, "api": ""}, api_key="ck")
    engine.save_provider({"name": "X Lab", "base_url": "https://router.example/v1", "api": "tinfoil"}, api_key="xk")
    t, c, x = (engine.cfg.provider(n) for n in ("T Lab", "C Lab", "X Lab"))
    assert t.api == "" and engine._api(t) == "tinfoil", "Tinfoil's host is spoken in its protocol"
    assert c.api == "" and engine._api(c) == "anthropic"
    assert x.api == "tinfoil" and engine._kind(x) == "tinfoil"
    view = {p["name"]: p for p in engine._config_view()["providers"]}
    assert (view["T Lab"]["kind"], view["T Lab"]["api_effective"]) == ("tinfoil", "tinfoil")
    assert (view["C Lab"]["kind"], view["C Lab"]["api_effective"]) == ("chutes", "anthropic")
    with pytest.raises(UserError, match="The API must be one of openai, anthropic, tinfoil"):
        engine.save_provider({"name": "Y Lab", "base_url": TINFOIL, "api": "chutes-e2ee"}, api_key="k")


def test_the_presets_offer_tinfoil_and_chutes_at_their_own_addresses():
    found = {kind: (url, api) for kind, _label, _name, url, api in providers.PRESETS}
    assert found["tinfoil"] == (TINFOIL, "") and found["chutes"] == (CHUTES, "")


def test_a_chutes_key_goes_between_chutes_own_hosts_and_nowhere_else(tmp_path):
    prov = Provider("C Lab", CLAUDE_CHUTES, key_origin="https://llm.chutes.ai:443")
    assert Engine._key_problem(prov) is None
    prov.base_url = "https://evil.example/v1"
    assert "wasn't sent there" in Engine._key_problem(prov)
    tinfoil = Provider("T Lab", "https://router-0.tinfoil.sh/v1", key_origin=privacy.TINFOIL_ORIGIN)
    assert "wasn't sent there" in Engine._key_problem(tinfoil), "Tinfoil's key: its one origin"


# --- the privacy level, wherever a model is chosen or marked --------------------------------


async def test_every_model_listed_says_how_private_it_is(tmp_path):
    tin, chu = Provider("T Lab", TINFOIL), Provider("C Lab", CHUTES)
    engine = engine_with(tmp_path, tin, chu)
    engine._sealed_states["T Lab"] = TinfoilState(TINFOIL, listing=Listing(TINFOIL, client=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=TINFOIL_MODELS)))))
    fake = FakeChutes()
    engine._caps_http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    listed = {m["id"]: m for m in await engine.list_models("T Lab")}
    glm = listed["glm-5-3"]
    assert glm["tier"] == "e2ee" and glm["privacy"]["level"] == "highest" and glm["privacy"]["service"] == "tinfoil"
    assert glm["efforts"] == ["low", "high", "max"] and glm["context"] == 1048576
    listed = {m["id"]: m for m in await engine.list_models("C Lab")}
    assert listed[CHUTES_TEE]["privacy"]["level"] == "partial" and listed[CHUTES_TEE]["tier"] == "tee"
    assert listed[ORDINARY]["privacy"]["level"] == "none"
    assert listed[ORDINARY]["privacy"]["note"].startswith("Not private. Chutes reads")
    assert all("authorization" not in r.headers for r in fake.requests), "Chutes' list read without the key"
    engine.select_model("C Lab", CHUTES_TEE)
    active = engine._active_view()
    assert active["kind"] == "chutes" and active["privacy"]["level"] == "partial"
    if engine._attest_task:
        engine._attest_task.cancel()


async def test_a_case_moves_only_to_a_model_at_its_level_or_higher(tmp_path):
    nano, tin, chu = Provider("N Lab", NANO), Provider("T Lab", TINFOIL), Provider("C Lab", CHUTES)
    engine = engine_with(tmp_path, nano, tin, chu)
    engine._start_attestation = lambda *a, **k: None
    engine.new_case("client X", "open")
    assert engine._kept_privacy() == privacy.NONE, "no reply yet: kept to nothing"
    engine.chat.append({"kind": "assistant", "text": "x", "model": "private/glm-5-3",
                        **engine._privacy_view(nano, "private/glm-5-3")})
    with pytest.raises(UserError) as refused:
        engine.select_model("C Lab", CHUTES_TEE)
    assert str(refused.value) == (
        f"{CHUTES_TEE} is end-to-end encrypted through Chutes, but has partial privacy, and this case is kept to "
        "the highest privacy: choose one of NanoGPT's private/ models or Tinfoil's, or start a new case.")
    with pytest.raises(UserError, match="isn't end-to-end encrypted"):
        engine.select_model("N Lab", "z-ai/glm-5.3")
    engine.select_model("T Lab", "glm-5-3")          # Tinfoil: the highest privacy too
    assert engine.cfg.active_provider == "T Lab"
    # a reply under Chutes' partial privacy: Chutes, NanoGPT's TEE/, or up
    engine.chat.append({"kind": "assistant", "text": "y", **engine._privacy_view(chu, CHUTES_TEE)})
    engine.select_model("N Lab", "TEE/glm-5.3")
    with pytest.raises(UserError, match="kept to partial privacy"):
        engine.select_model("C Lab", ORDINARY)
    listed = engine._move_problem(chu, ORDINARY)
    assert listed and "start a new case" in listed


async def test_a_case_opened_under_a_less_private_model_sends_nothing(tmp_path):
    nano, chu = Provider("N Lab", NANO), Provider("C Lab", CHUTES)
    engine = engine_with(tmp_path, nano, chu)
    engine._start_attestation = lambda *a, **k: None
    engine.select_model("C Lab", ORDINARY)
    engine.new_case("client Y", "open")
    engine.chat.append({"kind": "assistant", "text": "x", **engine._privacy_view(nano, "private/glm-5-3")})
    with pytest.raises(UserError, match="^Nothing was sent. plain/Model isn't end-to-end encrypted"):
        engine._require_model()


async def test_a_confidential_case_allows_the_highest_privacy_and_not_chutes_partial(tmp_path):
    tin, chu = Provider("T Lab", TINFOIL), Provider("C Lab", CHUTES)
    engine = engine_with(tmp_path, tin, chu)
    engine._start_attestation = lambda *a, **k: None
    engine.new_case("client Z", "confidential")
    engine.select_model("T Lab", "glm-5-3")
    with pytest.raises(UserError, match="CONFIDENTIAL: TEE-tier"):
        engine.select_model("C Lab", CHUTES_TEE)


# --- a turn on Chutes' -TEE model, through the engine -------------------------------------------


async def test_a_turn_on_a_chutes_tee_model_is_sealed_attested_and_marked(tmp_path, monkeypatch):
    fake = FakeChutes()
    chu = Provider("C Lab", CHUTES)
    engine = engine_with(tmp_path, chu)
    state = ChutesState()
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))

    def client(prov, model=""):
        assert privacy.chutes_tee(model, prov.base_url), "only the -TEE model in this test"
        return ChutesSealed(ChutesE2EEClient(prov.base_url, "cpk_test", client=http, checks_client=http,
                                             intel=lambda raw, s: UP_TO_DATE, nvidia=lambda a, n, s: 1, state=state,
                                             stream_transport=httpx.MockTransport(fake.handler)))

    monkeypatch.setattr(engine, "_client", client)
    engine._tee_factory = lambda *a, **k: pytest.fail("a TeeClient was made for a Chutes model")
    await engine.start()
    try:
        engine.new_case("client C", "open")
        engine.select_model("C Lab", CHUTES_TEE)
        engine.send("Open the door.")
        for _ in range(300):
            if not engine.busy and (engine.attestation or {}).get("status") == "verified" and engine.chat[-1].get("text"):
                break
            await asyncio.sleep(0.01)
        entry = engine.chat[-1]
        assert entry["text"] == "The door opens."
        assert entry["tier"] == "tee" and entry["privacy"]["level"] == "partial"
        assert entry["privacy"]["service"] == "chutes" and entry["sealed"]
        assert "tee_signature" not in entry, "no NanoGPT reply check for a Chutes model"
        att = engine.attestation
        assert att["service"] == "chutes" and att["headline"] == "E2EE attested (Chutes)" and att["level"] == "partial"
        assert fake.count("/e2e/invoke") == 1 and not [r for r in fake.requests if r.url.path.startswith("/tee/")]
        log = (engine.case.dir / "events.jsonl").read_text()
        assert "enclave_attested" in log and "cpk_test" not in log
        assert engine._kept_privacy() == privacy.Privacy("sealed", privacy.CHUTES)
        json.dumps(engine.snapshot())
    finally:
        await engine.stop()


def test_a_private_model_keeps_its_own_client(tmp_path):
    nano = Provider("N Lab", NANO)
    engine = engine_with(tmp_path, nano)
    assert isinstance(engine._client(nano, "private/glm-5-3"), PrivateModeClient)
    assert isinstance(engine._client(nano, "TEE/glm-5.3"), LLMClient)


# --- the helpers: nothing of a case goes to a model less private than it was answered with ----------


def kept_highest(tmp_path, *extra: Provider) -> tuple[Engine, Provider]:
    tin = Provider("T Lab", TINFOIL)
    engine = engine_with(tmp_path, tin, *extra)
    engine._start_attestation = lambda *a, **k: None
    engine.new_case("client H", "open")
    engine.chat.append({"kind": "assistant", "text": "x", **engine._privacy_view(tin, "glm-5-3")})
    engine.cfg.active_provider, engine.cfg.active_model = "T Lab", "glm-5-3"
    return engine, tin


HELD_WORDS = ("has no privacy (not end-to-end encrypted or TEE: not private), and this case is kept to the highest "
        "privacy, so nothing of the case goes to it: choose a ")
HELD = re.escape(HELD_WORDS)


async def test_the_vision_helper_less_private_than_the_case_gets_no_image(tmp_path):
    nano = Provider("N Lab", NANO, vision_overrides={"z-ai/vision": "yes"})
    engine, tin = kept_highest(tmp_path, nano)
    tin.vision_overrides = {"glm-5-3": "no"}
    engine.cfg.settings.vision_model = "N Lab|z-ai/vision"
    status = engine.vision_status()
    assert status["mode"] == "none" and f"The vision helper z-ai/vision {HELD_WORDS}vision helper at that level" in status["why"]
    with pytest.raises(UserError, match="The vision helper z-ai/vision has no privacy"):
        await engine._describe_image("a.png", "data:image/png;base64,AAAA", "")
    engine.cfg.settings.vision_model = "N Lab|private/vision"
    nano.vision_overrides["private/vision"] = "yes"
    assert engine.vision_status()["mode"] == "helper", "a helper at the case's level"


async def test_the_reviewer_less_private_than_the_case_reviews_nothing(tmp_path):
    nano = Provider("N Lab", NANO)
    engine, _tin = kept_highest(tmp_path, nano)
    engine.cfg.settings.review_model = "N Lab|z-ai/glm-5.3"
    for auto in (False, True):
        with pytest.raises(UserError, match=f"The reviewer z-ai/glm-5.3 {HELD}"):
            engine._review_target(auto)
    engine.cfg.settings.review_model = "N Lab|TEE/glm-5.3"
    with pytest.raises(UserError, match="has partial privacy .* kept to the highest privacy"):
        engine._review_target()
    engine.cfg.settings.review_model = "N Lab|private/glm-5-3"
    assert engine._review_target()[1] == "private/glm-5-3"


async def test_the_research_model_less_private_than_the_case_gets_no_brief(tmp_path):
    nano = Provider("N Lab", NANO)
    engine, _tin = kept_highest(tmp_path, nano)
    engine.cfg.settings.research_model = "N Lab|anthropic/claude-sonnet-5.5"
    with pytest.raises(UserError, match=f"The research model anthropic/claude-sonnet-5.5 {HELD}"):
        await engine._research_target()
    engine._models["N Lab"] = ["anthropic/claude-sonnet-5.5"]
    engine.cfg.settings.research_model = ""
    engine._search_provider = lambda: nano
    with pytest.raises(UserError, match="research model anthropic/claude-sonnet-5.5 has no privacy"):
        await engine._research_target()


async def test_compaction_and_write_ups_go_only_to_the_chat_model_at_the_cases_level(tmp_path):
    nano = Provider("N Lab", NANO)
    engine, _tin = kept_highest(tmp_path, nano)
    engine.conv = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"},
                   {"role": "user", "content": "z"}, {"role": "assistant", "content": "w"}]
    engine.cfg.active_provider, engine.cfg.active_model = "N Lab", "z-ai/glm-5.3"
    with pytest.raises(UserError, match="^Nothing was sent. z-ai/glm-5.3 isn't end-to-end encrypted"):
        await engine.ticket_summary()
    with pytest.raises(UserError, match="^Nothing was sent"):
        await engine._compact_preview(0)


def test_a_request_held_after_an_await_is_refused_once_the_case_was_answered_more_privately(tmp_path):
    nano = Provider("N Lab", NANO)
    engine, _tin = kept_highest(tmp_path, nano)
    with pytest.raises(UserError, match=f"The model z-ai/glm-5.3 {HELD}"):
        engine._still_open(engine.case, nano, "z-ai/glm-5.3")
    assert engine._still_open(engine.case, nano, "private/glm-5-3") == "e2ee"


async def test_a_tinfoil_or_chutes_provider_lists_its_models_before_a_key_is_saved(tmp_path):
    engine = Engine(Config(providers=[Provider("T Lab", TINFOIL), Provider("C Lab", CLAUDE_CHUTES)]), lambda e: None,
                    tmp_path / "rt", save_config=lambda c: None)
    engine._sealed_states["T Lab"] = TinfoilState(TINFOIL, listing=Listing(TINFOIL, client=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=TINFOIL_MODELS)))))
    engine._caps_http = httpx.AsyncClient(transport=httpx.MockTransport(FakeChutes().handler))
    assert "glm-5-3" in {m["id"] for m in await engine.list_models("T Lab")}
    assert CHUTES_TEE in {m["id"] for m in await engine.list_models("C Lab")}
