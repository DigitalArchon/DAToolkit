"""A provider's API key goes only to the address it was entered for (config.Provider.key_origin,
Engine._key_problem), and a host's saved password to the host's kind, address and port
(Host.password_for, Engine._host_password): the keyring keeps them by name, so an address changed
in Settings without them, or in config.toml by hand, took them, and everything sent with them, to
wherever the address pointed. The design every DA app keeps (Nov 2026)."""

import asyncio

import httpx
import pytest
import tomllib

from datoolkit import config as config_mod
from datoolkit import creds
from datoolkit.config import Config, Provider
from datoolkit.engine import Engine, UserError
from datoolkit.llm import netguard

from test_engine import BASE, env  # noqa: F401

ELSEWHERE = "https://api.example.org/v1"


def provider_data(engine, name="Fake", **changes) -> dict:
    data = {k: v for k, v in engine.cfg.provider(name).__dict__.items() if k != "key_origin"}
    return {**data, **changes}


def test_an_origin_is_scheme_host_and_port():
    assert netguard.origin("https://Nano-GPT.com/api/v1") == "https://nano-gpt.com:443"
    assert netguard.origin("https://nano-gpt.com:443/x") == netguard.origin("https://nano-gpt.com/y")
    assert netguard.origin("http://192.168.1.5:11434/v1") == "http://192.168.1.5:11434"
    assert netguard.origin("http://[::1]/v1") == "http://[::1]:80"
    assert netguard.origin("https://alice:secret@host.example/v1") == "https://host.example:443"
    assert netguard.origin("https://host.example:8443") != netguard.origin("https://host.example")
    assert netguard.origin("http://host.example") != netguard.origin("https://host.example")
    for nothing in ("", None, "training://x", "nano-gpt.com/api", "https://[::1"):
        assert netguard.origin(nothing) == ""
    assert netguard.shown_origin("https://nano-gpt.com:443") == "https://nano-gpt.com"
    assert netguard.shown_origin("https://nano-gpt.com:8443") == "https://nano-gpt.com:8443"


async def test_an_address_changed_without_the_key_doesnt_take_it_along(env):  # noqa: F811
    engine, _, _ = env
    prov = engine.cfg.provider("Fake")
    assert prov.key_origin == netguard.origin(BASE), "a key from before: the address beside it"
    assert Engine._client(engine, prov) is not None
    engine.save_provider(provider_data(engine, base_url=ELSEWHERE), None, "Fake")
    prov = engine.cfg.provider("Fake")
    with pytest.raises(UserError) as e:
        Engine._client(engine, prov)                         # nothing is built with the key, nothing sent
    said = str(e.value)
    assert "entered for https://fake.example" in said and "now https://api.example.org" in said
    assert "Settings → AI providers → Fake" in said and "put the address back" in said
    assert creds.get_secret("provider", "Fake") == "sk-test", "never deleted"
    view = next(p for p in engine.snapshot()["config"]["providers"] if p["name"] == "Fake")
    assert view["has_key"] and view["key_held"] == said and view["key_origin"] == netguard.origin(BASE)
    # put back, it goes again
    engine.save_provider(provider_data(engine, base_url=BASE + "/"), None, "Fake")
    assert Engine._client(engine, engine.cfg.provider("Fake")) is not None
    view = next(p for p in engine.snapshot()["config"]["providers"] if p["name"] == "Fake")
    assert view["key_held"] == ""


async def test_the_saved_key_goes_to_a_new_address_only_when_chosen_or_entered_again(env):  # noqa: F811
    engine, _, _ = env
    engine.save_provider(provider_data(engine, base_url=ELSEWHERE), None, "Fake", use_saved_key=True)
    assert engine.cfg.provider("Fake").key_origin == netguard.origin(ELSEWHERE)
    assert Engine._client(engine, engine.cfg.provider("Fake")) is not None
    engine.save_provider(provider_data(engine, base_url="https://third.example/v1"), None, "Fake")
    with pytest.raises(UserError, match="entered for https://api.example.org"):
        Engine._client(engine, engine.cfg.provider("Fake"))
    engine.save_provider(provider_data(engine), "sk-new", "Fake")               # entered again, for this address
    assert engine.cfg.provider("Fake").key_origin == "https://third.example:443"
    assert Engine._client(engine, engine.cfg.provider("Fake")) is not None


async def test_a_new_provider_has_no_key_until_one_is_given_and_a_rename_keeps_its(env):  # noqa: F811
    engine, _, _ = env
    engine.save_provider({"name": "Other", "base_url": ELSEWHERE})
    assert engine.cfg.provider("Other").key_origin == ""
    engine.save_provider({"name": "Other", "base_url": ELSEWHERE}, "sk-other", "Other")
    assert engine.cfg.provider("Other").key_origin == netguard.origin(ELSEWHERE)
    engine.save_provider({"name": "Renamed", "base_url": ELSEWHERE}, None, "Other")
    assert engine.cfg.provider("Renamed").key_origin == netguard.origin(ELSEWHERE)
    assert Engine._client(engine, engine.cfg.provider("Renamed")) is not None
    engine.save_provider({"name": "Again", "base_url": "https://moved.example/v1"}, None, "Renamed")
    with pytest.raises(UserError, match="saved for Again was entered for https://api.example.org"):
        Engine._client(engine, engine.cfg.provider("Again"))


async def test_search_research_tee_and_model_lists_dont_take_the_key_elsewhere_either(env):  # noqa: F811
    engine, _, _ = env
    engine.save_provider(provider_data(engine, base_url="https://nano-gpt.com/api/v1"), None, "Fake")
    prov = engine.cfg.provider("Fake")
    with pytest.raises(UserError, match="wasn't sent there"):
        engine._provider_key_safe(prov)
    sent: list = []
    engine._tee_factory = lambda base, key, model: sent.append(key)
    found = await engine._attest(prov, "TEE/glm-5.3", "chat")
    assert found["status"] == "failed" and "wasn't sent there" in found["error"] and sent == []
    engine._caps_http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(500)))
    await engine._load_caps(prov)
    assert sent == [], "no model list asked for with the key"


async def test_the_api_takes_only_a_json_true_as_the_technicians_yes(env, tmp_path):  # noqa: F811
    from datoolkit.server.app import create_app

    engine, _, _ = env
    app = create_app("tok", lambda emit: engine, companion_dir=tmp_path)
    app.state.engine = engine
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
        headers = {"x-token": "tok"}
        body = {"provider": provider_data(engine, base_url=ELSEWHERE), "original_name": "Fake", "use_saved_key": "true"}
        assert (await http.post("/api/providers", json=body, headers=headers)).status_code == 200
        assert engine.cfg.provider("Fake").key_origin == netguard.origin(BASE)
        body["use_saved_key"] = True
        assert (await http.post("/api/providers", json=body, headers=headers)).status_code == 200
        assert engine.cfg.provider("Fake").key_origin == netguard.origin(ELSEWHERE)


def test_a_key_saved_before_takes_the_address_beside_it_once_and_a_hand_edit_takes_nothing(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[[providers]]\nname = "Fake"\nbase_url = "https://fake.example/api/v1"\n')
    cfg = config_mod.load(path)
    assert cfg.provider("Fake").key_origin == "https://fake.example:443"
    written = tomllib.loads(path.read_text())
    assert written["providers"][0]["key_origin"] == "https://fake.example:443", "kept at once, before any edit"
    # edited by hand afterwards, to send it elsewhere: the key stays for its own address
    path.write_text(path.read_text().replace("https://fake.example/api/v1", "https://evil.example/v1"))
    cfg = config_mod.load(path)
    creds.set_secret("provider", "Fake", "sk-test")
    with pytest.raises(UserError, match="entered for https://fake.example, and Fake's address is now https://evil"):
        Engine(cfg, lambda e: None, tmp_path / "rt", save_config=lambda c: None)._provider_key(cfg.provider("Fake"))


def test_a_config_made_in_code_is_for_the_addresses_it_has(tmp_path):
    cfg = Config(providers=[Provider("Fake", BASE)])
    creds.set_secret("provider", "Fake", "sk-test")
    engine = Engine(cfg, lambda e: None, tmp_path / "rt", save_config=lambda c: None)
    assert cfg.provider("Fake").key_origin == netguard.origin(BASE)
    assert engine._provider_key(cfg.provider("Fake")) == "sk-test"


# --- a host's saved password, likewise -------------------------------------------------------


def host_data(engine, name="web01", **changes) -> dict:
    data = {k: v for k, v in engine.cfg.host(name).__dict__.items() if k != "password_for"}
    return {**data, **changes}


def test_a_hosts_origin_is_its_kind_address_and_port():
    from datoolkit.config import Host, host_origin

    assert host_origin(Host("a", "ssh", "Web01.LAN")) == "ssh://web01.lan:22"
    assert host_origin(Host("a", "rdp", "srv", port=3390)) == "rdp://srv:3390"
    assert host_origin(Host("a", "winrm", "dc")) == "winrm+https://dc:5986"
    assert host_origin(Host("a", "winrm", "dc", winrm_ssl=False)) == "winrm+http://dc:5985"


async def test_a_hosts_address_changed_without_its_password_doesnt_take_it_along(env):  # noqa: F811
    from test_engine import logs_in, prompt_of

    engine, _, events = env
    engine.new_case("x", "open")
    creds.set_secret("host", "web01", "pw1")
    assert engine.cfg.host("web01").password_for == "ssh://web01.lan:22", "saved before: the address beside it"
    engine.save_host(host_data(engine, host="web02.lan"), None, "web01")
    view = next(h for h in engine.snapshot()["config"]["hosts"] if h["name"] == "web01")
    assert "entered for ssh://web01.lan:22" in view["password_held"]
    engine.sessions.spawn("web01", ["/bin/cat"], {}, name="web01", kind="ssh", host_name="web01")
    task = asyncio.create_task(engine._askpass("web01", "bob@web02.lan's password: ", "askpass"))
    prompt = await prompt_of(events)                 # asked, as with none saved
    said = [e["text"] for e in events if e["type"] == "toast"]
    assert any("entered for ssh://web01.lan:22" in t and "now ssh://web02.lan:22" in t for t in said)
    assert creds.get_secret("host", "web01") == "pw1", "never deleted"
    engine.answer_prompt(prompt["id"], "pw2", save=True)
    assert await task == "pw2"
    await logs_in(engine, "web01")
    assert creds.get_secret("host", "web01") == "pw2"
    assert engine.cfg.host("web01").password_for == "ssh://web02.lan:22", "kept for the address it was typed for"
    engine.sessions.close_all()


async def test_the_saved_password_goes_to_a_new_address_when_chosen(env):  # noqa: F811
    engine, _, _ = env
    creds.set_secret("host", "web01", "pw1")
    engine.save_host(host_data(engine, port=2222), None, "web01")
    assert engine._host_password(engine.cfg.host("web01")) is None
    engine.save_host(host_data(engine), None, "web01", use_saved_password=True)
    assert engine._host_password(engine.cfg.host("web01")) == "pw1"
    engine.save_host(host_data(engine, host="web03.lan"), "pw3", "web01")               # typed for the new address
    assert engine._host_password(engine.cfg.host("web01")) == "pw3"
    engine.save_host({"name": "new", "kind": "rdp", "host": "srv"})
    assert engine.cfg.host("new").password_for == ""


def test_a_hosts_password_saved_before_takes_its_address_once(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[[hosts]]\nname = "dc"\nkind = "winrm"\nhost = "10.0.0.5"\nauth = "ntlm"\n')
    cfg = config_mod.load(path)
    assert tomllib.loads(path.read_text())["hosts"][0]["password_for"] == "winrm+https://10.0.0.5:5986"
    path.write_text(path.read_text().replace('host = "10.0.0.5"', 'host = "10.0.0.66"'))
    cfg = config_mod.load(path)
    creds.set_secret("host", "dc", "hunter2")
    engine = Engine(cfg, lambda e: None, tmp_path / "rt", save_config=lambda c: None)
    assert engine._host_password(cfg.host("dc")) is None
