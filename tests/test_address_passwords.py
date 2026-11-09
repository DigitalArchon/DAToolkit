"""A password written into a provider's address (https://user:pass@host/v1: a model server
behind one) is kept in the keyring, as an API key is, never in config.toml, and never shown in a
message, a notice or the log. Ported from DA Assistant's tests/test_address_passwords.py and
test_https.py (Nov 2026); the keyring is conftest's MemoryKeyring, or one standing in for a
locked one."""

import os
import stat

import httpx
import keyring
import pytest
from keyring.backend import KeyringBackend

from datoolkit import config as config_mod
from datoolkit import creds
from datoolkit.config import ADDRESS_SECRET, Config, Provider
from datoolkit.engine import Engine, UserError
from datoolkit.llm import netguard
from datoolkit.llm.private_mode import PrivateModeClient, relay_url
from datoolkit.llm.tee import TeeClient, TeeRefused

from test_engine import env  # noqa: F401

ENDPOINT = "https://bob:hunter2@llm.example/v1"
OLLAMA = "http://carol:pw2@192.168.1.20:11434/v1"


def test_the_user_name_and_password_go_and_the_address_stays():
    for url, plain in ((ENDPOINT, "https://llm.example/v1"),
                       ("https://token@host.example/v1?a=b#c", "https://host.example/v1?a=b#c"),
                       ("https://u:p@ss@host.example/", "https://host.example/"),
                       ("http://u:p@[fd00::5]:11434", "http://[fd00::5]:11434"),
                       ("u:p@proxy:3128", "proxy:3128"),
                       ("https://u:p@host:port/", "https://host:port/")):
        assert netguard.without_password(url) == plain, url
    for url in ("", "http://localhost:11434", "HTTPS://Host.Example:3128/", "https://host.example/a@b",
                "https://host.example/?to=a@b"):
        assert netguard.without_password(url) == url, url


def test_an_address_is_shown_without_its_password():
    assert netguard.shown("https://alice:s3cret@llm.example.com/v1") == "https://…@llm.example.com/v1"
    assert netguard.shown("http://192.168.1.20:11434/v1") == "http://192.168.1.20:11434/v1"
    assert netguard.shown("https://llm.example.com/v1/@cf/x") == "https://llm.example.com/v1/@cf/x"
    # One that can't be read as an address loses all it has up to its last "@".
    for typed, shown in (("http://alice:s3cret@[fd00::5", "http://…@[fd00::5"),
                         ("alice:s3cret@proxy.example:3128", "…@proxy.example:3128"),
                         ("https://alice:s3cret@llm.example.com:99999/v1", "https://…@llm.example.com:99999/v1"),
                         ("https://alice:s3/cret@llm.example.com/v1", "https://…@llm.example.com/v1")):
        assert netguard.shown(typed) == shown, typed
    # An error that quotes one (httpx's: "for url '...'") is said without it too.
    said = netguard.scrubbed("Client error '401 Unauthorized' for url 'https://alice:s3cret@llm.example.com/v1/models'")
    assert "s3cret" not in said and "alice" not in said and "https://…@llm.example.com/v1/models" in said


# Each written with a password, and none of them one that can be used.
WITH_PASSWORDS = ("http://alice:s3cret@llm.example.com/v1",          # not https
                  "https://alice:s3cret@llm.example.com:99999/v1",   # no such port
                  "http://alice:s3cret@[fd00::5",                     # half typed
                  "https://alice:s3cret@llm.exa mple.com/v1",         # a space
                  "alice:s3cret@llm.example.com/v1",                  # no scheme
                  "ftp://alice:s3cret@llm.example.com/v1",            # not the web
                  "https://alice:s3cret@/v1")                         # no host


def test_no_message_about_an_address_repeats_its_password(tmp_path):
    engine = Engine(Config(), lambda e: None, tmp_path / "rt", save_config=lambda c: None)
    for address in WITH_PASSWORDS:
        found = netguard.problem(address)
        assert found and "s3cret" not in found, address
        with pytest.raises(UserError) as refused:
            engine.save_provider({"name": "P", "base_url": address})
        assert "s3cret" not in str(refused.value), address
    assert creds.get_secret(ADDRESS_SECRET, "P") is None          # nothing kept of a refused one


def path_of(tmp_path):
    return tmp_path / "config.toml"


def test_an_address_typed_with_a_password_is_written_without_it(tmp_path):
    path = path_of(tmp_path)
    engine = Engine(Config(), lambda e: None, tmp_path / "rt", save_config=lambda c: config_mod.save(c, path))
    engine.save_provider({"name": "Mine", "base_url": ENDPOINT}, "sk-test")
    engine.save_provider({"name": "Ollama", "base_url": OLLAMA})
    text = path.read_text()
    assert 'base_url = "https://llm.example/v1"' in text and 'base_url = "http://192.168.1.20:11434/v1"' in text
    for secret in ("hunter2", "bob", "pw2", "carol"):
        assert secret not in text
    assert creds.get_secret(ADDRESS_SECRET, "Mine") == ENDPOINT
    # In use as typed, now and at the next start.
    assert engine.cfg.provider("Mine").base_url == ENDPOINT
    again = config_mod.load(path)
    assert [p.base_url for p in again.providers] == [ENDPOINT, OLLAMA]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_one_left_in_the_file_moves_to_the_keyring(tmp_path):
    path = path_of(tmp_path)
    path.write_text(f'active_provider = "Mine"\n\n[[providers]]\nname = "Mine"\nbase_url = "{ENDPOINT}"\n')
    cfg = config_mod.load(path)
    assert cfg.provider("Mine").base_url == ENDPOINT
    assert creds.get_secret(ADDRESS_SECRET, "Mine") == ENDPOINT
    assert "hunter2" not in path.read_text() and 'base_url = "https://llm.example/v1"' in path.read_text()
    assert 'active_provider = "Mine"' in path.read_text()


def test_one_the_keyring_already_holds_goes_from_the_file(tmp_path):
    path = path_of(tmp_path)
    creds.set_secret(ADDRESS_SECRET, "Mine", ENDPOINT)
    path.write_text(f'[[providers]]\nname = "Mine"\nbase_url = "{ENDPOINT}"\n')
    assert config_mod.load(path).provider("Mine").base_url == ENDPOINT
    assert "hunter2" not in path.read_text()


def test_the_password_goes_only_to_the_address_it_was_typed_for(tmp_path):
    path = path_of(tmp_path)
    creds.set_secret(ADDRESS_SECRET, "Mine", ENDPOINT)
    config_mod.save(Config(providers=[Provider("Mine", ENDPOINT)]), path)
    assert "hunter2" not in path.read_text()
    # config.toml edited by hand, or by another copy of the app, to another server
    path.write_text(path.read_text().replace("llm.example", "elsewhere.example"))
    assert config_mod.load(path).provider("Mine").base_url == "https://elsewhere.example/v1"


async def test_taking_the_password_out_takes_it_out_of_the_keyring(env, tmp_path):  # noqa: F811
    engine, _, _ = env
    engine.save_provider({"name": "Mine", "base_url": ENDPOINT})
    assert creds.get_secret(ADDRESS_SECRET, "Mine") == ENDPOINT
    engine.save_provider({"name": "Mine", "base_url": ENDPOINT, "default_model": "m"}, original_name="Mine")
    assert creds.get_secret(ADDRESS_SECRET, "Mine") == ENDPOINT        # saved again: still there
    engine.save_provider({"name": "Renamed", "base_url": ENDPOINT}, original_name="Mine")
    assert creds.get_secret(ADDRESS_SECRET, "Mine") is None and creds.get_secret(ADDRESS_SECRET, "Renamed") == ENDPOINT
    engine.save_provider({"name": "Renamed", "base_url": "https://llm.example/v1"}, original_name="Renamed")
    assert creds.get_secret(ADDRESS_SECRET, "Renamed") is None
    engine.save_provider({"name": "Again", "base_url": ENDPOINT})
    engine.delete_provider("Again")
    assert creds.get_secret(ADDRESS_SECRET, "Again") is None


class LockedKeyring(KeyringBackend):
    """A keyring that is there but refuses: locked, or its unlock prompt dismissed."""
    priority = 1

    def get_password(self, service, username):
        raise keyring.errors.KeyringLocked("locked")

    def set_password(self, service, username, password):
        raise keyring.errors.KeyringLocked("locked")

    def delete_password(self, service, username):
        raise keyring.errors.KeyringLocked("locked")


def test_with_no_keyring_to_take_it_one_in_the_file_stays_there_and_is_used(tmp_path):
    path = path_of(tmp_path)
    path.write_text(f'[[providers]]\nname = "Mine"\nbase_url = "{ENDPOINT}"\n')
    old = keyring.get_keyring()
    keyring.set_keyring(LockedKeyring())
    try:
        cfg = config_mod.load(path)
        assert cfg.provider("Mine").base_url == ENDPOINT
        cfg.active_model = "m"                      # a save about something else doesn't lose it
        config_mod.save(cfg, path)
        assert ENDPOINT in path.read_text()
    finally:
        keyring.set_keyring(old)


async def test_the_page_lists_an_address_without_its_password(env):  # noqa: F811
    engine, _, _ = env
    engine.save_provider({"name": "Mine", "base_url": ENDPOINT}, "sk-test")
    engine.cfg.active_provider = "Mine"
    snap = engine.snapshot()
    mine = next(p for p in snap["config"]["providers"] if p["name"] == "Mine")
    assert mine["shown_url"] == "https://…@llm.example/v1" and mine["base_url"] == ENDPOINT   # the form's field
    assert snap["active"]["base_url"] == "https://…@llm.example/v1"


async def test_nor_does_an_error_quoting_it(env, monkeypatch):  # noqa: F811
    """httpx's errors quote the address whole ("for url 'https://bob:hunter2@…'"), and DA Toolkit's
    own named it as typed: the model list, a TEE model's attestation, Private Mode."""
    engine, _, events = env
    prov = Provider("Mine", ENDPOINT)
    engine.cfg.providers.append(prov)
    creds.set_secret("provider", "Mine", "sk-test")

    def refuse(*a, **k):
        request = httpx.Request("GET", ENDPOINT + "/models")
        raise httpx.HTTPStatusError("Client error '401 Unauthorized' for url '%s'" % request.url,
                                    request=request, response=httpx.Response(401, request=request))

    class Listing:
        async def list_models(self):
            refuse()

    monkeypatch.setattr(engine, "_client", lambda p, model="": Listing())
    with pytest.raises(UserError) as failed:
        await engine.list_models("Mine")
    assert "hunter2" not in str(failed.value) and "https://…@llm.example/v1" in str(failed.value)
    engine.emit("toast", level="error", text=f"Could not read model capabilities from Mine: for url '{ENDPOINT}'")
    assert "hunter2" not in str(events[-1])
    tee = TeeClient(ENDPOINT, "sk", "TEE/x", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    with pytest.raises(TeeRefused) as refused:
        tee.attest()
    assert "hunter2" not in str(refused.value) and "…@llm.example" in str(refused.value)
    assert "hunter2" not in relay_url("https://bob:hunter2@nano-gpt.com/api/v1")
    with pytest.raises(Exception) as not_private:
        PrivateModeClient(ENDPOINT, "sk", None)
    assert "hunter2" not in str(not_private.value)
