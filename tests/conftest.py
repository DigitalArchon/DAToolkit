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


@pytest.fixture(autouse=True)
def no_real_model_lists(monkeypatch):
    """Never fetch a provider's real model list (NanoGPT's, OpenRouter's): the engine asks for it
    when a model is chosen or a message sent. A test that wants one passes its own client
    (engine._caps_http); without one, the list is out of reach."""
    import httpx

    from datoolkit.llm import capabilities

    for name in ("fetch_nanogpt", "fetch_openrouter"):
        async def offline(base_url, api_key, http=None, _real=getattr(capabilities, name)):
            if http is None:
                raise httpx.ConnectError("no network in tests")
            return await _real(base_url, api_key, http=http)
        monkeypatch.setattr(capabilities, name, offline)


@pytest.fixture
def suspend(monkeypatch):
    """`suspend(hours)`: the machine sleeps that long. The boot clock (dcap.since_boot) moves on;
    time.monotonic, which stops while the machine is suspended, doesn't (and the event loop
    keeps running on it)."""
    import time

    if not hasattr(time, "CLOCK_BOOTTIME"):
        pytest.skip("no CLOCK_BOOTTIME here")
    slept = [0.0]
    real = time.clock_gettime
    monkeypatch.setattr(time, "clock_gettime",
                        lambda which: real(which) + (slept[0] if which == time.CLOCK_BOOTTIME else 0))

    def sleep(hours: float) -> None:
        slept[0] += hours * 3600

    return sleep
