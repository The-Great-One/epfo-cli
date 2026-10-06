"""Passbook client tests, driven by the live response shapes.

Every fixture here mirrors a response captured from the running portal: the
``success/total/ee/er`` JSON from the balance endpoint, and the per-endpoint
tokens embedded in the logged-in page. Field names and error semantics are
copied from those captures, not invented.
"""

from pathlib import Path

import pytest

from epfo.models import MemberBalance, parse_home_summary
from epfo.passbook import BALANCE_PATH, PassbookClient
from epfo.session import EPFOError

# Mirrors the real page: per-endpoint tokens live in inline JavaScript, and
# member ids appear as data-mid attributes.
HOME_HTML = (
    "<script>\n"
    f"var u = '/MemberPassBook{BALANCE_PATH}?token=BALANCE_TOKEN_123';\n"
    "</script>\n"
    '<a href="/MemberPassBook/home?token=HOME_TOKEN_456">home</a>\n'
    '<span name="mid-bal" data-mid="MID_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"></span>\n'
    '<span name="mid-bal" data-mid="MID_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"></span>\n'
    "<table><tr><td>UAN</td><td>100123456789</td></tr></table>"
)

BALANCE_OK = {"success": True, "total": 75600, "ee": 37800, "er": 37800,
              "error": ""}


class _StubSession:
    """A session whose responses are scripted, so no network is involved."""

    def __init__(self, home=HOME_HTML, responses=None):
        self._home = home
        self._responses = responses or {}
        self.posted = []

    def get(self, path):
        return self._home

    def post_json(self, path, data):
        self.posted.append((path, dict(data)))
        result = self._responses.get(data["mid"])
        if isinstance(result, Exception):
            raise result
        return result if result is not None else BALANCE_OK


def test_read_returns_a_balance_per_member_id():
    client = PassbookClient(_StubSession())
    result = client.read("login-token")
    assert len(result.balances) == 2
    assert all(b.ok for b in result.balances)
    assert result.total == 75600 * 2


def test_read_posts_the_balance_endpoint_with_each_member_id():
    session = _StubSession()
    PassbookClient(session).read("t")
    assert len(session.posted) == 2
    paths = {p.split("?")[0] for p, _ in session.posted}
    assert paths == {BALANCE_PATH}
    assert {d["mid"] for _, d in session.posted} == {
        "MID_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "MID_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"}


def test_read_uses_the_endpoint_token_not_the_login_token():
    """The API rejects the login token; the page embeds a per-endpoint one."""
    session = _StubSession()
    PassbookClient(session).read("login-token-value")
    for path, _ in session.posted:
        assert "BALANCE_TOKEN_123" in path
        assert "login-token-value" not in path


def test_read_reports_a_missing_endpoint_token_as_a_note_not_a_crash():
    html = "<html><body>logged in but no balance token</body></html>"
    result = PassbookClient(_StubSession(home=html)).read("t")
    assert result.balances == []
    assert any("token" in n for n in result.notes)


def test_read_reports_missing_member_ids_as_a_note():
    html = (f"<script>var u = '{BALANCE_PATH}?token=TTT';</script>"
            "<html>no ids</html>")
    result = PassbookClient(_StubSession(home=html)).read("t")
    assert result.balances == []
    assert any("member ids" in n for n in result.notes)


def test_a_failing_member_records_its_error_without_aborting_the_others():
    session = _StubSession(responses={
        "MID_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA": EPFOError("boom"),
    })
    result = PassbookClient(session).read("t")
    assert len(result.balances) == 2
    failed = next(b for b in result.balances if not b.ok)
    assert "boom" in failed.error
    assert result.total == 75600  # only the succeeding member contributes


def test_success_false_is_treated_as_failure_despite_http_200():
    """The API answers 200 with success:false; ok must follow the body."""
    session = _StubSession(responses={
        "MID_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA":
            {"success": False, "total": None, "ee": None, "er": None,
             "error": "Member ID not found"},
    })
    result = PassbookClient(session).read("t")
    failed = next(b for b in result.balances if not b.ok)
    assert failed.error == "Member ID not found"
    assert failed.total is None


def test_home_summary_is_parsed_from_the_logged_in_page():
    summary = parse_home_summary(HOME_HTML)
    assert summary.get("UAN") == "100123456789"


def test_member_balance_as_dict_is_json_safe():
    import json

    payload = MemberBalance(member_id="x", total=1.0, ok=True).as_dict()
    assert json.dumps(payload)  # must not raise
    assert payload["ok"] is True
