"""HTTP session for the EPFO member passbook portal.

The portal is old and fragile, and several of its behaviours are load-bearing
for any client that wants to work at all:

* ``JSESSIONID`` is issued on the login page. The login POST is only accepted
  if it arrives with that same session cookie.
* The login form carries a server-generated ``login-token`` hidden field. It is
  rotated on every failed attempt, so a failed login must re-fetch it.
* Every path after login is guarded: requesting ``home2`` or ``passbook``
  without a valid token redirects to ``login?error=token-exception``. There is
  no unauthenticated way to enumerate the post-login API, which is why
  ``epfo.discover`` exists.
* The captcha is a JPEG returned as base64 in ``html`` with the matching token
  in ``object`` from ``/passbook/ajax/get-new-captcha``.
"""

from __future__ import annotations

import base64
import email.message
import http.client
import os as _os
import threading
import io
import json
import re
import ssl
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit
from urllib.error import HTTPError
from urllib.request import (
    HTTPRedirectHandler, HTTPSHandler, Request, build_opener,
)

from .crypto import encode_password

BASE = "https://passbook.epfindia.gov.in/MemberPassBook"
LOGIN_PATH = "/login"
CAPTCHA_PATH = "/passbook/ajax/get-new-captcha"
CHECK_LOGIN_PATH = "/passbook/api/ajax/final/checkLogin"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def ssl_context() -> ssl.SSLContext:
    """A TLS context that verifies certificates on any interpreter.

    This exists because of a real failure, not a hypothetical one: some
    python.org framework builds (including the default ``python3`` on this
    machine) ship with no configured trust store, so every HTTPS request raises
    ``CERTIFICATE_VERIFY_FAILED``. ``certifi`` carries Mozilla's CA bundle and is
    used when it is importable.

    Verification is never disabled — ``CERT_NONE`` appears nowhere in this
    project. If no trust store can be found, requests fail loudly, which is the
    correct outcome for a client that will carry an EPFO password.
    """
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


class NoRedirect(HTTPRedirectHandler):
    """Refuse to follow redirects, and surface them as data.

    Two real portal behaviours make automatic redirect-following actively
    harmful here:

    * On an expired session the portal answers ``302`` to
      ``login?error=session-exception``. Following it silently turns a session
      failure into a login page that parses as "no data", which is exactly the
      kind of quiet wrong answer this client exists to avoid.
    * The redirect target is ``http://`` - plain HTTP. Following it hangs
      indefinitely, because the plain-HTTP endpoint does not respond the same
      way. That hang is what made ``passbook`` appear to freeze.

    Returning the response un-followed lets the caller read the ``Location``
    header and report precisely what happened.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def plain_opener():
    """An opener with the working TLS context but no cookies.

    Used by ``doctor`` and ``discover`` for anonymous fetches, so they do not
    depend on the caller's interpreter having a usable CA bundle.
    """
    return build_opener(HTTPSHandler(context=ssl_context()))


class _ConnOpener:
    """A transport that pre-warms a pool of TCP connections, in parallel.

    The passbook host charges a large, fixed **per-TCP-connection** penalty -
    measured at 45-75 s to establish a socket in Oct 2026 - while any request on
    an already-open socket answers in ~0.04 s. Worse, the server honours
    keep-alive for its AJAX endpoints but sends ``Connection: close`` on the
    login and ``passbook`` page GETs, so a single command needs *several* fresh
    connections and pays the stall on each.

    Two things make it fast without changing what is sent:

    * **Warm in parallel.** ``connect()`` stalls are independent, so opening N
      sockets concurrently costs one stall of wall-clock time, not N. The pool
      is filled up front, before the first real request.
    * **One request per connection.** Because a connection dies after the
      request the server closes on, each pooled socket is used for exactly one
      request and then discarded; the pool is refilled lazily (again in
      parallel) when it runs dry.

    ``open()`` is the only method the callers use, and it returns an object with
    ``.read()`` / ``.headers`` / ``.geturl()`` / ``.status`` that also works as a
    context manager - the parts of ``HTTPResponse`` actually relied upon.
    """

    POOL_SIZE = int(_os.environ.get("EPFO_POOL_SIZE", "30"))

    def __init__(self, jar: CookieJar, context: ssl.SSLContext):
        self._jar = jar
        self._ctx = context
        self._pool: list[http.client.HTTPSConnection] = []
        self._host, self._port = _host_port(BASE)

    # -- pool management ---------------------------------------------------

    def _new_conn(self) -> http.client.HTTPSConnection:
        return http.client.HTTPSConnection(self._host, self._port, timeout=300,
                                           context=self._ctx)

    def _warm(self) -> http.client.HTTPSConnection | None:
        """Fill the pool with ``POOL_SIZE`` sockets, connecting concurrently."""
        made: list[http.client.HTTPSConnection] = [None] * self.POOL_SIZE  # type: ignore

        def worker(i: int) -> None:
            conn = self._new_conn()
            try:
                conn.connect()
                made[i] = conn
            except Exception:
                pass

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(self.POOL_SIZE)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(360)
        self._pool = [c for c in made if c is not None]

    def _take(self) -> http.client.HTTPSConnection | None:
        if not self._pool:
            self._warm()
        return self._pool.pop() if self._pool else None

    def close(self) -> None:
        for conn in self._pool:
            try:
                conn.close()
            except Exception:
                pass
        self._pool = []

    # -- request path ------------------------------------------------------

    def _once(self, url: str, method: str, headers: dict,
              body: bytes | None) -> tuple[int, Any, bytes]:
        conn = self._take()
        if conn is None:
            raise HTTPError(url, 0, "could not establish any connection to %s"
                            % self._host, None, None)
        path = _path_of(url)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        except (http.client.HTTPException, OSError) as exc:
            # This socket is spent (a pool entry can be killed by the server
            # between warm-up and use). Take another and try the request once
            # on a genuinely fresh connection.
            try:
                conn.close()
            except Exception:
                pass
            replacement = self._take()
            if replacement is None:
                raise HTTPError(url, 0, "connection dropped: %s" % exc,
                                None, None) from exc
            try:
                replacement.request(method, path, body=body, headers=headers)
                resp = replacement.getresponse()
                raw = resp.read()
            except (http.client.HTTPException, OSError) as exc2:
                raise HTTPError(url, 0, "connection dropped: %s" % exc2,
                                None, None) from exc2
            finally:
                try:
                    replacement.close()
                except Exception:
                    pass
            return resp.status, resp.headers, raw
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return resp.status, resp.headers, raw

    def open(self, request: Request, timeout: float | None = None):
        url = request.full_url
        method = request.get_method()
        headers = {"User-Agent": USER_AGENT, "Connection": "keep-alive"}
        for key, value in request.header_items():
            headers[key] = value
        body = request.data
        cookie_header = self._cookie_header(url)
        if cookie_header:
            headers["Cookie"] = cookie_header
        status, resp_headers, raw = self._once(url, method, headers, body)
        self._absorb_cookies(url, resp_headers)
        return _PooledResponse(url, status, resp_headers, raw)

    # -- cookie plumbing ---------------------------------------------------

    def _cookie_header(self, url: str) -> str:
        parts = urlsplit(url)
        return "; ".join(
            "%s=%s" % (c.name, c.value)
            for c in self._jar
            if _cookie_matches(c, parts.hostname or "", parts.path or "/")
        )

    def _absorb_cookies(self, url: str, headers) -> None:
        raw = headers.get_all("Set-Cookie") or []
        if not raw:
            return
        parts = urlsplit(url)
        msg = email.message.Message()
        for chunk in raw:
            msg["Set-Cookie"] = chunk
        self._jar.extract_cookies(_HeadersResponse(msg, url), _FakeRequest(parts))


class _HeadersResponse:
    """The ``response`` half of ``CookieJar.extract_cookies``.

    ``extract_cookies`` reads ``.info()`` for the Set-Cookie headers and
    ``.geturl()`` for the default cookie path.
    """

    def __init__(self, headers: email.message.Message, url: str):
        self._headers = headers
        self._url = url

    def info(self) -> email.message.Message:
        return self._headers

    def geturl(self) -> str:
        return self._url


class _FakeRequest:
    """The ``request`` half of ``CookieJar.extract_cookies``."""

    def __init__(self, parts):
        self.host = parts.netloc
        self.type = parts.scheme
        self.unverifiable = False
        self._parts = parts

    def get_full_url(self) -> str:
        return "%s://%s%s" % (self._parts.scheme, self._parts.netloc,
                              self._parts.path)

    def has_header(self, name: str) -> bool:
        return False


class _PooledResponse(io.BytesIO):
    """The minimal response surface the callers use, wrapping pooled bytes."""

    def __init__(self, url: str, status: int, headers, body: bytes):
        super().__init__(body)
        self._url = url
        self.status = status
        self.code = status
        self.headers = headers
        self.reason = getattr(headers, "reason", "")

    def geturl(self) -> str:
        return self._url

    def getcode(self) -> int:
        return self.status

    def info(self):
        return self.headers


def _host_port(url: str) -> tuple[str, int]:
    """Split a URL into (host, port), defaulting the port by scheme."""
    parts = urlsplit(url)
    return parts.hostname or "", parts.port or (
        443 if parts.scheme == "https" else 80)


def _path_of(url: str) -> str:
    """The request path (with query) for an absolute URL."""
    parts = urlsplit(url)
    return parts.path + (("?" + parts.query) if parts.query else "")


def _cookie_matches(cookie, host: str, path: str) -> bool:
    """Standard RFC 6265 domain/path matching, enough for this host."""
    domain = (cookie.domain or "").lstrip(".")
    if domain and not (host == domain or host.endswith("." + domain)):
        return False
    return path.startswith(cookie.path or "/")


class EPFOError(RuntimeError):
    """Any failure raised by this client, with portal context attached."""


class CaptchaRequired(EPFOError):
    """Login was refused because the captcha answer was absent or wrong."""


class TokenExpired(EPFOError):
    """A post-login request was bounced to the token-exception login page."""


class SessionExpired(EPFOError):
    """The server no longer recognises the session (session-exception).

    Distinct from an expired login token: this means the server-side session is
    gone and only a fresh login helps.
    """


_TOKEN_RE = re.compile(r'name="login-token"\s+value="([^"]+)"')
_CAPTCHA_RE = re.compile(r'id="captcha_id"[^>]*src="data:image/jpg;base64,([^"]+)"')


@dataclass
class Captcha:
    """One captcha challenge: the image plus the token it is bound to."""

    image_base64: str
    token: str

    def to_bytes(self) -> bytes:
        """The captcha image as raw JPEG bytes.

        Needed because ddddocr takes the image in memory, while the tesseract
        path needs it on disk. Decoding in one place keeps both backends honest
        about reading the same bytes.
        """
        return base64.b64decode(self.image_base64)

    def write_png(self, path: Path) -> Path:
        """Decode the base64 JPEG to disk so a human or OCR can read it."""
        path = Path(path)
        path.write_bytes(self.to_bytes())
        return path


@dataclass
class LoginResult:
    """Raw outcome of a login attempt."""

    ok: bool
    message: str = ""
    token: str | None = None
    otp_required: bool = False
    otp_html: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class EPFOSession:
    """Cookie-bearing HTTP client for the EPFO member passbook portal."""

    def __init__(self, cookies_path: Path | None = None) -> None:
        self.cookies_path = Path(cookies_path) if cookies_path else None
        self._jar = CookieJar()
        self._opener = _ConnOpener(self._jar, ssl_context())
        self.login_token: str | None = None
        self.session_token: str | None = None

    # -- plumbing ---------------------------------------------------------

    def absolute(self, path: str) -> str:
        """Resolve a path against BASE, collapsing a duplicated path prefix.

        ``BASE`` already ends in ``/MemberPassBook``. A caller who copies a URL
        out of the address bar therefore passes ``/MemberPassBook/passbook``,
        and a naive concatenation asks for
        ``/MemberPassBook/MemberPassBook/passbook`` - an unhelpful 404. The
        duplicate is collapsed rather than rejected.
        """
        if path.startswith("http"):
            return path
        prefix = "/MemberPassBook/"
        if path.startswith(prefix):
            path = "/" + path[len(prefix):]
        return BASE + path

    def _request(self, path: str, data: dict | None = None,
                 method: str | None = None) -> str:
        url = self.absolute(path)
        body = None
        headers = {}
        if data is not None:
            body = urlencode(data).encode("utf-8")
            headers["Content-Type"] = (
                "application/x-www-form-urlencoded; charset=UTF-8")
            headers["X-Requested-With"] = "XMLHttpRequest"
        request = Request(url, data=body, headers=headers,
                          method=method or ("POST" if data is not None else "GET"))
        with self._opener.open(request, timeout=60) as response:
            if getattr(response, "status", 200) in (301, 302, 303, 307, 308):
                location = response.headers.get("Location", "")
                raise self._session_error(path, location)
            return response.read().decode("utf-8", errors="replace")

    # -- login ------------------------------------------------------------

    def start(self) -> Captcha:
        """Load the login page and return its embedded captcha challenge."""
        html = self._request(LOGIN_PATH)
        token_match = _TOKEN_RE.search(html)
        captcha_match = _CAPTCHA_RE.search(html)
        if not token_match or not captcha_match:
            raise EPFOError(
                "login page did not contain the expected login-token/captcha "
                "pair; the portal layout may have changed")
        self.login_token = token_match.group(1)
        return Captcha(image_base64=captcha_match.group(1),
                       token=self.login_token)

    def refresh_captcha(self) -> Captcha:
        """Fetch a fresh captcha. Required after every failed attempt."""
        raw = self._request(CAPTCHA_PATH, data={})
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EPFOError(f"captcha endpoint returned non-JSON: {raw[:200]!r}") from exc
        if not payload.get("success"):
            raise EPFOError(f"captcha endpoint refused the request: {payload}")
        self.login_token = payload.get("object")
        return Captcha(image_base64=payload.get("html", ""),
                       token=self.login_token or "")

    def login(self, uan: str, password: str, captcha_answer: str) -> LoginResult:
        """Submit credentials.

        The wire `password` value is the encoded form, never the plaintext.
        """
        if not self.login_token:
            raise EPFOError("call start() before login() to obtain a login token")
        raw = self._request(CHECK_LOGIN_PATH, data={
            "username": uan,
            "password": encode_password(password),
            "token": self.login_token,
            "answer": captcha_answer,
        })
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EPFOError(f"login endpoint returned non-JSON: {raw[:200]!r}") from exc

        result = LoginResult(
            ok=bool(payload.get("success")),
            message=payload.get("message", "") or "",
            otp_required=bool(payload.get("otp") is True),
            otp_html=payload.get("object", "") if payload.get("otp") is True else "",
            raw=payload,
        )
        if result.ok:
            if not result.otp_required:
                # page_Redirect() sends the browser to home2?token=<object>
                self.session_token = payload.get("object")
        return result

    # -- authenticated access ---------------------------------------------

    def get(self, path: str) -> str:
        """Fetch a post-login path, failing loudly on session/token expiry.

        The portal signals an unusable session by redirecting to
        ``login?error=session-exception`` (or ``token-exception``). Both are
        detected from the ``Location`` header, which is why redirects are not
        followed.
        """
        url = self.absolute(path)
        request = Request(url)
        try:
            with self._opener.open(request, timeout=60) as response:
                if getattr(response, "status", 200) in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location", "")
                    raise self._session_error(path, location)
                html = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location", "")
                raise self._session_error(path, location) from exc
            raise
        if "error=token-exception" in html:
            raise TokenExpired(
                f"{path} was bounced to the login page for an expired token")
        return html

    @staticmethod
    def _session_error(path: str, location: str) -> EPFOError:
        """Turn a login redirect into a precise, actionable error."""
        if "session-exception" in location:
            return SessionExpired(
                f"{path} -> {location}: the server did not recognise this "
                "session. Log in again; EPFO sessions are short-lived.")
        if "token-exception" in location:
            return TokenExpired(f"{path} -> {location}: token no longer valid.")
        return EPFOError(f"{path} redirected to {location}")

    def post_raw(self, path: str, data: dict) -> str:
        """POST form data and return the body verbatim.

        Needed because the portal's AJAX endpoints do not share one response
        format: the balance and arch endpoints answer JSON, while
        ``get-member-yearly-passbook-data`` answers a fragment of HTML that the
        page drops straight into the DOM. Parsing that as JSON would throw away
        the ledger.
        """
        return self._request(path, data=data)

    def post_json(self, path: str, data: dict) -> Any:
        """POST form data and parse the response as JSON.

        The portal's AJAX endpoints answer with a JSON *string body*, not
        ``application/json``, and they signal failure with ``success:false`` plus
        an ``error`` field at HTTP 200.
        """
        raw = self._request(path, data=data)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EPFOError(
                f"{path} returned non-JSON ({len(raw)} bytes): {raw[:200]!r}"
            ) from exc

    # -- persistence -------------------------------------------------------

    def save_cookies(self, path: Path | None = None) -> Path:
        """Write the jar in Netscape format, which MozillaCookieJar can read.

        Two details matter: the attribute is ``domain_specified`` (not
        ``domain_specific`` - a typo here meant this method always raised), and
        ``MozillaCookieJar.load()`` rejects a file without the magic header
        line, so it is written explicitly.
        """
        target = Path(path or self.cookies_path or "epfo-cookies.txt")
        lines = ["# Netscape HTTP Cookie File", ""]
        lines += [
            "\t".join([
                c.domain,
                "TRUE" if c.domain_specified else "FALSE",
                c.path,
                "TRUE" if c.secure else "FALSE",
                str(int(c.expires)) if c.expires else "0",
                c.name,
                c.value or "",
            ])
            for c in self._jar
        ]
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        target.chmod(0o600)
        return target

    def load_cookies(self, path: Path | None = None) -> None:
        from http.cookiejar import MozillaCookieJar
        target = Path(path or self.cookies_path or "epfo-cookies.txt")
        jar = MozillaCookieJar(str(target))
        jar.load(ignore_discard=True, ignore_expires=True)
        self._opener.close()
        self._jar = jar
        self._opener = _ConnOpener(jar, ssl_context())
