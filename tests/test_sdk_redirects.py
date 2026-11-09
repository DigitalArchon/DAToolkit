"""Tinfoil's SDK fetches over https only: a redirect off https, or from the internet into this
network, is not followed (llm/sdk_guard.py).

The SDK makes bare requests.get and requests.post calls (GitHub's release and Sigstore bundle,
AMD's and Intel's certificates), and requests follows a redirect anywhere, https to plain http
included. sdk_guard.guard hands each of its modules a stand-in for requests whose Session judges
every redirect first, without changing the SDK on disk. Here the SDK is a fake one, written to a
folder of the test's own, in the shapes the real one takes requests in; nothing reaches the
network. Ported from DA Assistant's tests/test_sdk_redirects.py (Nov 2026)."""

import importlib
import sys
import textwrap

import pytest
import requests

from datoolkit.llm import netguard, private_mode, sdk_guard

FILES = {
    "__init__.py": "from .client import SecureClient\n",
    "client.py": """
        from .github import fetch_latest_release

        class SecureClient:
            def __init__(self, **kwargs):
                self.enclave = "enclave.example"

            def verify(self):
                return fetch_latest_release("owner/repo")
        """,
    # As tinfoil.github and tinfoil.attestation.* take it.
    "github.py": """
        import requests

        def fetch_latest_release(repo):
            response = requests.get(f"https://github-proxy.example/repos/{repo}", timeout=5)
            response.raise_for_status()
            return response.text
        """,
    # A function taken by name, and Session in an annotation made at import.
    "byname.py": """
        from typing import Optional

        import requests
        from requests import post

        HttpSession = Optional[requests.Session]

        def send(url, session: HttpSession = None):
            if session is not None:
                return session.get(url, timeout=5).text
            return post(url, json={}, timeout=5).text
        """,
    # A module the package doesn't import until it is wanted.
    "later/__init__.py": "",
    "later/collateral.py": """
        import requests

        def fetch(url):
            return requests.get(url, timeout=5).text
        """,
}
URL = "https://github-proxy.example/repos/owner/repo"


class Network:
    """Each URL answered as `answers` says (a redirect to another, or 200), and every request that
    reached it recorded in `sent`."""

    def __init__(self):
        self.sent: list[str] = []
        self.answers: dict[str, str] = {}

    def send(self, request):
        self.sent.append(request.url)
        response = requests.Response()
        response.url, response.request = request.url, request
        target = self.answers.get(request.url)
        response.status_code = 302 if target else 200
        if target:
            response.headers["Location"] = target
        response._content = b"" if target else b"fine"
        return response


@pytest.fixture
def net(tmp_path, monkeypatch):
    """The fake SDK in place of the real one (back afterwards), and the fake network."""
    for name, text in FILES.items():
        path = tmp_path / "tinfoil" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
    saved = dict(sys.modules)
    for name in [n for n in sys.modules if n == "tinfoil" or n.startswith("tinfoil.")]:
        del sys.modules[name]
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    importlib.import_module("tinfoil")
    network = Network()
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send",
                        lambda adapter, request, **kwargs: network.send(request))
    yield network
    for name in [n for n in sys.modules if n == "tinfoil" or n.startswith("tinfoil.")]:
        del sys.modules[name]
    sys.modules.update({n: m for n, m in saved.items() if n == "tinfoil" or n.startswith("tinfoil.")})


def module(name):
    return importlib.import_module(f"tinfoil.{name}")


def test_unguarded_the_sdk_follows_one_into_the_clear(net):
    # What the SDK does by itself, and why: the plain http fetch went out.
    net.answers[URL] = "http://evil.example/release"
    assert module("github").fetch_latest_release("owner/repo") == "fine"
    assert net.sent == [URL, "http://evil.example/release"]


def test_guarded_it_is_refused_and_nothing_is_sent_there(net):
    sdk_guard.guard(netguard.is_local)
    net.answers[URL] = "http://evil.example/release"
    with pytest.raises(requests.RequestException, match="isn't https; not followed"):
        module("github").fetch_latest_release("owner/repo")
    assert net.sent == [URL]


def test_one_from_the_internet_into_this_network_is_refused(net):
    sdk_guard.guard(netguard.is_local)
    net.answers[URL] = "https://192.168.1.1/admin"
    with pytest.raises(requests.RequestException, match="on this computer or its network; not followed"):
        module("github").fetch_latest_release("owner/repo")
    assert net.sent == [URL]


def test_a_redirect_that_stays_on_https_is_followed_and_a_relative_one_too(net):
    sdk_guard.guard(netguard.is_local)
    net.answers[URL] = "https://objects.example/release"
    assert module("github").fetch_latest_release("owner/repo") == "fine"
    assert net.sent == [URL, "https://objects.example/release"]
    net.sent.clear()
    net.answers[URL] = "/releases/latest"
    assert module("github").fetch_latest_release("owner/repo") == "fine"
    assert net.sent == [URL, "https://github-proxy.example/releases/latest"]


def test_a_function_taken_by_name_and_a_module_loaded_later_are_guarded(net):
    given = sdk_guard.guard(netguard.is_local)
    assert "tinfoil.byname" in given and "tinfoil.later.collateral" in given
    for send in (module("byname").send, module("later.collateral").fetch):
        net.sent.clear()
        net.answers["https://kds.example/vcek"] = "http://kds.example/vcek"
        with pytest.raises(requests.RequestException, match="isn't https"):
            send("https://kds.example/vcek")
        assert net.sent == ["https://kds.example/vcek"]


def test_a_session_of_its_own_is_guarded_too(net):
    sdk_guard.guard(netguard.is_local)
    session = module("byname").requests.Session()
    assert isinstance(session, requests.Session)
    net.answers["https://pcs.example/tcb"] = "http://pcs.example/tcb"
    with pytest.raises(requests.RequestException, match="isn't https"):
        module("byname").send("https://pcs.example/tcb", session)


def test_the_rest_of_requests_is_requests_own_and_twice_is_once(net):
    sdk_guard.guard(netguard.is_local)
    stand_in = module("github").requests
    assert stand_in.RequestException is requests.RequestException
    assert stand_in.Response is requests.Response
    assert sdk_guard.guard(netguard.is_local) == []
    assert module("github").requests is stand_in
    assert sys.modules["requests"] is requests            # the app's own untouched


def test_attesting_guards_the_sdk_first(net):
    net.answers[URL] = "http://evil.example/release"
    with pytest.raises(requests.RequestException, match="isn't https"):
        private_mode.tinfoil_verify("https://nano-gpt.com/api/v1/private/tinfoil", "s" * 64)
    assert net.sent == [URL]


def test_without_the_sdk_there_is_nothing_to_guard(net):
    del sys.modules["tinfoil"]
    assert sdk_guard.guard(netguard.is_local) == []
