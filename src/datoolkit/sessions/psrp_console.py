"""Minimal interactive PowerShell Remoting (WinRM) console, run inside a PTY by the app.

Connection settings arrive as JSON in DATOOLKIT_PSRP (no secrets); the password is fetched
through the askpass bridge. Limitations: single-line input, no Read-Host/Get-Credential
prompts, no tab completion.
"""

from __future__ import annotations

import json
import os
import readline  # noqa: F401 - enables line editing and history for input()
import shutil
import signal
import sys

RED, YELLOW, DIM, RESET = "\x1b[31m", "\x1b[33m", "\x1b[2m", "\x1b[0m"


def _password(cfg: dict) -> str | None:
    from datoolkit.sessions.askpass import request

    return request(f"Password for {cfg.get('user')}@{cfg['host']} (WinRM):", kind="password")


def run_command(pool, line: str) -> None:
    from pypsrp.complex_objects import PSInvocationState
    from pypsrp.powershell import PowerShell

    width = max(80, shutil.get_terminal_size((200, 40)).columns - 1)
    ps = PowerShell(pool)
    ps.add_script(line).add_cmdlet("Out-String").add_parameter("Width", width)
    ps.begin_invoke()
    try:
        while ps.state == PSInvocationState.RUNNING:
            ps.poll_invoke()
    except KeyboardInterrupt:
        print("^C")
        try:
            ps.stop()
        except Exception as e:  # noqa: BLE001
            print(f"{DIM}(stop failed: {e}){RESET}")
        return
    out = "".join(str(o) for o in ps.output)
    if out.strip():
        sys.stdout.write(out.rstrip("\n") + "\n")
    for w in ps.streams.warning:
        print(f"{YELLOW}WARNING: {w}{RESET}")
    for e in ps.streams.error:
        print(f"{RED}{e}{RESET}")
    if ps.state == PSInvocationState.FAILED and not ps.streams.error:
        print(f"{RED}Command failed.{RESET}")


def current_location(pool) -> str:
    from pypsrp.powershell import PowerShell

    try:
        ps = PowerShell(pool)
        ps.add_script("(Get-Location).Path")
        out = ps.invoke()
        return str(out[0]) if out else ""
    except Exception:  # noqa: BLE001
        return ""


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
        wsman = WSMan(host, port=port, username=cfg.get("user") or None, password=password, ssl=ssl,
                      auth=cfg.get("auth", "negotiate"), cert_validation=bool(cfg.get("cert_validation", True)),
                      operation_timeout=20, read_timeout=30)
        with wsman, RunspacePool(wsman) as pool:
            del password
            print(f"Connected. Type PowerShell commands (single line). 'exit' to disconnect.\n")
            while True:
                loc = current_location(pool)
                try:
                    line = input(f"[{host}]: PS {loc}> ")
                except KeyboardInterrupt:
                    print("^C")
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
                except KeyboardInterrupt:
                    print("^C")
                except Exception as e:  # noqa: BLE001
                    print(f"{RED}{type(e).__name__}: {e}{RESET}")
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001
        print(f"{RED}Connection failed: {type(e).__name__}: {e}{RESET}")
        return 1
    print("Disconnected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
