"""WinRM hosts: plain HTTP only on the local network, Basic over it only with the technician's
say-so, and the certificate checked by DA Toolkit (pinned) when the system CA store isn't used."""

import asyncio
import ssl

import pytest

from datoolkit.config import Host
from datoolkit.engine import UserError
from datoolkit.sessions import rdpcert, winrm

from test_engine import env  # noqa: F401
from test_rdp import _self_signed, answer_prompts


def test_plain_http_rules():
    assert winrm.problem(Host("a", "winrm", "dc01.corp.example", auth="ntlm")) is None          # HTTPS
    assert "HTTPS" in winrm.problem(Host("a", "winrm", "dc01.corp.example", auth="ntlm", winrm_ssl=False))
    assert winrm.problem(Host("a", "winrm", "192.168.1.14", auth="ntlm", winrm_ssl=False)) is None
    assert winrm.problem(Host("a", "winrm", "dc01.local", auth="kerberos", winrm_ssl=False)) is None
    assert "clear" in winrm.problem(Host("a", "winrm", "192.168.1.14", auth="basic", winrm_ssl=False))
    assert winrm.problem(Host("a", "winrm", "192.168.1.14", auth="basic", winrm_ssl=False, winrm_plain_basic=True)) is None
    assert "HTTPS" in winrm.problem(Host("a", "winrm", "203.0.113.5", auth="basic", winrm_ssl=False, winrm_plain_basic=True))
    assert winrm.port(Host("a", "winrm", "h")) == 5986 and winrm.port(Host("a", "winrm", "h", winrm_ssl=False)) == 5985
    assert winrm.pins(Host("a", "winrm", "h", winrm_cert_validation=False))
    assert not winrm.pins(Host("a", "winrm", "h")) and not winrm.pins(Host("a", "winrm", "h", winrm_ssl=False, winrm_cert_validation=False))


async def test_save_host_refuses_what_the_rules_refuse(env):  # noqa: F811
    engine, _, _ = env
    base = {"name": "dc", "kind": "winrm", "host": "203.0.113.5", "user": "admin", "auth": "ntlm"}
    with pytest.raises(UserError, match="Host not saved.*HTTPS"):
        engine.save_host({**base, "winrm_ssl": False})
    with pytest.raises(UserError, match="clear"):
        engine.save_host({**base, "host": "192.168.1.14", "winrm_ssl": False, "auth": "basic"})
    engine.save_host({**base, "host": "192.168.1.14", "winrm_ssl": False, "auth": "basic", "winrm_plain_basic": True})
    assert engine.cfg.host("dc").winrm_plain_basic is True
    # a config edited by hand is checked again where the session opens
    engine.cfg.host("dc").host = "203.0.113.5"
    engine.new_case("c", "open")
    with pytest.raises(UserError, match="HTTPS"):
        engine.open_session("winrm", "dc")
    with pytest.raises(UserError, match="HTTPS"):
        await engine.open_winrm("dc")


async def tls_server(cert, key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)

    async def handle(reader, writer):
        try:
            await reader.read(1)
        finally:
            writer.close()
    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
    return server, server.sockets[0].getsockname()[1]


async def test_fetch_tls_reads_the_listener_certificate(tmp_path):
    c, k, fp = _self_signed(tmp_path, "win10.lan")
    server, port = await tls_server(c, k)
    async with server:
        got = await rdpcert.fetch_tls("127.0.0.1", port)
    assert got["tls"] and got["sha256"] == fp and "win10.lan" in got["subject"] and got["self_signed"]
    with pytest.raises(rdpcert.CertError, match="cannot reach"):
        await rdpcert.fetch_tls("127.0.0.1", 1)


async def test_pinned_winrm_host_is_checked_before_the_console_starts(env, tmp_path, monkeypatch):  # noqa: F811
    engine, _, events = env
    engine.new_case("c", "open")
    c1, k1, fp1 = _self_signed(tmp_path, "win10")
    server, port = await tls_server(c1, k1)
    engine.cfg.hosts.append(Host("win10", "winrm", "127.0.0.1", port=port, user="admin", auth="ntlm",
                                 winrm_cert_validation=False))
    opened = []
    monkeypatch.setattr(engine, "open_session", lambda kind, host_name="", _cert_checked=False:
                        opened.append((kind, host_name, _cert_checked)) or {"id": host_name, "kind": kind})
    async with server:
        # not pinned yet: the technician is shown the fingerprint and accepts it
        opener = asyncio.create_task(engine.open_winrm("win10"))
        await answer_prompts(engine, ["yes"])
        assert (await opener)["id"] == "win10"
        text = [e for e in events if e["type"] == "prompt"][-1]["prompt"]["text"]
        assert fp1 in text and "winrm/config/listener" in text
        assert opened == [("winrm", "win10", True)]
        assert engine.pins.get("127.0.0.1", port)["sha256"] == fp1
        assert engine.snapshot()["config"]["hosts"][-1]["pinned"] == fp1
        assert "winrm_cert_pinned" in (engine.case.dir / "events.jsonl").read_text()
        # pinned: no prompt the second time
        n = len([e for e in events if e["type"] == "prompt"])
        await engine.open_winrm("win10")
        assert len([e for e in events if e["type"] == "prompt"]) == n
    # the listener's certificate changes: refused
    c2, k2, _ = _self_signed(tmp_path, "evil")
    server, port2 = await tls_server(c2, k2)
    engine.cfg.hosts[-1].port = port2
    engine.pins.pin("127.0.0.1", port2, {"sha256": fp1, "subject": "", "issuer": "", "not_after": ""})
    async with server:
        with pytest.raises(UserError, match="CHANGED"):
            await engine.open_winrm("win10")
        assert "winrm_cert_mismatch" in (engine.case.dir / "events.jsonl").read_text()
        assert engine.forget_pin("win10") is True
        assert engine.snapshot()["config"]["hosts"][-1]["pinned"] == ""
    # a host checked by the CA store, or over HTTP, has no pin to forget
    assert engine.forget_pin("web01") is False


async def test_pinned_host_never_opens_unchecked(env):  # noqa: F811
    engine, _, _ = env
    engine.new_case("c", "open")
    engine.cfg.hosts.append(Host("win10", "winrm", "192.168.1.14", user="admin", auth="ntlm", winrm_cert_validation=False))
    with pytest.raises(UserError, match="checked before every connection"):
        engine.open_session("winrm", "win10")


def _tls_server(tmp_path):
    """A local HTTPS server with a self-signed certificate; returns (port, SHA-256 of its certificate, stop)."""
    import datetime
    import http.server
    import ssl
    import threading

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    from datoolkit.sessions import rdpcert

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "winrm.test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    (tmp_path / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "k.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()))

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
        do_GET = do_POST

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def stop():
        httpd.shutdown()
        httpd.server_close()
    return httpd.server_address[1], rdpcert.fingerprint(cert.public_bytes(serialization.Encoding.DER)), stop


def test_the_console_checks_the_pin_on_its_own_connections(tmp_path):
    # The engine checks the pin on a probe connection of its own; the console's connections,
    # which carry the login, must present the same certificate or nothing is sent.
    import requests
    from pypsrp.wsman import WSMan

    from datoolkit.sessions.psrp_console import pin_certificate

    port, good, stop = _tls_server(tmp_path)
    try:
        for pin, ok in ((good, True), ("AB:" * 31 + "AB", False)):
            wsman = WSMan("127.0.0.1", port=port, username="bob", password="pw", ssl=True, auth="basic",
                          cert_validation=False, operation_timeout=5, read_timeout=10)
            pin_certificate(wsman, pin)
            session = wsman.transport._build_session()
            try:
                if ok:
                    assert session.post(wsman.transport.endpoint, timeout=10).status_code == 401
                else:
                    with pytest.raises(requests.exceptions.SSLError, match="(?i)fingerprint"):
                        session.post(wsman.transport.endpoint, timeout=10)
            finally:
                session.close()
    finally:
        stop()


def test_a_pinned_hosts_console_is_given_the_pin():
    import json
    pinned = Host("dc", "winrm", "dc.lan", winrm_cert_validation=False)
    assert json.loads(winrm.console_config(pinned, "AA:BB"))["pin"] == "AA:BB"
    assert "pin" not in json.loads(winrm.console_config(Host("dc", "winrm", "dc.lan")))


def test_the_console_prints_write_host_and_verbose_output(monkeypatch, capsys):
    from types import SimpleNamespace

    import pypsrp.powershell
    from pypsrp.complex_objects import PSInvocationState

    from datoolkit.sessions import psrp_console

    class FakePowerShell:            # what pypsrp returns for: Write-Host "copied"; Write-Verbose "checking" -Verbose; "done"
        def __init__(self, pool):
            self.state = PSInvocationState.COMPLETED
            self.output = ["done\n"]
            self.streams = SimpleNamespace(information=[SimpleNamespace(message_data="copied 3 files")],
                                           verbose=[SimpleNamespace(message="checking the share")], warning=[], error=[])

        def add_script(self, s):
            return self

        def add_cmdlet(self, c):
            return self

        def add_parameter(self, k, v):
            return self

        def begin_invoke(self):
            pass

    monkeypatch.setattr(pypsrp.powershell, "PowerShell", FakePowerShell)
    psrp_console.run_command(None, "Copy-Things")
    out = capsys.readouterr().out
    assert "copied 3 files" in out and "done" in out and "VERBOSE: checking the share" in out


class _RunningPowerShell:
    """A fake pypsrp pipeline that stays running: each poll delivers the next batch of output
    lines (as Out-String -Stream sends them), and `during_poll` runs inside a poll, while the
    exchange with the server would be in flight."""

    instances: list = []

    def __init__(self, pool, batches=(), during_poll=None):
        from types import SimpleNamespace

        from pypsrp.complex_objects import PSInvocationState

        self.RUNNING, self.STOPPED = PSInvocationState.RUNNING, PSInvocationState.STOPPED
        self.state = PSInvocationState.NOT_STARTED
        self.output, self.batches, self.during_poll = [], list(batches), during_poll
        self.streams = SimpleNamespace(information=[], verbose=[], warning=[], error=[])
        self.params, self.polls, self.stops = {}, [], 0
        _RunningPowerShell.instances.append(self)

    def add_script(self, s):
        return self

    def add_cmdlet(self, c):
        return self

    def add_parameter(self, k, v=None):
        self.params[k] = v
        return self

    def begin_invoke(self):
        self.state = self.RUNNING

    def poll_invoke(self, timeout=None):
        self.polls.append(timeout)
        if self.during_poll:
            self.during_poll(self)
        if self.batches:
            self.output.extend(self.batches.pop(0))

    def stop(self):
        self.stops += 1
        self.state = self.STOPPED


def test_ctrl_c_stops_the_command_between_polls_and_keeps_its_output(monkeypatch, capsys):
    """Seen against Windows 11 over plain HTTP with NTLM: a KeyboardInterrupt raised inside a
    receive left pypsrp unable to talk to the server (every later request, the stop included,
    got HTTP 400). Ctrl+C now only marks the command; it is stopped between polls."""
    import os
    import signal

    import pypsrp.powershell

    from datoolkit.sessions import psrp_console

    def sigint_mid_exchange(ps):
        if len(ps.polls) == 2:
            os.kill(os.getpid(), signal.SIGINT)      # delivered while "the receive is in flight"

    monkeypatch.setattr(pypsrp.powershell, "PowerShell", lambda pool: _RunningPowerShell(
        pool, batches=[["tick 1"], ["tick 2"], ["tick 3"], ["tick 4"]], during_poll=sigint_mid_exchange))
    old = signal.signal(signal.SIGINT, psrp_console._interrupt)
    try:
        psrp_console.run_command(None, "1..9 | % { \"tick $_\"; sleep 2 }")    # no KeyboardInterrupt escapes
    finally:
        signal.signal(signal.SIGINT, old)
    ps = _RunningPowerShell.instances[-1]
    out = capsys.readouterr().out
    assert ps.stops == 1 and len(ps.polls) == 2                      # stopped right after the poll it hit
    assert all(t == psrp_console.POLL_SECONDS for t in ps.polls)      # short polls: Ctrl+C acts within about a second
    assert "tick 1" in out and "tick 2" in out and "tick 3" not in out    # what came before the stop is kept
    assert out.index("tick 2") < out.index("(stopped)")
    assert ps.params.get("Stream") is True and "Width" in ps.params
    # a Ctrl+C pressed at the prompt, before a command, doesn't stop the next one
    psrp_console._interrupt.pending = True

    def finish(ps):
        if len(ps.polls) == 2:
            ps.state = pypsrp.powershell.PSInvocationState.COMPLETED
    monkeypatch.setattr(pypsrp.powershell, "PowerShell", lambda pool: _RunningPowerShell(pool, batches=[["only"]], during_poll=finish))
    psrp_console.run_command(None, "'only'")
    ps2 = _RunningPowerShell.instances[-1]
    assert ps2.stops == 0 and "only" in capsys.readouterr().out


def test_the_console_prints_output_as_it_arrives(monkeypatch, capsys):
    """A slow command (or a watch) shows each line when it comes, not all of it at the end;
    PowerShell's padding blank lines after a table don't pile up."""
    import pypsrp.powershell
    from pypsrp.complex_objects import PSInvocationState

    from datoolkit.sessions import psrp_console

    seen_during = []

    def look(ps):
        seen_during.append(capsys.readouterr().out)
        if len(ps.polls) == 3:
            ps.state = PSInvocationState.COMPLETED

    monkeypatch.setattr(pypsrp.powershell, "PowerShell", lambda pool: _RunningPowerShell(
        pool, batches=[["", "Status  Name", "------  ----"], ["Running WinRM", "", ""], []], during_poll=look))
    psrp_console.run_command(None, "Get-Service WinRM")
    rest = capsys.readouterr().out
    assert seen_during[1] == "\nStatus  Name\n------  ----\n"     # the first lines, while it still ran
    assert seen_during[2] == "Running WinRM\n" and rest == ""      # trailing blank lines dropped


def test_kerberos_without_gssapi_says_what_is_missing():
    from datoolkit.sessions import psrp_console

    e = ImportError("GSSAPIProxy requires the Python gssapi library: No module named 'gssapi'")
    msg = psrp_console._connection_problem(e, "kerberos")
    assert "gssapi" in msg and "krb5" in msg and "NTLM" in msg and "ImportError" not in msg
    assert psrp_console._connection_problem(OSError("refused"), "ntlm") == "OSError: refused"


def test_basic_refused_by_windows_names_its_defaults():
    """Windows 11 with Basic off (its default), or Basic on but over HTTP without
    AllowUnencrypted, answers like a wrong password."""
    from pypsrp.exceptions import AuthenticationError

    from datoolkit.sessions import psrp_console

    e = AuthenticationError("Failed to authenticate the user test with basic")
    over_http = psrp_console._connection_problem(e, "basic", ssl=False)
    assert "Failed to authenticate" in over_http and "AllowUnencrypted" in over_http and "off by default" in over_http
    assert "AllowUnencrypted" not in psrp_console._connection_problem(e, "basic", ssl=True)
    assert "off by default" not in psrp_console._connection_problem(AuthenticationError("x"), "ntlm")


async def test_the_certificate_prompt_shows_the_thumbprint_windows_shows(env, tmp_path):  # noqa: F811
    """Windows names a certificate by its SHA-1 thumbprint (certlm.msc, `winrm enumerate`, the
    Cert: drive), so the technician is shown that beside the SHA-256 that is pinned."""
    import hashlib

    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    engine, _, events = env
    engine.new_case("c", "open")
    c1, k1, fp1 = _self_signed(tmp_path, "win11")
    der = x509.load_pem_x509_certificate(c1.read_bytes()).public_bytes(serialization.Encoding.DER)
    thumb = hashlib.sha1(der).hexdigest().upper()
    server, port = await tls_server(c1, k1)
    async with server:
        got = await rdpcert.fetch_tls("127.0.0.1", port)
        assert got["sha1"] == thumb and got["sha256"] == fp1
        engine.cfg.hosts.append(Host("win11", "winrm", "127.0.0.1", port=port, user="test", auth="ntlm",
                                     winrm_cert_validation=False))
        engine.open_session = lambda kind, host_name="", _cert_checked=False: {"id": host_name}
        opener = asyncio.create_task(engine.open_winrm("win11"))
        await answer_prompts(engine, ["yes"])
        await opener
    text = [e for e in events if e["type"] == "prompt"][-1]["prompt"]["text"]
    assert f"SHA-256: {fp1}" in text and f"Thumbprint (SHA-1, as Windows shows it): {thumb}" in text
    assert engine.pins.get("127.0.0.1", port)["sha256"] == fp1 and "sha1" not in engine.pins.get("127.0.0.1", port)

