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
    assert backend.written == {}
    assert any(e.get("type") == "toast" and "password for dc01 wasn't saved" in e.get("text", "") for e in events)
