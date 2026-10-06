"""Read the EPFO **Unified Portal** (claims, KYC, transfers) through a real browser.

The passbook host (``passbook.epfindia.gov.in``) is a plain HTTP client's
playground - see ``session.py``. The Unified Portal is not. Its login POST was
reproduced in Python with byte-exact crypto (the password hash matched the page's
own), and even the browser's *captured* POST body and cookies replayed over
``urllib`` still answered ``302 -> error.jsp``. The gate is a browser-level signal
(a ``_sec_sess_id`` security-session cookie plus a WAF), so a headless HTTP client
cannot authenticate. See ``docs/unified-portal-claims.md``.

So this module does the only thing that works: it drives a **real Chrome** the
operator has started with a debugging port, over the Chrome DevTools Protocol, using
Playwright's ``connect_over_cdp``. Playwright is an optional extra
(``pip install 'epfo-cli[browser]'``); the rest of the CLI never needs it.

Two behaviours are load-bearing:

* **Typing must be real.** The page's fields react only to trusted input events, so
  the visible ``#userName``/``#password`` are filled through the driver - assigning
  ``.value`` in JavaScript leaves the hidden twins empty and the login silently
  fails.
* **Navigation must be a click.** A direct navigation to any in-app URL after login
  invalidates the session (``Session Error`` / ``error.jsp``). Only clicking the
  page's own nav link works, and the nav tokens are single-use.

Honest limits, encoded here on purpose: the OTP is mandatory and goes to the
Aadhaar-linked mobile (a human must supply it, so nothing here is unattended), and
the *claim* form itself is gated behind a valid KYC bank + IFSC, so this reads the
surface and reports the blocker rather than pretending a claim can be filed.
"""

from __future__ import annotations

import time

BASE = "https://unifiedportal-mem.epfindia.gov.in/memberinterface/"

# The claim-relevant pages, by the href fragment the nav uses. Order is the order
# they are visited; each is read from the page currently loaded (never by URL).
CLAIM_PAGES: list[tuple[str, str]] = [
    ("claim", "cenonline/claim/getReceipt"),
    ("claim-status", "cenonline/claim/onlineClaimStatus"),
    ("transfer", "cenOtcpMemberInterface/loadTxClaimHome"),
    ("kyc", "kyc/viewKYCRegistrationForm"),
    ("service-history", "memberServiceHistoryNew/loadMemberServiceHistory"),
    ("nomination", "eNomination/geteNominationPage"),
]


class UnifiedError(RuntimeError):
    """Raised when the browser session cannot be driven."""


def _sync_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - only without the extra
        raise UnifiedError(
            "the browser integration needs Playwright. Install it with "
            "`pip install 'epfo-cli[browser]'` and `playwright install chromium`."
        ) from exc
    return sync_playwright


class UnifiedSession:
    """Drives one open Chrome page through the Unified Portal.

    A real browser is not a convenience here - the portal refuses a scripted HTTP
    login (``error.jsp``), so it is the only transport that authenticates.
    """

    def __init__(self, endpoint: str = "127.0.0.1:9222", timeout: float = 30.0):
        self.endpoint = endpoint if "://" in endpoint else f"http://{endpoint}"
        self.timeout = timeout
        self._pw = None
        self._browser = None
        self._page = None

    # -- lifecycle --------------------------------------------------------

    def __enter__(self) -> "UnifiedSession":
        sync_playwright = _sync_playwright()
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.connect_over_cdp(self.endpoint)
        except Exception as exc:
            raise UnifiedError(
                f"cannot reach a Chrome at {self.endpoint} ({exc}). Start one with "
                "--remote-debugging-port=9222 (see README).") from exc
        self._page = self._new_tab()
        # Submit-time actions sit behind a native confirm(); unless dialogs are
        # accepted, the click is silently swallowed.
        self._page.on("dialog", lambda dialog: dialog.accept())
        return self

    def __exit__(self, *exc_info) -> None:
        try:
            if self._browser is not None:
                self._browser.close()  # detaches; the operator's Chrome stays open
        finally:
            if self._pw is not None:
                self._pw.stop()

    def _new_tab(self):
        """A fresh tab, so a stale error page elsewhere cannot be picked up."""
        context = (self._browser.contexts[0] if self._browser.contexts
                   else self._browser.new_context())
        return context.new_page()

    def close(self) -> None:
        self.__exit__(None, None, None)

    # -- primitives -------------------------------------------------------

    def _js(self, expression: str):
        return self._page.evaluate(expression)

    def _settle(self, seconds: float = 1.2) -> None:
        self._page.wait_for_load_state("domcontentloaded")
        time.sleep(seconds)

    def state(self) -> dict:
        return self._js(
            "(() => ({"
            "  url: location.href,"
            "  title: document.title,"
            "  hasLogin: !!document.querySelector('#userName'),"
            "  hasOtp: !!document.querySelector('#otp'),"
            "  hasClaimLink: !!document.querySelector(\"a[href*='cenonline/claim/getReceipt']\"),"
            "  sessionError: /Session Error|Session is expired/.test(document.body.innerText)"
            "}))()")

    # -- login + OTP ------------------------------------------------------

    def open_login(self) -> None:
        """Load a fresh login page.

        The ``dataId``, challenge and ``_HDIV_STATE_`` rotate per page load, so a
        stale page cannot authenticate. A direct navigation is safe *here*; the
        "never navigate by URL" rule applies only to in-app pages after login.
        """
        self._page.goto(BASE, wait_until="domcontentloaded",
                        timeout=self.timeout * 1000)
        self._page.wait_for_selector("#userName", timeout=self.timeout * 1000)
        self.dismiss_alert()

    def dismiss_alert(self) -> None:
        """Close the "Dear EPF Members!!" notice modal.

        It is an overlay that intercepts pointer events, so a real click on the
        login button never lands until it is gone. A JS `click()` on its close
        button does not satisfy Bootstrap, so close it for real, then force the
        overlay away as a fallback (the modal is re-shown on every page load).
        """
        try:
            if self._page.query_selector("#btnCloseModal"):
                self._page.click("#btnCloseModal", timeout=3000)
        except Exception:
            pass
        try:
            self._js(
                "(() => {"
                "  document.querySelectorAll('.modal.show').forEach(m => {"
                "    m.classList.remove('show'); m.style.display='none'; });"
                "  document.querySelectorAll('.modal-backdrop').forEach(b => b.remove());"
                "  document.body.classList.remove('modal-open');"
                "  document.body.style.overflow='';"
                "})()")
        except Exception:
            pass
        time.sleep(0.3)

    def submit_credentials(self, uan: str, password: str) -> None:
        """Fill the visible fields and let the page's own JS hash + POST them.

        The password lives only in this browser call - never on disk, never in the
        CLI's output.
        """
        self._page.fill("#userName", uan)
        self._page.fill("#password", password)
        # A real click, not a JS `e.click()`: the login form's handler is bound in
        # a way that only a trusted mouse event triggers, so `eval_on_selector`
        # leaves the hidden twins unbuilt and the POST never fires.
        self._page.click('button:has-text("Sign in")')
        time.sleep(2.5)
        # A second login while one is still live raises the concurrent-session
        # alert instead of the OTP page. Its "Login Here" button is the portal's
        # own "take over this session" action - the same choice a human would make.
        self._resolve_concurrent()
        self._wait_for(
            "() => !!document.querySelector('#otp')"
            " || document.title === 'EPFO: Home'"
            " || /error.jsp/.test(location.href)")

    def _resolve_concurrent(self) -> None:
        """Dismiss the concurrent-session alert by clicking "Login Here"."""
        visible = self._js(
            "(() => { const b=document.querySelector('#loginHereButton');"
            " if(!b || b.offsetParent===null) return false;"
            " const m=document.querySelector('#concurrenttSessionAlert');"
            " return !!m && (m.classList.contains('show')"
            " || getComputedStyle(m).display !== 'none'); })()")
        if visible:
            try:
                self._page.click("#loginHereButton", timeout=4000)
                time.sleep(2.0)
            except Exception:
                pass

    def otp_id(self) -> str | None:
        """The OTP-ID the portal printed, for the operator to cross-check."""
        return self._js(
            "(() => { const m=document.body.innerText.match(/OTP-ID\\s*:?\\s*(\\d+)/);"
            " return m ? m[1] : null; })()")

    def submit_otp(self, otp: str) -> None:
        self._page.fill("#otp", otp)
        self._page.click("#submitButton")
        self._wait_for(
            "() => document.title === 'EPFO: Home'"
            " || !!document.querySelector(\"a[href*='cenonline']\")"
            " || /error.jsp/.test(location.href)")

    def _wait_for(self, condition_js: str) -> None:
        try:
            self._page.wait_for_function(condition_js,
                                         timeout=self.timeout * 1000)
        except Exception:
            pass
        self._settle()

    # -- reading the surface ---------------------------------------------

    def nav_links(self) -> list[dict]:
        return self._js(
            "Array.from(document.querySelectorAll('a[href]'))"
            ".map(a => ({text:(a.innerText||'').trim(), href:a.getAttribute('href')}))"
            ".filter(l => l.href && l.href !== '#')")

    def click_and_read(self, href_fragment: str) -> dict:
        """Click the nav link matching ``href_fragment`` and return the page.

        Uses the page's own JavaScript ``click()``: a real DOM click on a
        collapsed-menu item never becomes visible, and ``goto`` invalidates the
        session. This is the only navigation that survives.
        """
        clicked = self._js(
            "(() => { const a=document.querySelector("
            f"\"a[href*='{href_fragment}']\"); if(!a) return false; a.click(); return true; }})()")
        if not clicked:
            return {"absent": True}
        self._settle(1.5)
        self.dismiss_alert()
        return {
            "url": self._js("location.href"),
            "title": self._js("document.title"),
            "text": self._js("document.body.innerText"),
        }

    def go_home(self) -> None:
        self._js("(() => { const a=document.querySelector("
                 "\"a[href*='/memberinterface/home']\"); if(a) a.click(); })()")
        self._settle(1.0)
