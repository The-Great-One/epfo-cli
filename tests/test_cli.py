"""CLI tests: the commands must fail closed, never invent success."""

import json
from types import SimpleNamespace

import pytest

from epfo.cli import build_parser, main


def test_parser_exposes_every_documented_command():
    choices = build_parser()._subparsers._group_actions[0].choices
    for command in ("login", "discover", "passbook", "export", "config", "doctor"):
        assert command in choices


def test_main_requires_a_subcommand(capsys):
    with pytest.raises(SystemExit):
        main([])
    assert "usage" in capsys.readouterr().err.lower()


def _isolate_config(monkeypatch, tmp_path):
    """Point the config module at a temp dir.

    Setting EPFO_CLI_HOME alone is not enough: CONFIG_PATH is computed at import
    time, so without this a machine with a real ~/.epfo-cli/config.json leaks
    its UAN into the test.
    """
    from epfo import config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")


def test_passbook_without_a_uan_fails_closed(capsys, monkeypatch, tmp_path):
    """passbook logs in itself, so a missing UAN is the failure, not a cookie.

    This used to assert "no saved session": the command originally restored a
    cookie jar. Live testing showed EPFO rejects a reused session
    (``session-exception``), so the command now logs in and the old message is
    gone deliberately.
    """
    monkeypatch.setenv("EPFO_CLI_HOME", str(tmp_path))
    _isolate_config(monkeypatch, tmp_path)
    assert main(["passbook"]) == 2
    assert "no UAN" in capsys.readouterr().err


def test_login_without_a_uan_fails_closed(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("EPFO_CLI_HOME", str(tmp_path))
    _isolate_config(monkeypatch, tmp_path)
    assert main(["login"]) == 2
    assert "no UAN" in capsys.readouterr().err


class _StubSession:
    """A session whose page has no tables - the parser must say so."""

    def get(self, path):
        return "<html><body>no tables here</body></html>"


def _args(**kwargs):
    return SimpleNamespace(**kwargs)



def test_passbook_is_listed_as_a_command_that_logs_in():
    """The parser must expose passbook's own credentials flags, not a --path.

    ``--ocr`` was renamed: reading the captcha automatically is the default now,
    so the switch that remains is the one that turns it *off*.
    """
    parser = build_parser()
    sub = parser._subparsers._group_actions[0].choices["passbook"]
    flags = {o for a in sub._actions for o in a.option_strings}
    assert "--uan" in flags
    assert "--store-password" in flags
    assert "--password-stdin" in flags
    # --no-auto-captcha is the second option_string of a BooleanOptionalAction,
    # so every option must be collected, not just the first.
    assert "--auto-captcha" in flags
    assert "--no-auto-captcha" in flags


def test_captcha_is_read_automatically_by_default(monkeypatch, tmp_path):
    """The default must not prompt: that is what makes this drivable by an agent."""
    from epfo import cli, config
    from epfo.session import Captcha

    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "load_profile",
                        lambda: config.Profile(uan="100123456789"))
    monkeypatch.setattr(cli, "load_password", lambda uan: "pw")

    captcha = Captcha(image_base64="", token="t")
    monkeypatch.setattr(cli, "_session_from_profile", lambda p: object())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "auto_answer", lambda c, solver=None: "ABC123")
    monkeypatch.setattr("builtins.input",
                        lambda *a, **k: pytest.fail("must not prompt"))

    assert cli._answer_captcha(captcha, auto=True) == "ABC123"


def test_no_auto_captcha_asks_a_human(monkeypatch, tmp_path):
    """--no-auto-captcha must prompt, so a human read is always available."""
    from epfo import cli
    from epfo.session import Captcha

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "auto_answer",
                        lambda c, solver=None: pytest.fail("must not read"))
    monkeypatch.setattr("builtins.input", lambda *a, **k: " human9 ")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: None)

    captcha = Captcha(image_base64="", token="t")
    assert cli._answer_captcha(captcha, auto=False) == "human9"


def test_auto_captcha_falls_back_to_a_human_when_ocr_fails(monkeypatch, tmp_path):
    """OCR failing must degrade to a prompt, never to a wrong submitted answer."""
    from epfo import cli
    from epfo.captcha import CaptchaOCRError
    from epfo.session import Captcha

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "auto_answer",
                        lambda c, solver=None: (_ for _ in ()).throw(
                            CaptchaOCRError("nope")))
    monkeypatch.setattr("builtins.input", lambda *a, **k: "typed2")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: None)

    captcha = Captcha(image_base64="", token="t")
    assert cli._answer_captcha(captcha, auto=True) == "typed2"


def test_password_stdin_reads_a_piped_password(monkeypatch):
    """A piped password makes unattended runs possible without argv exposure."""
    import io

    from epfo import cli

    monkeypatch.setattr(cli.sys, "stdin",
                        type("S", (), {"isatty": lambda self: False,
                                       "readline": lambda self: "piped-secret\n"})())
    assert cli._password_from_stdin() == "piped-secret"


def test_password_stdin_refuses_a_terminal(monkeypatch):
    """On a TTY it must raise rather than silently eat the next line."""
    from epfo import cli
    from epfo.session import EPFOError

    monkeypatch.setattr(cli.sys, "stdin",
                        type("S", (), {"isatty": lambda self: True})())
    with pytest.raises(EPFOError, match="stdin is a terminal"):
        cli._password_from_stdin()


def test_authenticated_read_reauthenticates_once_on_a_lapsed_session(monkeypatch):
    """A mid-read session lapse must trigger exactly one re-login, then retry."""
    from epfo import cli
    from epfo.session import SessionExpired

    logins = []

    def fake_login(session, uan, password, *, auto_captcha, attempts):
        logins.append(uan)
        return f"token{len(logins)}", 0

    monkeypatch.setattr(cli, "_login", fake_login)

    calls = []

    def read(token):
        calls.append(token)
        if len(calls) == 1:
            raise SessionExpired("session-exception")
        return f"ok on {token}"

    result, code = cli._authenticated_read(
        object(), "100123456789", "pw", auto_captcha=True, attempts=3,
        read=read)
    assert code == 0
    assert result == "ok on token2"
    assert len(logins) == 2 and len(calls) == 2


def test_authenticated_read_gives_up_after_a_second_lapse(monkeypatch):
    """It must not loop: a second lapse is reported as a failure."""
    from epfo import cli
    from epfo.session import SessionExpired

    monkeypatch.setattr(cli, "_login",
                        lambda *a, **k: ("token", 0))

    def read(token):
        raise SessionExpired("session-exception")

    result, code = cli._authenticated_read(
        object(), "100123456789", "pw", auto_captcha=True, attempts=3,
        read=read)
    assert result is None and code == 3


def test_discover_actually_fetches_the_static_sources(monkeypatch, tmp_path, capsys):
    """Regression: the static-fetch path must run, not raise NameError.

    A refactor once dropped the `Request` import while the fetch loop still used
    it. The scan_* unit tests did not touch this path, so only executing
    cmd_discover could catch it.
    """
    monkeypatch.setenv("EPFO_CLI_HOME", str(tmp_path))
    from epfo import cli

    fetched = []

    class _StubResponse:
        def __init__(self, body): self._body = body.encode()
        def read(self): return self._body
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _StubOpener:
        def open(self, request, timeout=None):
            fetched.append(request.full_url)
            return _StubResponse(
                "<script>\n"
                '  url: "/MemberPassBook/passbook/api/ajax/final/checkLogin",\n'
                '  data["username"] = u; data["password"] = p;\n'
                '  data["token"] = t; data["answer"] = a;\n'
                "</script>")

    monkeypatch.setattr(cli, "plain_opener", lambda: _StubOpener())
    code = cli.cmd_discover(_args(endpoint=None, target=None, origins=None,
                                  out=str(tmp_path / "d.json")))
    assert code == 0
    assert len(fetched) == 3, f"expected 3 static fetches, got {fetched}"
    out = capsys.readouterr().out
    assert "checkLogin" in out
    assert "params: answer, password, token, username" in out


def test_passbook_never_prompts_when_a_password_is_stored(monkeypatch, tmp_path):
    """With a working keychain the CLI must not ask for the password at all."""
    from epfo import cli, config

    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "load_profile",
                        lambda: config.Profile(uan="100123456789"))
    monkeypatch.setattr(cli, "load_password", lambda uan: "stored-secret")
    monkeypatch.setattr(cli, "getpass",
                        lambda *a, **k: pytest.fail("must not prompt"))
    monkeypatch.setattr(cli, "store_password", lambda *a, **k: None)
    # Intercept before the network so only the credential path is exercised.
    monkeypatch.setattr(cli, "_login", lambda *a, **k: (None, 7))

    assert cli.main(["passbook", "--store-password"]) == 7


def test_a_broken_keychain_is_reported_not_hidden(monkeypatch, tmp_path, capsys):
    """A keychain read failure must print a note, then fall back to prompting.

    Silently returning None here is what made a saved password look unsaved.
    """
    from epfo import cli, config

    _isolate_config(monkeypatch, tmp_path)

    def broken(uan):
        raise config.SecretStorageUnavailable("keychain is unreadable")

    monkeypatch.setattr(cli, "load_profile",
                        lambda: config.Profile(uan="100123456789"))
    monkeypatch.setattr(cli, "load_password", broken)
    monkeypatch.setattr(cli, "getpass", lambda *a, **k: "typed-secret")
    monkeypatch.setattr(cli, "_login", lambda *a, **k: (None, 7))

    assert cli.main(["passbook"]) == 7
    assert "keychain is unreadable" in capsys.readouterr().err


def test_login_alternates_captcha_backends_across_attempts(monkeypatch):
    """Consecutive attempts must use different readers.

    ddddocr and tesseract disagreed on 4 of 8 live captchas (always O/9 or
    T/7), so re-reading with the same backend after a refusal is a repeat of the
    same bet. Alternating makes each attempt a genuinely new one.
    """
    from epfo import cli
    from epfo.session import Captcha

    used = []

    class _Session:
        login_token = "t"

        def start(self):
            return Captcha(image_base64="", token="t")

        def refresh_captcha(self):
            return Captcha(image_base64="", token="t")

        def login(self, uan, password, answer):
            from epfo.session import LoginResult

            return LoginResult(ok=False, message="Invalid Captcha")

    def record(captcha, *, auto, solver=None):
        used.append(solver)
        return "ABC123"

    monkeypatch.chdir(  # keep captcha.jpg out of the repo
        __import__("tempfile").mkdtemp())
    monkeypatch.setattr(cli, "_answer_captcha", record)
    cli._login(_Session(), "u", "p", auto_captcha=True, attempts=3)
    assert used == ["ddddocr", "tesseract", "ddddocr"]


def test_no_auto_captcha_never_names_a_solver(monkeypatch):
    """With automatic reading off, no backend should be chosen at all."""
    from epfo import cli
    from epfo.session import Captcha

    used = []

    class _Session:
        login_token = "t"

        def start(self):
            return Captcha(image_base64="", token="t")

        def login(self, uan, password, answer):
            from epfo.session import LoginResult

            return LoginResult(ok=True, message="", raw={"object": "tok"})

    def record(captcha, *, auto, solver=None):
        used.append((auto, solver))
        return "HUMAN1"

    monkeypatch.chdir(__import__("tempfile").mkdtemp())
    monkeypatch.setattr(cli, "_answer_captcha", record)
    cli._login(_Session(), "u", "p", auto_captcha=False, attempts=2)
    assert used == [(False, None)]


def test_passbook_authenticates_exactly_once(monkeypatch, tmp_path):
    """Regression: passbook once logged in twice per run.

    cmd_passbook called _login directly *and* _authenticated_read, which logs in
    itself - so every read cost two captchas and two login POSTs. The symptom
    was a duplicated captcha line in the output.
    """
    from epfo import cli, config
    from epfo.passbook import PassbookResult

    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "load_profile",
                        lambda: config.Profile(uan="100123456789"))
    monkeypatch.setattr(cli, "load_password", lambda uan: "pw")
    monkeypatch.setattr(cli, "_session_from_profile", lambda p: object())
    monkeypatch.setattr(cli, "_login",
                        lambda *a, **k: pytest.fail("must not log in directly"))

    reads = []

    def fake_read(session, uan, password, *, auto_captcha, attempts, read):
        reads.append((uan, read))
        return PassbookResult(), 0

    monkeypatch.setattr(cli, "_authenticated_read", fake_read)
    assert cli.main(["passbook"]) == 0
    assert len(reads) == 1, "the authenticated read must run exactly once"
