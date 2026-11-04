"""Provider, host and client names. Secrets are kept in the keyring under the name, so a name
belongs to one entry: a second entry with it would take over the first's key or password and
send it to its own address. A rename carries everything that names the entry along with it,
and a name with a "/" in it can be renamed and removed like any other."""

import httpx
import pytest
from fastapi import FastAPI

from datoolkit import creds, tools_cache
from datoolkit.config import Provider
from datoolkit.engine import UserError
from datoolkit.server.app import create_app

from test_engine import BASE, env  # noqa: F401


def test_a_new_provider_cannot_take_an_existing_name(env):  # noqa: F811
    engine, _, _ = env
    with pytest.raises(UserError, match="already a provider called Fake"):
        engine.save_provider({"name": "Fake", "base_url": "https://llm.example.net/v1"})
    assert [p.base_url for p in engine.cfg.providers] == [BASE]
    assert creds.get_secret("provider", "Fake") == "sk-test"


def test_one_address_can_have_several_providers_each_with_its_own_key(env):  # noqa: F811
    # a NanoGPT subscription and pay-as-you-go: the same host, two keys, told apart by name
    from datoolkit.engine import Engine
    engine, _, _ = env
    engine.save_provider({"name": "NanoGPT Sub Only", "base_url": "https://nano-gpt.com/api/v1"}, api_key="sk-sub")
    engine.save_provider({"name": "NanoGPT PAYG", "base_url": "https://nano-gpt.com/api/v1"}, api_key="sk-payg")
    with pytest.raises(UserError, match="under a name of its own"):
        engine.save_provider({"name": "NanoGPT PAYG", "base_url": "https://nano-gpt.com/api/v1"}, api_key="sk-other")
    keys = {name: Engine._client(engine, engine.cfg.provider(name))._client.api_key
            for name in ("NanoGPT Sub Only", "NanoGPT PAYG")}
    assert keys == {"NanoGPT Sub Only": "sk-sub", "NanoGPT PAYG": "sk-payg"}
    engine.save_host({"name": "web01 as app", "kind": "ssh", "host": "web01.lan", "user": "app"}, password="pw-app")
    assert {h.host for h in engine.cfg.hosts if h.name.startswith("web01")} == {"web01.lan"}
    assert creds.get_secret("host", "web01 as app") == "pw-app"


def test_a_provider_cannot_be_renamed_onto_another(env):  # noqa: F811
    engine, _, _ = env
    engine.save_provider({"name": "Other", "base_url": "https://other.example/v1"}, api_key="sk-other")
    with pytest.raises(UserError, match="already a provider called Fake"):
        engine.save_provider({"name": "Fake", "base_url": "https://other.example/v1"}, original_name="Other")
    assert creds.get_secret("provider", "Fake") == "sk-test" and creds.get_secret("provider", "Other") == "sk-other"
    assert sorted(p.name for p in engine.cfg.providers) == ["Fake", "Other"]
    engine.save_provider({"name": "Fake", "base_url": BASE, "default_model": "m"}, original_name="Fake")   # an edit
    assert engine.cfg.provider("Fake").default_model == "m"


def test_a_new_host_cannot_take_an_existing_name(env):  # noqa: F811
    engine, _, _ = env
    creds.set_secret("host", "web01", "client-A-password")
    with pytest.raises(UserError, match="already a host called web01"):
        engine.save_host({"name": "web01", "kind": "ssh", "host": "web01.clientb.lan"})
    with pytest.raises(UserError, match="already a host called web01"):
        engine.save_host({"name": "db01", "kind": "ssh", "host": "db01.lan"})
        engine.save_host({"name": "web01", "kind": "ssh", "host": "db01.lan"}, original_name="db01")
    assert engine.cfg.host("web01").host == "web01.lan"
    assert [h.name for h in engine.cfg.hosts].count("web01") == 1


def test_renaming_a_provider_carries_the_settings_that_name_it(env):  # noqa: F811
    engine, _, _ = env
    s = engine.cfg.settings
    s.review_model, s.vision_model, s.research_model = "Fake|a", "Fake|b", "Fake|c"
    s.search_via = "Fake"
    engine.cfg.recent_models = ["Fake|a", "Other|x"]
    engine._learned_ctx[("Fake", "a")] = 32768
    engine.save_provider({"name": "Renamed", "base_url": BASE}, original_name="Fake")
    assert (s.review_model, s.vision_model, s.research_model, s.search_via) == ("Renamed|a", "Renamed|b", "Renamed|c", "Renamed")
    assert engine.cfg.recent_models == ["Renamed|a", "Other|x"]
    assert engine._learned_ctx == {("Renamed", "a"): 32768}
    assert engine.cfg.active_provider == "Renamed" and creds.get_secret("provider", "Renamed") == "sk-test"


def test_a_tee_attestation_does_not_outlive_a_change_of_address(env):  # noqa: F811
    engine, _, _ = env
    closed = []

    class Attested:
        def close(self):
            closed.append(True)
    engine._tee[("Fake", "TEE/glm-5.3")] = (Attested(), 0.0, {})
    engine.save_provider({"name": "Fake", "base_url": BASE}, original_name="Fake")         # same address: kept
    assert ("Fake", "TEE/glm-5.3") in engine._tee
    engine.save_provider({"name": "Fake", "base_url": "https://another-gateway.example/v1"}, original_name="Fake")
    assert not engine._tee and closed == [True]


async def test_names_with_a_slash_can_be_renamed_and_removed(env):  # noqa: F811
    engine, _, _ = env
    engine.cfg.providers.append(Provider("Lab / GPU", "https://gpu.example/v1"))
    engine.save_host({"name": "AV/rack1", "kind": "ssh", "host": "rack1.lan"})
    engine.add_client("A/V Solutions")
    engine.new_case("c", "open")
    engine.hypotheses = [{"id": "dns/ttl", "text": "TTL too long", "confidence": 0.5, "status": "open"}]
    app: FastAPI = create_app("main", lambda emit: engine)
    app.state.engine = engine
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        m = {"x-token": "main"}
        r = await c.post("/api/clients/A%2FV%20Solutions", json={"name": "AV Solutions"}, headers=m)
        assert r.status_code == 200 and engine.cfg.clients == ["AV Solutions"]
        assert (await c.post("/api/hypotheses/dns%2Fttl/mark", json={"mark": "ruled_out"}, headers=m)).status_code == 200
        assert (await c.post("/api/hosts/AV%2Frack1/forget-password", headers=m)).status_code == 200
        assert (await c.delete("/api/hosts/AV%2Frack1", headers=m)).status_code == 200
        assert (await c.delete("/api/providers/Lab%20%2F%20GPU", headers=m)).status_code == 200
    assert not engine.cfg.host("AV/rack1") and not engine.cfg.provider("Lab / GPU")


def test_a_tool_never_replaces_another_tools_file(tmp_path):
    root, a, b = tmp_path / "cache", tmp_path / "v1", tmp_path / "v2"
    a.mkdir(), b.mkdir()
    (a / "PsExec.exe").write_bytes(b"version 2.40")
    (b / "PsExec.exe").write_bytes(b"version 2.43")
    tools_cache.add(a / "PsExec.exe", "psexec-2.40", "windows", root=root)
    with pytest.raises(ValueError, match="already has a file called PsExec.exe"):
        tools_cache.add(b / "PsExec.exe", "psexec-2.43", "windows", root=root)
    tools_cache.add(b / "PsExec.exe", "psexec-2.40", "windows", root=root)     # the same tool, updated
    assert [t["status"] for t in tools_cache.verify(root)] == ["ok"]
    (a / "tools.toml").write_text("not a tool")
    with pytest.raises(ValueError, match="tools.toml"):
        tools_cache.add(a / "tools.toml", "manifest", "any", root=root)
    assert [t.name for t in tools_cache.load(root)] == ["psexec-2.40"]


@pytest.mark.parametrize("data,why", [
    ({"host": "-oProxyCommand=touch /tmp/x", "port": 22}, "on its own"),
    ({"host": "-p 2222 web01"}, "on its own"),
    ({"host": "web01.lan", "port": 70000}, "between 1 and 65535"),
    ({"host": "web01.lan", "port": "22a"}, "must be a number"),
])
def test_a_host_address_that_ssh_would_take_for_options_is_refused(env, data, why):  # noqa: F811
    engine, _, _ = env
    with pytest.raises(UserError, match=why):
        engine.save_host({"name": "bad", "kind": "ssh", **data})
    assert not engine.cfg.host("bad")
