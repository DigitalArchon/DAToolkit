"""What a provider leaves behind when it is deleted: nothing learned or set for it passes to a new
one of its name."""

import pytest

from datoolkit.config import Config, Provider
from datoolkit.engine import Engine, UserError

from test_engine import env  # noqa: F401

# ---------------------------------------------------------------- a deleted provider's leftovers

async def test_a_deleted_provider_leaves_nothing_to_a_new_one_of_its_name(env):  # noqa: F811
    engine, _, _ = env
    engine.save_provider({"name": "Other", "base_url": "https://other.example/v1"})
    await engine.list_models("Fake")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    engine.save_settings({"review_model": "Fake|TEE/glm-5.3", "vision_model": "Fake|anthropic/claude-opus-5.5",
                          "research_model": "Fake|anthropic/claude-opus-5.5", "search_via": "Fake"})
    engine.cfg.recent_models = ["Fake|anthropic/claude-opus-5.5", "Other|m", "Fake|TEE/glm-5.3"]
    engine._caps["Fake"] = {"m": {"vision": True, "context": 1000}}
    engine._learned_ctx[("Fake", "m")] = 4096
    engine._unsupported[("Fake", "m")] = {"temperature"}
    engine._efforts[("Fake", "m")] = ("low", "high")
    engine._research_auto["Fake"] = "anthropic/claude-sonnet-5.5"
    engine._learned_ctx[("Other", "m")] = 8192

    engine.delete_provider("Fake")
    s = engine.cfg.settings
    assert (s.review_model, s.vision_model, s.research_model, s.search_via) == ("", "", "", "")
    assert engine.cfg.recent_models == ["Other|m"]
    assert (engine.cfg.active_provider, engine.cfg.active_model) == ("Other", "")
    for cache in (engine._caps, engine._models, engine._research_auto):
        assert "Fake" not in cache
    for cache in (engine._learned_ctx, engine._unsupported, engine._efforts):
        assert not [k for k in cache if k[0] == "Fake"]
    assert engine._learned_ctx == {("Other", "m"): 8192}                 # another provider's is left alone

    engine.save_provider({"name": "Fake", "base_url": "https://newcomer.example/v1"})
    new = engine.cfg.provider("Fake")
    assert engine.caps_for(new, "m") is None and engine.context_limit(new, "m") is None
    assert isinstance(engine._vision_helper(), str)                      # no helper: the old one's role wasn't inherited


async def test_a_provider_name_cant_contain_the_separator(tmp_path):
    engine = Engine(Config(providers=[Provider("Old|Name", "http://localhost:1234/v1")]), lambda e: None, tmp_path / "rt",
                    save_config=lambda c: None)
    with pytest.raises(UserError, match="name can't contain \\|"):
        engine.save_provider({"name": "A|B", "base_url": "http://localhost:11434/v1"})
    assert [p.name for p in engine.cfg.providers] == ["Old|Name"]
    # one saved before the rule keeps working, and can be edited, until it is renamed
    engine.save_provider({"name": "Old|Name", "base_url": "http://localhost:5678/v1"}, original_name="Old|Name")
    assert engine.cfg.provider("Old|Name").base_url == "http://localhost:5678/v1"
    with pytest.raises(UserError, match="name can't contain \\|"):
        engine.save_provider({"name": "New|Name", "base_url": "http://localhost:5678/v1"}, original_name="Old|Name")
    engine.save_provider({"name": "New Name", "base_url": "http://localhost:5678/v1"}, original_name="Old|Name")
    assert [p.name for p in engine.cfg.providers] == ["New Name"]
