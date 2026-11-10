"""Minimal interactive PowerShell Remoting (WinRM) console, run inside a PTY by the app.

Connection settings arrive as JSON in DATOOLKIT_PSRP (no secrets); the password is fetched
through the askpass bridge. Limitations: single-line input, no Read-Host/Get-Credential
prompts, no tab completion.

Ctrl+C never interrupts an exchange with the server. Over plain HTTP, NTLM and Negotiate
encrypt every message on the authenticated connection, and a request cut off half way
leaves pypsrp unable to talk to the server again: every later command, and the stop itself,
got "Bad HTTP response ... 400". So Ctrl+C only sets a flag while a command runs; the
command is polled in short slices and stopped between them (POLL_SECONDS).
"""

from __future__ import annotations

import json
import os
import readline  # noqa: F401 - enables line editing and history for input()
import shutil
import signal
import sys

RED, YELLOW, DIM, RESET = "\x1b[31m", "\x1b[33m", "\x1b[2m", "\x1b[0m"
POLL_SECONDS = 1     # longest a Ctrl+C waits for the receive in flight before the stop is sent


class _Interrupt:
    """SIGINT while a command runs or the console talks to the server: noted, not raised."""

    def __init__(self) -> None:
        self.pending = False

    def __call__(self, signum, frame) -> None:
        self.pending = True

    def take(self) -> bool:
        was, self.pending = self.pending, False
        return was


_interrupt = _Interrupt()


def _password(cfg: dict) -> str | None:
    from datoolkit.sessions.askpass import request

    return request(f"Password for {cfg.get('user')}@{cfg['host']} (WinRM):", kind="password")


class _Printer:
    """Prints what a running command has produced so far, each stream from where it left off.
    Output lines come one at a time (Out-String -Stream); blank lines are held back until
    something follows them, so the blank lines PowerShell puts after a table don't pile up."""

    def __init__(self, ps) -> None:
        self.ps = ps
        self.seen = {"information": 0, "output": 0, "verbose": 0, "warning": 0, "error": 0}
        self.blank = 0

    def _line(self, text: str) -> None:
        if not text.strip():
            self.blank += 1
            return
        sys.stdout.write("\n" * self.blank + text + "\n")
        self.blank = 0

    def _new(self, name: str, items: list) -> list:
        start, self.seen[name] = self.seen[name], len(items)
        return items[start:]

    def flush(self) -> None:
        s = self.ps.streams
        # Write-Host and Write-Information (PowerShell 5 and later) go to the information
        # stream, not the output: a script reporting with them would print nothing
        for record in self._new("information", s.information):
            text = str(getattr(record, "message_data", record) or "")
            if text.strip():
                self._line(text.rstrip("\r\n"))
        for o in self._new("output", self.ps.output):
            for line in str(o).replace("\r\n", "\n").split("\n"):
                self._line(line.rstrip("\r"))
        for v in self._new("verbose", s.verbose):
            self._line(f"{DIM}VERBOSE: {getattr(v, 'message', v)}{RESET}")
        for w in self._new("warning", s.warning):
            self._line(f"{YELLOW}WARNING: {w}{RESET}")
        for e in self._new("error", s.error):
            self._line(f"{RED}{e}{RESET}")
        sys.stdout.flush()


def run_command(pool, line: str) -> None:
    from pypsrp.complex_objects import PSInvocationState
    from pypsrp.powershell import PowerShell

    width = max(80, shutil.get_terminal_size((200, 40)).columns - 1)
    ps = PowerShell(pool)
    # -Stream: each line as soon as it is formatted, so a slow command (or a watch) shows its
    # output as it goes, and what came before an error that ends the command isn't lost with it
    ps.add_script(line).add_cmdlet("Out-String").add_parameter("Stream", True).add_parameter("Width", width)
    printer = _Printer(ps)
    _interrupt.take()                 # a Ctrl+C from before the command doesn't stop it
    ps.begin_invoke()
    while ps.state == PSInvocationState.RUNNING:
        if _interrupt.take():
            try:
                ps.stop()
                print(f"\n{DIM}(stopped){RESET}")
            except Exception as e:  # noqa: BLE001
                print(f"\n{DIM}(stop failed: {e}){RESET}")
            return
        ps.poll_invoke(timeout=POLL_SECONDS)
        printer.flush()
    printer.flush()
    if ps.state == PSInvocationState.FAILED and not ps.streams.error:
        print(f"{RED}Command failed.{RESET}")


def _read_line(prompt: str) -> str:
    """input() with Ctrl+C raising KeyboardInterrupt: nothing is being sent while it waits."""
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        return input(prompt)
    finally:
        signal.signal(signal.SIGINT, _interrupt)


# What MIT Kerberos says when it fails, and what a technician can do about it. Each was seen
# against the lab's Samba AD domain (Nov 2026); the messages come from gssapi and krb5.
_KERBEROS = (
    ("Cannot find KDC for realm", "this computer can't find a domain controller (KDC) for the realm. Kerberos "
     "finds it through DNS (_kerberos._udp.<realm>), so this computer must use the domain's DNS (on its network or "
     "over the VPN), or /etc/krb5.conf must name the KDC under [realms]. The realm is the domain in capitals."),
    ("Preauthentication failed", "the domain refused the password."),
    ("Server not found in Kerberos database", "the domain has no service of that name: connect by the computer's "
     "full DNS name as the domain knows it (e.g. fs01.corp.example.com), not an address or another alias."),
    ("not found in Kerberos database", "the domain doesn't know this user: give it as user@REALM."),
    ("Clock skew too great", "this computer's clock and the domain's differ by more than five minutes."),
    ("Client's credentials have been revoked", "the domain refused this account (disabled or locked out)."),
    ("Password has expired", "the account's password has expired: change it first."),
)


def _connection_problem(e: Exception, auth: str, ssl: bool = True) -> str:
    text = str(e)
    if isinstance(e, ImportError) or "requires the Python gssapi library" in text:
        if "libgssapi_krb5" in text:
            return (f"{auth.capitalize()} authentication needs MIT Kerberos' libgssapi_krb5 on this computer "
                    "(libgssapi-krb5-2 on Debian and Ubuntu, krb5-libs on Fedora, krb5 on Arch). Use NTLM without it.")
        if "gssapi" in text.lower():
            return (f"{auth.capitalize()} authentication needs the Python gssapi package, which this installation "
                    "doesn't have (the AppImage does; from source: pip install 'datoolkit[kerberos]', with MIT krb5's "
                    "headers to build it). Use NTLM or Negotiate, which uses NTLM without it.")
    if auth in ("kerberos", "negotiate"):
        for needle, meaning in _KERBEROS:
            if needle in text:
                return f"Kerberos: {meaning} ({needle})"
    text = f"{type(e).__name__}: {e}"
    if auth == "basic" and type(e).__name__ == "AuthenticationError":
        # Windows turns Basic away without saying why, which reads like a wrong password
        text += (". Besides a wrong password, this is what Windows answers when it doesn't accept Basic: it is off by "
                 "default (winrm/config/service/auth Basic)" + ("" if ssl else ", and over HTTP it also needs "
                 "AllowUnencrypted") + ". NTLM needs neither.")
    return text


def current_location(pool) -> str:
    from pypsrp.powershell import PowerShell

    try:
        ps = PowerShell(pool)
        ps.add_script("(Get-Location).Path")
        out = ps.invoke()
        return str(out[0]) if out else ""
    except Exception:  # noqa: BLE001
        return ""


def pin_certificate(wsman, sha256: str) -> None:
    """Every TLS connection pypsrp opens must present the certificate with this SHA-256: urllib3
    compares it before a byte of the login is sent. The engine checked the pin on a connection
    of its own first, which can't vouch for these (one let through, the next intercepted).
    Added to pypsrp's own adapter, which also controls when new connections may be made."""
    transport = wsman.transport
    build = transport._build_session

    def pinned_session():
        session = build()
        adapter = session.get_adapter("https://")
        adapter.poolmanager.connection_pool_kw["assert_fingerprint"] = sha256
        through_proxy = adapter.proxy_manager_for

        def proxy_manager_for(proxy, **kwargs):     # an https_proxy from the environment
            return through_proxy(proxy, **{**kwargs, "assert_fingerprint": sha256})
        adapter.proxy_manager_for = proxy_manager_for
        return session
    transport._build_session = pinned_session


def main() -> int:
    from pypsrp.powershell import RunspacePool
    from pypsrp.wsman import WSMan

    cfg = json.loads(os.environ["DATOOLKIT_PSRP"])
    host = cfg["host"]
    ssl = bool(cfg.get("ssl", True))
    port = cfg.get("port") or (5986 if ssl else 5985)
    password = _password(cfg)
    if password is None:
        print(f"{RED}No password provided.{RESET}")
        return 1

    print(f"{DIM}Connecting to {host}:{port} over {'HTTPS' if ssl else 'HTTP'} ({cfg.get('auth', 'negotiate')})...{RESET}")
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        auth = cfg.get("auth", "negotiate")
        # Basic over plain HTTP (only with the host's "Allow Basic over plain HTTP" tick, on the
        # local network: winrm.problem) has no message encryption, which pypsrp otherwise insists on
        wsman = WSMan(host, port=port, username=cfg.get("user") or None, password=password, ssl=ssl,
                      auth=auth, cert_validation=bool(cfg.get("cert_validation", True)),
                      encryption="never" if not ssl and auth == "basic" else "auto",
                      operation_timeout=20, read_timeout=30)
        if ssl and cfg.get("pin"):
            pin_certificate(wsman, cfg["pin"])
        elif ssl and not cfg.get("cert_validation", True):
            print(f"{RED}No pinned certificate to check this connection against: not connecting.{RESET}")
            return 1
        with wsman, RunspacePool(wsman) as pool:
            del password
            # from here on the server is never cut off mid-request (see the module docstring)
            signal.signal(signal.SIGINT, _interrupt)
            print("Connected. Type PowerShell commands (single line). 'exit' to disconnect.\n")
            while True:
                loc = current_location(pool)
                try:
                    line = _read_line(f"[{host}]: PS {loc}> ")
                except KeyboardInterrupt:
                    print()
                    continue
                except EOFError:
                    print()
                    break
                if not line.strip():
                    continue
                if line.strip().lower() in ("exit", "exit-pssession", "quit"):
                    break
                try:
                    run_command(pool, line)
                except Exception as e:  # noqa: BLE001
                    print(f"{RED}{type(e).__name__}: {e}{RESET}")
    except KeyboardInterrupt:          # while connecting
        return 130
    except Exception as e:  # noqa: BLE001
        print(f"{RED}Connection failed: {_connection_problem(e, cfg.get('auth', 'negotiate'), ssl)}{RESET}")
        return 1
    print("Disconnected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
