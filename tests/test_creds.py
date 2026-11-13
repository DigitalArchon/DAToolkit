"""Secrets go only to a keyring that keeps them safe (creds.py): with none running, keyring may
pick one that writes to a plain file, and the API keys and the hosts' passwords were written
there. After DA Private Coder's creds._backend (Nov 2026). Nothing here touches the real keyring:
conftest's MemoryKeyring, or one standing in for a plaintext or a locked one."""

import asyncio

import keyring
import keyring.core
import pytest
from keyring.backend import KeyringBackend

from datoolkit import creds
from datoolkit.config import Config
from datoolkit.engine import Engine, UserError


class PlaintextKeyring(KeyringBackend):
    """What keyrings.alt's PlaintextKeyring is: a file anyone with the disk can read."""
    priority = 0.5

    def __init__(self):
        super().__init__()
        self.written = {}

    def get_password(self, service, username):
        return self.written.get((service, username))

    def set_password(self, service, username, password):
        self.written[(service, username)] = password

    def delete_password(self, service, username):
        self.written.pop((service, username), None)


class LockedKeyring(PlaintextKeyring):
    priority = 1

    def set_password(self, service, username, password):
        raise keyring.errors.KeyringLocked("the collection is locked")


@pytest.fixture
def plaintext(monkeypatch):
    backend = PlaintextKeyring()
    old = keyring.get_keyring()
    keyring.set_keyring(backend)
    looked = []
    monkeypatch.setattr(keyring.core, "init_backend", lambda *a: looked.append(1))   # never the real one
    yield backend, looked
    keyring.set_keyring(old)


def test_a_keyring_that_writes_a_plain_file_is_refused(plaintext):
    backend, looked = plaintext
    with pytest.raises(creds.KeyringUnavailable, match="No secure keyring is available.*nothing was stored"):
        creds.set_secret("provider", "NanoGPT", "sk-secret")
    assert backend.written == {} and looked == [1]          # looked for again first: one may have started


def test_a_locked_keyring_says_so_and_stores_nothing():
    backend = LockedKeyring()
    old = keyring.get_keyring()
    keyring.set_keyring(backend)
    try:
        with pytest.raises(creds.KeyringUnavailable, match="couldn't store it.*locked"):
            creds.set_secret("host", "dc01", "hunter2")
    finally:
        keyring.set_keyring(old)


async def test_settings_save_nothing_when_the_keyring_would_not_keep_it(plaintext, tmp_path):
    backend, _ = plaintext
    engine = Engine(Config(), lambda e: None, tmp_path / "rt", save_config=lambda c: None)
    with pytest.raises(UserError, match="Provider not saved. No secure keyring"):
        engine.save_provider({"name": "NanoGPT", "base_url": "https://nano-gpt.com/api/v1"}, "sk-secret")
    with pytest.raises(UserError, match="Provider not saved. No secure keyring"):
        engine.save_provider({"name": "Mine", "base_url": "https://bob:hunter2@llm.example/v1"})
    with pytest.raises(UserError, match="Host not saved. No secure keyring"):
        engine.save_host({"name": "dc01", "kind": "ssh", "host": "dc01.lan", "auth": "password"}, "hunter2")
    assert engine.cfg.providers == [] and engine.cfg.hosts == [] and backend.written == {}
    engine.save_provider({"name": "Local", "base_url": "http://localhost:11434/v1"})     # nothing secret: saved
    assert [p.name for p in engine.cfg.providers] == ["Local"]


async def test_a_password_ticked_to_be_saved_is_used_even_when_it_cannot_be(plaintext, tmp_path):
    backend, _ = plaintext
    events = []
    engine = Engine(Config(), events.append, tmp_path / "rt", save_config=lambda c: None)
    asking = asyncio.create_task(engine.ask_user("s1", "Password:", secret=True, can_save=True, host_name="dc01"))
    for _ in range(100):
        if engine._prompts:
            break
        await asyncio.sleep(0.01)
    engine.answer_prompt(next(iter(engine._prompts)), "hunter2", save=True)
    assert await asking == "hunter2"
    # it is saved once the connection it was typed for has authenticated: here, the remote desktop is there
    from datoolkit.sessions.manager import RdpSession
    engine.rdp_connected(RdpSession(id="s1", name="dc01", params={}, host_name="dc01"), None)
    assert backend.written == {}
    assert any(e.get("type") == "toast" and "password for dc01 wasn't saved" in e.get("text", "") for e in events)


# ---------------------------------------------------------------- removing a secret

class Relocked(KeyringBackend):
    """A keyring that held secrets and is locked now, its unlock prompt dismissed: what keyring's
    Secret Service backend raises then (KeyringLocked, or InitError when the service doesn't
    answer), and KWallet's (PasswordDeleteError "Cancelled by user" for a delete)."""
    priority = 1

    def __init__(self, error=keyring.errors.KeyringLocked("Failed to unlock the collection!"), on_delete=None):
        super().__init__()
        self.store = {}
        self.error, self.on_delete = error, on_delete or error
        self.locked = False

    def get_password(self, service, username):
        if self.locked:
            raise self.error
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        if self.locked:
            raise self.error
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        if self.locked:
            raise self.on_delete
        if (service, username) not in self.store:
            raise keyring.errors.PasswordDeleteError("No such password!")
        del self.store[(service, username)]


@pytest.fixture(params=["locked", "no answer", "kwallet cancelled"])
def relocked(request):
    backend = {"locked": Relocked(),
               "no answer": Relocked(keyring.errors.InitError("Failed to create the collection")),
               "kwallet cancelled": Relocked(on_delete=keyring.errors.PasswordDeleteError("Cancelled by user"))}[request.param]
    old = keyring.get_keyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(old)


def test_a_secret_that_isnt_there_is_no_error_and_one_that_cant_be_removed_is(relocked):
    creds.set_secret("host", "dc01", "hunter2")
    creds.delete_secret("host", "never-saved")                   # not there: nothing to do
    relocked.locked = True
    with pytest.raises(creds.KeyringUnavailable, match="couldn't remove it.*nothing was changed"):
        creds.delete_secret("host", "dc01")
    relocked.locked = False
    assert creds.get_secret("host", "dc01") == "hunter2"
    creds.delete_secret("host", "dc01")
    assert creds.get_secret("host", "dc01") is None


def test_with_no_keyring_at_all_there_is_nothing_to_remove():
    from keyring.backends.fail import Keyring as NoKeyring
    old = keyring.get_keyring()
    keyring.set_keyring(NoKeyring())
    try:
        creds.delete_secret("provider", "Local")                 # a local model server never had a key
    finally:
        keyring.set_keyring(old)


async def test_deleting_with_a_locked_keyring_changes_nothing(relocked, tmp_path):
    from datoolkit.config import Host, Provider
    cfg = Config(providers=[Provider("Mine", "https://llm.example/v1"), Provider("Other", "https://other.example/v1")],
                 hosts=[Host(name="dc01", kind="ssh", host="10.99.0.9", auth="password")],
                 active_provider="Mine", active_model="m1")
    creds.set_secret("provider", "Mine", "sk-mine")
    creds.set_secret("host", "dc01", "hunter2")
    saved, events = [], []
    engine = Engine(cfg, events.append, tmp_path / "rt", save_config=lambda c: saved.append(c.to_dict()))
    before = cfg.to_dict()
    relocked.locked = True
    with pytest.raises(UserError, match="Provider not deleted. The keyring couldn't remove it"):
        engine.delete_provider("Mine")
    with pytest.raises(UserError, match="Host not deleted. The keyring couldn't remove it"):
        engine.delete_host("dc01")
    with pytest.raises(UserError, match="password for dc01 wasn't forgotten. The keyring couldn't remove it"):
        engine.forget_host_password("dc01")
    # a rename moves the secret to the new name: reading it, or removing the old one, can fail too
    with pytest.raises(UserError, match="Provider not saved. The keyring"):
        engine.save_provider({"name": "Mine 2", "base_url": "https://llm.example/v1"}, original_name="Mine")
    with pytest.raises(UserError, match="Host not saved. The keyring"):
        engine.save_host({"name": "dc02", "kind": "ssh", "host": "10.99.0.9", "auth": "password"}, original_name="dc01")
    assert engine.cfg.to_dict() == before and saved == []        # memory and config.toml as they were
    assert not [e for e in events if e["type"] == "state"]
    assert relocked.store == {("datoolkit", "provider:Mine"): "sk-mine", ("datoolkit", "host:dc01"): "hunter2"}
    relocked.locked = False                                      # unlocked: the same clicks go through
    engine.delete_provider("Mine")
    engine.delete_host("dc01")
    assert [p.name for p in engine.cfg.providers] == ["Other"] and engine.cfg.hosts == [] and relocked.store == {}
    assert saved[-1]["providers"][0]["name"] == "Other" and saved[-1]["hosts"] == []
