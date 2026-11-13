"""Secret storage in the OS keyring (Secret Service on Linux): the providers' API keys, the hosts'
passwords, and a password written into a provider's address.

A secret is written only to a keyring that keeps it safe. keyring picks the best backend it can
find, and with no Secret Service running that may be one that writes to a plain file (keyrings.alt's
PlaintextKeyring, priority below 1): set_secret refuses then, with KeyringUnavailable, instead of
writing the secret there. The backend is looked for again first, as the Secret Service may have
started since (right after login it often isn't up yet). After DA Private Coder's creds.py (Nov 2026)."""

from __future__ import annotations

import keyring
import keyring.core
from keyring.errors import NoKeyringError, PasswordDeleteError

SERVICE = "datoolkit"


class KeyringUnavailable(Exception):
    """No keyring would keep the secret safe, or the keyring refused it (locked, its prompt
    dismissed): nothing was stored. Said to the technician as it is."""


def backend_error() -> str | None:
    """Return a human-readable problem if no usable keyring backend exists."""
    try:
        backend = keyring.get_keyring()
    except Exception as e:  # noqa: BLE001 - any backend failure is reportable
        return f"Keyring unavailable: {e}"
    if getattr(backend, "priority", 0) < 1:
        return (
            f"No secure keyring backend available ({type(backend).__name__}). "
            "Install and unlock a Secret Service provider such as gnome-keyring or KWallet."
        )
    return None


def _secure_backend():
    """The keyring backend, if it keeps secrets safe (priority 1 or more); else KeyringUnavailable."""
    backend = keyring.get_keyring()
    if getattr(backend, "priority", 0) < 1:
        keyring.core.init_backend()          # the Secret Service may have started since
        backend = keyring.get_keyring()
    if getattr(backend, "priority", 0) < 1:
        raise KeyringUnavailable(
            f"No secure keyring is available ({type(backend).__name__}), so nothing was stored. Install and unlock "
            "a Secret Service provider such as GNOME Keyring or KWallet.")
    return backend


def _user(kind: str, name: str) -> str:
    return f"{kind}:{name}"


def set_secret(kind: str, name: str, value: str) -> None:
    """Store a secret, only in a keyring that keeps it safe. Raises KeyringUnavailable."""
    backend = _secure_backend()
    try:
        backend.set_password(SERVICE, _user(kind, name), value)
    except Exception as e:  # noqa: BLE001 - locked, or its prompt dismissed: said as it is
        raise KeyringUnavailable(f"The keyring couldn't store it ({e or type(e).__name__}), so nothing was "
                                 "stored. Unlock it, then try again.") from e


def get_secret(kind: str, name: str) -> str | None:
    return keyring.get_password(SERVICE, _user(kind, name))


def delete_secret(kind: str, name: str) -> None:
    """Remove a secret; one that isn't there is no error, and neither is having no keyring at all
    (nothing is kept in none). Raises KeyringUnavailable when the keyring couldn't be asked: it is
    locked, its unlock prompt was dismissed, the Secret Service didn't answer. Until Nov 2026 only
    "no such password" was caught, and anything else went up as the keyring's own exception from
    whatever was deleting a provider or a host, half done."""
    user = _user(kind, name)
    try:
        try:
            keyring.delete_password(SERVICE, user)
        except PasswordDeleteError:
            # "no such password" from the Secret Service, and from KWallet; but KWallet says the
            # same when its unlock prompt was cancelled, so the secret is looked for: still
            # there, or not to be read, and it wasn't deleted
            if keyring.get_password(SERVICE, user) is not None:
                raise
    except NoKeyringError:
        return
    except Exception as e:  # noqa: BLE001 - locked, dismissed, no answer: said as it is
        raise KeyringUnavailable(f"The keyring couldn't remove it ({e or type(e).__name__}), so nothing was "
                                 "changed. Unlock it, then try again.") from e


def has_secret(kind: str, name: str) -> bool:
    return get_secret(kind, name) is not None
