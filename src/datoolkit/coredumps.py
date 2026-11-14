"""No core dumps: a native crash must not write the process's memory to disk.

DA Toolkit holds what a technician gives it in memory: the passwords and keys of SSH, WinRM and
RDP sessions (answered through the askpass bridge, held by the WinRM console's own process), the
AI providers' keys, and every case's conversation. A core file of any of its processes would put
them on disk, readable by whoever reads the crash. So each entry point (the app, `python -m
datoolkit`, datoolkit-replay, the askpass helper ssh runs, the WinRM console) calls
`no_core_dumps` first, the same three steps as every DA app (the cross-app standard, Nov 2026):

- /proc/self/coredump_filter 0 (DA Privacy Kit's): whatever core is written anyway holds none of
  the process's memory. Written first: once the process isn't dumpable its own /proc files are
  root's, this one included, and the write fails (found by DA Vibe Manager's and DA Private
  Coder's tests in a real process);
- the core size's soft limit 1: the kernel aborts the core of a process whose limit is exactly 1,
  even of a dumpable one ("RLIMIT_CORE is set to 1, aborting core"), where a limit of 0 alone is
  ignored by systemd-coredump's pipe (measured on MintDev, Nov 14 2026). The hard limit is left as
  it was, so a program the app starts (the technician's shells, ssh) may raise the soft limit again
  (`ulimit -c`), which the hard 0 it had until Nov 2026 never let it;
- not dumpable (`PR_SET_DUMPABLE` 0): the kernel writes no core of the process, so neither does the
  system's collector (systemd-coredump, apport, which read the flag rather than the limit), and, as
  a side effect, no other process attaches a debugger. A program the process starts is dumpable
  again after its exec (ssh, the shells); the limit and the filter are inherited.

Linux only, as the app is, and any failure is ignored: the app runs as before where the calls
aren't there. DA Toolkit keeps no faulthandler crash log of its own, so nothing else changes.
From SealedLore (gui/app.py `_no_core_dumps`, Nov 2026).
"""

from __future__ import annotations

import sys

PR_SET_DUMPABLE = 4


def no_core_dumps() -> None:
    """No core file and not dumpable, for this process (Linux; failures ignored)."""
    if not sys.platform.startswith("linux"):
        return
    try:
        # first: once not dumpable, the process's own /proc files are root's, this one included
        with open("/proc/self/coredump_filter", "w", encoding="ascii") as f:
            f.write("0")
    except OSError:
        pass
    try:
        import resource

        _soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
        soft = 1 if hard == resource.RLIM_INFINITY else min(1, hard)
        resource.setrlimit(resource.RLIMIT_CORE, (soft, hard))
    except (ImportError, ValueError, OSError):
        pass
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
    except (OSError, AttributeError):
        pass
