"""https, or nothing (llm/netguard.py).

A plain http base URL used to be saved and used: the API key and the case went out
unencrypted. Now such an address is refused by Settings, and again at each place a request
leaves from, so a config.toml edited by hand sends nothing either. Plain http stays allowed
for this machine and its local network, which is how a model server of one's own is reached.
"""

import asyncio

import pytest

from datoolkit import creds
from datoolkit.config import Config, Provider
from datoolkit.engine import Engine, UserError
from datoolkit.llm import netguard


def test_https_is_accepted_anywhere():
    for url in ("https://nano-gpt.com/api/v1", "https://api.example.com:8443/v1", "HTTPS://Example.com/x",
                "https://192.0.2.7/v1", "  https://nano-gpt.com/api  "):
        assert netguard.problem(url) is None, url


def test_plain_http_is_accepted_only_on_this_machine_and_its_network():
    for url in ("http://localhost:11434/v1", "http://LOCALHOST/v1", "http://127.0.0.1:8080/v1",
                "http://127.8.9.10/v1", "http://[::1]:1234/v1", "http://10.0.0.5/v1", "http://172.16.0.1/v1",
                "http://172.31.255.254/v1", "http://192.168.1.20:5000/v1", "http://169.254.10.10/v1",
                "http://[fe80::1]/v1", "http://[fd12:3456:789a::1]/v1", "http://[fc00::1]/v1",
                "http://ollama.local:11434/v1", "http://box.localhost/v1"):
        assert netguard.problem(url) is None, url


def test_plain_http_to_anywhere_else_is_refused_with_the_reason():
    for url in ("http://api.example.com/v1", "http://nano-gpt.com/api/v1", "http://8.8.8.8/v1",
                "http://172.15.0.1/v1", "http://172.32.0.1/v1", "http://192.169.0.1/v1", "http://11.0.0.1/v1",
                "http://169.255.0.1/v1", "http://[2001:db8::1]/v1", "http://[fec0::1]/v1",
                # ranges Python calls "private" that are not the local network
                "http://192.0.2.1/v1", "http://198.18.0.1/v1", "http://100.64.0.1/v1",
                # names that only look local (a .lan name is reached over https only)
                "http://localhost.example.com/v1", "http://local/v1", "http://10.0.0.5.example.com/v1",
                "http://mylocal/v1", "http://ollama.lan:11434/v1"):
        found = netguard.problem(url)
        assert found and "not https" in found and "https://" in found, url


def test_an_ipv4_address_dressed_as_ipv6_is_judged_as_what_it_is():
    assert netguard.problem("http://[::ffff:192.168.1.5]/v1") is None
    assert netguard.problem("http://[::ffff:8.8.8.8]/v1") is not None


def test_what_is_not_a_web_address_at_all_is_refused():
    for url in ("", "   ", None, "nano-gpt.com/api", "ftp://example.com/x", "file:///etc/passwd", "http://",
                "https://", "javascript:alert(1)"):
        assert netguard.problem(url) is not None, url
    with pytest.raises(netguard.InsecureURL):
        netguard.check("http://api.example.com/v1")
    assert netguard.check(" https://api.example.com/v1 ") == "https://api.example.com/v1"


def engine_with(tmp_path, *providers):
    engine = Engine(Config(providers=list(providers)), lambda e: None, tmp_path / "rt", save_config=lambda c: None)
    for p in providers:
        creds.set_secret("provider", p.name, "sk-test")
    return engine


async def test_settings_will_not_save_a_plain_http_address_off_the_local_network(tmp_path):
    engine = engine_with(tmp_path)
    with pytest.raises(UserError, match="not saved.*not https"):
        engine.save_provider({"name": "Elsewhere", "base_url": "http://api.example.com/v1"})
    assert engine.cfg.providers == []
    for url in ("https://api.example.com/v1", "http://localhost:11434/v1", "http://10.0.0.5:8000/v1",
                "http://ollama.local:11434/v1", "training://disk-full"):
        engine.save_provider({"name": url, "base_url": url})
    assert [p.base_url for p in engine.cfg.providers] == [
        "https://api.example.com/v1", "http://localhost:11434/v1", "http://10.0.0.5:8000/v1",
        "http://ollama.local:11434/v1", "training://disk-full"]


async def test_a_config_edited_by_hand_to_hold_one_sends_nothing(tmp_path):
    """Checked again where each request leaves from: the chat, the model list, a TEE model's
    attestation, web search and research."""
    engine = engine_with(tmp_path, Provider("Elsewhere", "http://api.example.com/v1"),
                         Provider("NanoGPT", "http://nano-gpt.com/api/v1"))
    sent = []

    class Never:                       # stands in for the HTTP clients: records any attempt to use one
        def __getattr__(self, name):
            return lambda *a, **k: sent.append(name)

    engine._search_http = engine._caps_http = Never()
    await engine.start()
    try:
        with pytest.raises(UserError, match="Could not list models.*not https"):
            await engine.list_models("Elsewhere")
        engine.new_case("hand-edited", "open")
        engine.cfg.active_provider, engine.cfg.active_model = "Elsewhere", "any/model"
        engine.send("hello")
        for _ in range(200):
            if not engine.busy:
                break
            await asyncio.sleep(0.01)
        assert engine.chat[-1]["kind"] == "note" and "not sent" in engine.chat[-1]["text"]
        assert "not https" in engine.chat[-1]["text"] and engine.can_retry

        engine.cfg.active_model = "TEE/glm-5.3"
        att = await engine.attest_now()
        assert att["status"] == "failed" and "not https" in att["error"]

        with pytest.raises(UserError, match="not https"):
            await engine._run_search("anything")
        with pytest.raises(UserError, match="not https"):
            engine._research_tools({})
    finally:
        await engine.stop()
    assert sent == [], "no request left the machine"
