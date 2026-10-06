"""Session tests, driven against the real (trimmed) login page."""

import json

import pytest

from epfo.crypto import encode_password
from epfo.session import (
    BASE, CaptchaRequired, EPFOSession, EPFOError, SessionExpired, TokenExpired,
    _CAPTCHA_RE, _TOKEN_RE,
)


def test_start_extracts_the_login_token_and_captcha(login_html):
    session = EPFOSession()
    session._request = lambda *a, **k: login_html
    captcha = session.start()
    assert session.login_token
    assert captcha.image_base64.startswith("/9j/")  # JPEG magic in base64
    assert captcha.token == session.login_token


def test_start_fails_loudly_when_the_page_layout_changes():
    session = EPFOSession()
    session._request = lambda *a, **k: "<html>nothing useful here</html>"
    with pytest.raises(EPFOError, match="layout may have changed"):
        session.start()


def test_login_sends_the_encoded_password_never_the_plaintext(login_html):
    sent = {}

    def fake_request(path, data=None, method=None):
        sent["path"] = path
        sent["data"] = data
        return json.dumps({"success": True, "object": "session-token-xyz"})

    session = EPFOSession()
    session._request = fake_request
    session.login_token = "tok"
    result = session.login("100123456789", "Sup3rSecret!", "Ab3dEf")

    assert result.ok
    assert sent["data"]["password"] == encode_password("Sup3rSecret!")
    assert sent["data"]["password"] != "Sup3rSecret!"
    assert "Sup3rSecret!" not in json.dumps(sent["data"])
    assert sent["data"]["username"] == "100123456789"
    assert sent["data"]["token"] == "tok"
    assert sent["data"]["answer"] == "Ab3dEf"
    assert session.session_token == "session-token-xyz"


def test_login_reports_refusal_with_the_portal_message():
    session = EPFOSession()
    session._request = lambda *a, **k: json.dumps(
        {"success": False, "message": "Invalid Captcha"})
    session.login_token = "tok"
    result = session.login("100123456789", "pw", "wrong")
    assert not result.ok
    assert result.message == "Invalid Captcha"
    assert session.session_token is None


def test_login_flags_pending_otp_instead_of_claiming_success():
    session = EPFOSession()
    session._request = lambda *a, **k: json.dumps(
        {"success": True, "otp": True, "object": "<div id='otp'>enter otp</div>"})
    session.login_token = "tok"
    result = session.login("100123456789", "pw", "Ab3dEf")
    assert result.otp_required
    assert "otp" in result.otp_html
    # An OTP challenge is NOT a usable session: no token may be adopted.
    assert session.session_token is None


def test_login_refuses_non_json_rather_than_guessing():
    session = EPFOSession()
    session._request = lambda *a, **k: "<html>gateway error</html>"
    session.login_token = "tok"
    with pytest.raises(EPFOError, match="non-JSON"):
        session.login("100123456789", "pw", "Ab3dEf")


def test_login_requires_a_token_first():
    session = EPFOSession()
    session._request = lambda *a, **k: "{}"
    with pytest.raises(EPFOError, match="call start"):
        session.login("100123456789", "pw", "Ab3dEf")


class _Response:
    def __init__(self, html): self._html = html
    def read(self): return (self._html or "").encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _redirect_error(url, location, code=302):
    """A genuine HTTPError carrying a Location header, as urllib raises."""
    from email.message import Message
    from io import BytesIO
    from urllib.error import HTTPError

    message = Message()
    message["Location"] = location
    return HTTPError(url, code, "Found", message, BytesIO(b""))


class _StubOpener:
    """Stands in for the urllib opener so get() can be tested directly."""

    def __init__(self, html=None, location=None, code=302):
        self._html, self._location, self._code = html, location, code

    def open(self, request, timeout=None):
        if self._location is not None:
            raise _redirect_error(request.full_url, self._location, self._code)
        return _Response(self._html)


def test_get_reports_a_session_exception_precisely():
    """The portal signals a dead session by redirecting, not by status code."""
    location = ("http://passbook.epfindia.gov.in/MemberPassBook/login"
                "?error=session-exception")
    session = EPFOSession()
    session._opener = _StubOpener(location=location)
    with pytest.raises(SessionExpired, match="session-exception"):
        session.get("/passbook")


def test_get_reports_a_token_exception_precisely():
    location = "/MemberPassBook/login?error=token-exception"
    session = EPFOSession()
    session._opener = _StubOpener(location=location)
    with pytest.raises(TokenExpired, match="token-exception"):
        session.get("/passbook")


def test_get_does_not_hang_on_a_plain_http_redirect():
    """A non-login redirect must raise, never be followed into http://."""
    session = EPFOSession()
    session._opener = _StubOpener(location="http://passbook.epfindia.gov.in/x")
    with pytest.raises(EPFOError, match="redirected to"):
        session.get("/passbook")


def test_get_raises_token_expired_on_the_redirect_body():
    session = EPFOSession()
    session._opener = _StubOpener(html="<html>login?error=token-exception</html>")
    with pytest.raises(TokenExpired):
        session.get("/MemberPassBook/passbook")


def test_get_returns_html_when_authenticated():
    session = EPFOSession()
    session._opener = _StubOpener(html="<html><table>passbook</table></html>")
    assert "passbook" in session.get("/MemberPassBook/passbook")


def test_redirects_are_not_followed():
    """Following the http:// redirect is what made passbook hang forever."""
    from epfo.session import NoRedirect
    assert NoRedirect().redirect_request(None, None, 302, "", {}, "http://x/") is None


def test_base_url_is_the_real_portal():
    assert BASE == "https://passbook.epfindia.gov.in/MemberPassBook"


# -- regression: a URL copied from the address bar 404'd --------------------

@pytest.mark.parametrize("path", [
    "/passbook",
    "/MemberPassBook/passbook",
    "https://passbook.epfindia.gov.in/MemberPassBook/passbook",
])
def test_absolute_never_duplicates_the_memberpassbook_prefix(path):
    expected = "https://passbook.epfindia.gov.in/MemberPassBook/passbook"
    assert EPFOSession().absolute(path) == expected


def test_absolute_collapses_only_the_prefix_not_a_real_segment():
    resolved = EPFOSession().absolute("/passbook/ajax/get-new-captcha")
    assert resolved.endswith("/MemberPassBook/passbook/ajax/get-new-captcha")
    assert resolved.count("/MemberPassBook") == 1


# -- regression: python.org framework builds have no CA trust store ---------

def test_ssl_context_has_a_trust_store_and_verifies():
    import ssl

    from epfo.session import ssl_context

    context = ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    # A context with no CA bundle has an empty get_ca_certs(); that is the exact
    # condition that made the default python3 on this machine fail every HTTPS
    # request to the portal.
    assert context.get_ca_certs(), "no CA certificates loaded"


def test_no_code_path_disables_certificate_verification():
    """No module may actually disable verification.

    Matches real usage (``ssl.CERT_NONE``, ``verify_mode = CERT_NONE``,
    ``check_hostname = False``) rather than the bare token, because the session
    module's docstring mentions CERT_NONE by name while explaining why it is
    never used.
    """
    import re
    from pathlib import Path

    import epfo

    pattern = re.compile(
        r"\bssl\s*\.\s*CERT_NONE\b"
        r"|verify_mode\s*=\s*(?!ssl\.CERT_REQUIRED)[^\n]*CERT_NONE"
        r"|check_hostname\s*=\s*False")
    offenders = [p.name for p in Path(epfo.__file__).parent.glob("*.py")
                 if pattern.search(p.read_text())]
    assert not offenders, f"certificate verification disabled in {offenders}"


def test_plain_opener_returns_an_opener():
    from epfo.session import plain_opener

    assert hasattr(plain_opener(), "open")


# -- regression: the parallel pre-warmed connection pool ---------------------


class _FakeConn:
    """Stands in for an http.client.HTTPSConnection in the pool tests."""

    def __init__(self, request_error=None, will_close=True):
        self.request_error = request_error
        self.will_close = will_close
        self.requests = []

    def connect(self):
        pass

    def request(self, method, path, body=None, headers=None):
        if self.request_error is not None:
            raise self.request_error
        self.requests.append((method, path))

    def getresponse(self):
        return _FakeResp(self.will_close)

    def close(self):
        pass


class _FakeResp:
    def __init__(self, will_close):
        self.status = 200
        self.headers = _FakeHeaders()
        self.will_close = will_close

    def read(self):
        return b"<html>ok</html>"


class _FakeHeaders(dict):
    """Mimics http.client.HTTPMessage closely enough for _absorb_cookies."""

    def get_all(self, name, default=None):
        return [v for k, v in self.items() if k.lower() == name.lower()] or default


def _pooled_session(monkeypatch, factory, size):
    from urllib.request import Request

    from epfo import session as mod

    monkeypatch.setattr(mod._ConnOpener, "POOL_SIZE", size)
    monkeypatch.setattr(mod._ConnOpener, "_new_conn", lambda self: factory())
    return mod.EPFOSession(), Request


def test_pool_prewarms_all_sockets_and_never_reuses_one(monkeypatch):
    """N sockets open up front, one request each.

    Reusing a socket would fail against the real server, which closes them -
    so a pooled connection must serve exactly one request.
    """
    created = []

    def factory():
        conn = _FakeConn()
        created.append(conn)
        return conn

    sess, Request = _pooled_session(monkeypatch, factory, size=3)
    for _ in range(3):
        sess._opener.open(Request("https://passbook.epfindia.gov.in/MemberPassBook/x"))
    assert len(created) == 3
    assert all(len(c.requests) == 1 for c in created)


def test_pool_refills_when_it_runs_dry(monkeypatch):
    created = []

    def factory():
        conn = _FakeConn()
        created.append(conn)
        return conn

    sess, Request = _pooled_session(monkeypatch, factory, size=2)
    for _ in range(5):  # more requests than the pool held
        sess._opener.open(Request("https://passbook.epfindia.gov.in/MemberPassBook/x"))
    assert len(created) == 6  # refills in POOL_SIZE batches: 2 + 2 + 2


def test_pool_recovers_from_a_socket_the_server_already_killed(monkeypatch):
    """A warmed socket can die before use; the request must still succeed."""
    import http.client

    made = {"n": 0}

    def factory():
        made["n"] += 1
        if made["n"] == 1:
            return _FakeConn(request_error=http.client.CannotSendRequest("dead"))
        return _FakeConn()

    sess, Request = _pooled_session(monkeypatch, factory, size=1)
    resp = sess._opener.open(Request("https://passbook.epfindia.gov.in/MemberPassBook/x"))
    assert resp.read() == b"<html>ok</html>"


def test_pool_size_is_configurable_by_env(monkeypatch):
    import importlib

    from epfo import session as mod

    monkeypatch.setenv("EPFO_POOL_SIZE", "7")
    importlib.reload(mod)
    try:
        assert mod._ConnOpener.POOL_SIZE == 7
    finally:
        monkeypatch.delenv("EPFO_POOL_SIZE", raising=False)
        importlib.reload(mod)
