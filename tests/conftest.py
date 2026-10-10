import keyring
import pytest
from keyring.backend import KeyringBackend


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.store = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        from keyring.errors import PasswordDeleteError

        if (service, username) not in self.store:
            raise PasswordDeleteError(username)
        del self.store[(service, username)]


@pytest.fixture(autouse=True)
def memory_keyring(tmp_path, monkeypatch):
    """Never touch the real keyring or real config/data dirs in tests."""
    backend = MemoryKeyring()
    old = keyring.get_keyring()
    keyring.set_keyring(backend)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    yield backend
    keyring.set_keyring(old)


@pytest.fixture(autouse=True)
def no_real_containers(monkeypatch):
    """Never find the real podman or docker: the guacd container code would start or remove
    containers on this machine. Tests that pass their own `which` still see their fakes."""
    from datoolkit.sessions import guac

    real = guac.container_runtime
    monkeypatch.setattr(guac, "container_runtime", lambda which=guac._which: None if which is guac._which else real(which))
