"""Credential and session storage.

A UAN and EPFO password are sensitive. This module refuses to write the
password to a plaintext file: it uses the OS keychain via ``keyring`` when that
is available, and otherwise stores nothing and asks for the password each run.

Only the UAN (an identifier, not a secret) is cached in a config file, and that
file is created with mode 0600.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, asdict
from pathlib import Path

SERVICE_NAME = "epfo-cli"
CONFIG_DIR = Path(os.environ.get("EPFO_CLI_HOME", Path.home() / ".epfo-cli"))
CONFIG_PATH = CONFIG_DIR / "config.json"


class SecretStorageUnavailable(RuntimeError):
    """Raised when no secure store exists and a secret was requested."""


def _keyring():
    try:
        import keyring
    except ImportError as exc:
        raise SecretStorageUnavailable(
            "the keyring package is not installed, so no OS keychain is "
            "available; install it with `pip install keyring`, or the password "
            "will be prompted for on each run") from exc
    return keyring


def keyring_available() -> bool:
    try:
        backend = _keyring().get_keyring()
    except Exception:
        return False
    return backend is not None and "fail" not in type(backend).__name__.lower()


@dataclass
class Profile:
    """Non-secret settings for one EPFO account."""

    uan: str = ""
    cookies_path: str = ""
    last_endpoint: str = ""
    last_target_id: str = ""

    @property
    def display(self) -> str:
        return f"UAN {self.uan}" if self.uan else "no account configured"


def load_profile() -> Profile:
    if not CONFIG_PATH.exists():
        return Profile()
    try:
        return Profile(**json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError):
        return Profile()


def save_profile(profile: Profile) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(asdict(profile), indent=2),
                           encoding="utf-8")
    CONFIG_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600: may hold identifiers
    return CONFIG_PATH


def store_password(uan: str, password: str) -> None:
    """Save the password in the OS keychain. Raises if none is available."""
    _keyring().set_password(SERVICE_NAME, uan, password)


def load_password(uan: str) -> str | None:
    """Fetch the stored password for a UAN.

    Returns ``None`` only when there is genuinely nothing stored (or the keyring
    feature is not installed). A keychain that exists but cannot be read raises
    ``SecretStorageUnavailable`` - collapsing the two cases into a single
    ``None`` once hid a missing dependency and silently downgraded the CLI to
    prompting as if no password had ever been saved.
    """
    try:
        keyring_module = _keyring()
    except SecretStorageUnavailable:
        return None
    try:
        return keyring_module.get_password(SERVICE_NAME, uan)
    except Exception as exc:
        raise SecretStorageUnavailable(
            f"the OS keychain is unavailable or unreadable ({type(exc).__name__}: "
            f"{exc}); the stored password cannot be retrieved") from exc


def delete_password(uan: str) -> bool:
    try:
        _keyring().delete_password(SERVICE_NAME, uan)
        return True
    except Exception:
        return False
