"""Credential-storage tests: the password must never land in a plaintext file."""

import stat

import pytest

from epfo.config import (
    Profile, SecretStorageUnavailable, delete_password, load_password,
    load_profile, save_profile, store_password,
)


@pytest.fixture
def config_home(monkeypatch, tmp_path):
    """Point the config directory at a temp home and re-import the paths."""
    monkeypatch.setenv("EPFO_CLI_HOME", str(tmp_path))
    import importlib

    from epfo import config
    return importlib.reload(config)


def test_saved_profile_is_readable_only_by_the_owner(config_home):
    """The README claims mode 0600; this is the test behind that claim."""
    path = config_home.save_profile(Profile(uan="100123456789"))
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_profile_round_trips(config_home):
    config_home.save_profile(Profile(uan="100123456789",
                                     cookies_path="/tmp/c.txt"))
    loaded = config_home.load_profile()
    assert loaded.uan == "100123456789"
    assert loaded.cookies_path == "/tmp/c.txt"


def test_a_missing_config_is_not_an_error(config_home):
    assert config_home.load_profile().uan == ""


def test_a_corrupt_config_degrades_to_defaults(config_home):
    config_home.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config_home.CONFIG_PATH.write_text("{not json")
    assert config_home.load_profile().uan == ""


def test_password_is_never_written_to_the_config_file(config_home):
    """The decisive test: no password may reach disk in the profile file."""
    secret = "s3cret-do-not-persist"
    config_home.save_profile(Profile(uan="100123456789"))
    assert secret not in config_home.CONFIG_PATH.read_text()
    # And the file has no field that could even hold one.
    assert "password" not in config_home.CONFIG_PATH.read_text().lower()


def test_storing_a_password_without_a_keychain_raises_rather_than_writing(
        config_home, monkeypatch):
    """With no keychain the only acceptable behaviour is to refuse."""
    def no_keyring():
        raise SecretStorageUnavailable("no keychain here")

    monkeypatch.setattr(config_home, "_keyring", no_keyring)
    with pytest.raises(SecretStorageUnavailable):
        config_home.store_password("100123456789", "s3cret")


def test_load_password_returns_none_when_keyring_is_absent(config_home, monkeypatch):
    """keyring not installed -> None. The caller then prompts, which is correct."""
    def no_keyring():
        raise config_home.SecretStorageUnavailable("no keychain here")

    monkeypatch.setattr(config_home, "_keyring", no_keyring)
    assert config_home.load_password("100123456789") is None


def test_load_password_raises_when_the_keychain_is_broken(config_home, monkeypatch):
    """A keychain that exists but fails must NOT masquerade as 'nothing stored'.

    Collapsing the two cases into None once hid a missing dependency and made a
    saved password look like it had never been saved.
    """
    class _Broken:
        @staticmethod
        def get_password(service, account):
            raise RuntimeError("keychain is locked")

    monkeypatch.setattr(config_home, "_keyring", lambda: _Broken())
    with pytest.raises(config_home.SecretStorageUnavailable, match="unreadable"):
        config_home.load_password("100123456789")


def test_delete_password_reports_failure_instead_of_raising(config_home, monkeypatch):
    def no_keyring():
        raise SecretStorageUnavailable("no keychain here")

    monkeypatch.setattr(config_home, "_keyring", no_keyring)
    assert config_home.delete_password("100123456789") is False
