"""The environment for programs DAToolkit starts on the technician's behalf (their shell, ssh,
the file manager). When DAToolkit runs from an AppImage, the AppImage runtime adds variables
describing its own mount; they mean nothing to those programs, and another AppImage started
from a DAToolkit terminal would misread them, so they are left out. So is what the AppImage's
AppRun sets for DAToolkit's own TLS (see _set_by_apprun)."""

from __future__ import annotations

import os

# AppRun's list of what it set (see _set_by_apprun) is the AppImage's own, like the runtime's variables
APPRUN_SET = "DATOOLKIT_APPRUN_SET"
APPIMAGE_VARS = ("APPIMAGE", "APPDIR", "ARGV0", "OWD", "APPIMAGE_EXTRACT_AND_RUN", APPRUN_SET)
# The host CA bundles AppRun (packaging/appimage/AppRun) looks for, in its order: it points
# SSL_CERT_FILE at the first one there is, and REQUESTS_CA_BUNDLE at the same, when the
# technician's environment sets neither.
APPRUN_CA_BUNDLES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt",
                     "/etc/ssl/ca-bundle.pem", "/etc/pki/tls/cert.pem", "/etc/ssl/cert.pem")
CA_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")
# variables DAToolkit changed for itself, with their values before (None: they weren't set)
ORIGINAL: dict[str, str | None] = {}


def set_for_self(name: str, value: str) -> None:
    """Set an environment variable for DAToolkit's own process only: host_env() gives children
    the value it had before."""
    ORIGINAL.setdefault(name, os.environ.get(name))
    os.environ[name] = value


def _set_by_apprun(env) -> set[str]:
    """The TLS variables that AppRun set, which the technician's environment did not have.

    AppRun sets SSL_CERT_FILE and REQUESTS_CA_BUNDLE so that DAToolkit's own HTTP clients trust
    the host's CA store rather than the bundled one. Passed on, they changed what the
    technician's programs trust: a Python tool in a DAToolkit terminal (a cloud CLI with its own
    certificates) was pointed at the system bundle instead, and behaved unlike the same tool in
    any other terminal. AppRun only ever sets a variable that was unset or empty, so restoring
    the environment it found means leaving those out.

    An AppRun that says which it set (APPRUN_SET, the names separated by spaces) is taken at
    its word. One that doesn't is read by what it would have done: SSL_CERT_FILE naming one of
    the host bundles it picks from is its work, and REQUESTS_CA_BUNDLE equal to that too. A
    technician who had set SSL_CERT_FILE to that very file loses nothing by its absence (it is
    the system's default), and one with a bundle of their own keeps both variables."""
    if APPRUN_SET in env:
        # said by AppRun itself, however it was started: from the AppImage, or from an extracted one,
        # where the runtime's own variables (APPDIR, APPIMAGE) aren't there to go by
        return set(env[APPRUN_SET].split()) & set(CA_VARS)
    if not (env.get("APPDIR") or env.get("APPIMAGE")):
        return set()
    found = set()
    if env.get("SSL_CERT_FILE") in APPRUN_CA_BUNDLES:
        found.add("SSL_CERT_FILE")
        if env.get("REQUESTS_CA_BUNDLE") == env["SSL_CERT_FILE"]:
            found.add("REQUESTS_CA_BUNDLE")
    return found


def host_env(**extra: str) -> dict[str, str]:
    dropped = set(APPIMAGE_VARS) | _set_by_apprun(os.environ)
    env = {k: v for k, v in os.environ.items() if k not in dropped}
    for name, value in ORIGINAL.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    env.update(extra)
    return env


def own_tls_env() -> dict[str, str]:
    """The TLS variables DAToolkit itself runs with, for a helper of its own that makes TLS
    connections with DAToolkit's Python (the WinRM console): host_env() takes AppRun's out, and
    without them the bundled CA list would be used in place of the host's."""
    return {name: os.environ[name] for name in CA_VARS if os.environ.get(name)}
