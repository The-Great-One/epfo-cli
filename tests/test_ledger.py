"""The yearly ledger and the pages that serve it.

Everything here is pinned to the shape of the portal's real responses: the
yearly fragment holds a summary header *before* the transaction header, so a
parser that binds columns to the first month-ish row mis-reads every value, and
a colspan'd disclaimer footer looks exactly like a row.
"""

from __future__ import annotations

import pytest

from epfo.models import parse_yearly_passbook
from epfo.passbook import (
    LedgerClient,
    extract_members,
    extract_page_token,
    extract_pdf_token,
    nav_tokens,
)

FRAGMENT = r"""<div class="pb-wrap">
  <div class="pb-heading">Passbook for Member Id : [ <b class="color-primary">GNGGN00000000000000001</b> ] [ <b>2026 - 2027</b> ]</div>
  <table class="table table-bordered">
    <tr><th>Particulars</th><th>Employee Share</th><th>Employer Share</th><th>Pension Share</th></tr>
    <tr><td>OB Int. Updated Upto</td><td>&#8377; 0</td><td>&#8377; 0</td><td>&#8377; 0</td></tr>
    <tr>
      <th>Wage Month (12%)</th><th>Transaction Date</th><th>Type</th><th>Particulars</th>
      <th>EPF Wages</th><th>EPS Wages</th><th>Employee Share (12%)</th>
      <th>Employer Share (3.67%)</th><th>Pension Share (8.33%)</th>
    </tr>
    <tr><td>May-2026</td><td>01-06-2026</td><td>+</td><td>Contribution</td><td>30,000</td><td>30,000</td><td>3,600</td><td>3,600</td><td>0</td></tr>
    <tr><td>Jun-2026</td><td>17-09-2026</td><td>+</td><td>Contribution</td><td>30,000</td><td>30,000</td><td>3,600</td><td>3,600</td><td>0</td></tr>
    <tr><td>Jul-2026</td><td>17-09-2026</td><td>+</td><td>Contribution</td><td>30,000</td><td>30,000</td><td>3,600</td><td>3,600</td><td>0</td></tr>
    <tr><td>Aug-2026</td><td>17-09-2026</td><td>+</td><td>Contribution</td><td>30,000</td><td>30,000</td><td>3,600</td><td>3,600</td><td>0</td></tr>
    <tr><td>Total Contributions for the year [ 2026 ]</td><td>&#8377; 14,400</td><td>&#8377; 14,400</td><td>&#8377; 0</td></tr>
    <tr><td>Total Withdrawals for the year [ 2026 ]</td><td>&#8377; 0</td><td>&#8377; 0</td><td>&#8377; 0</td></tr>
    <tr><td>Int. Updated upto 31/03/2027</td><td>&#8377; 1,200</td><td>&#8377; 0</td><td>&#8377; 0</td></tr>
    <tr><td>Closing Balance as on 31/03/2027</td><td>&#8377; 14,400</td><td>&#8377; 14,400</td><td>&#8377; 0</td></tr>
    <tr><td colspan="9">Disclaimer - Information shown above is based on available data on central server.</td></tr>
  </table>
  <button class="pb-pdf" onclick="genpdf(2026, 'PDFTOKEN1', this)">Download</button>
</div>"""

HOME = """
<a class="page-url" data-name="home" data-token="HOMETOK">Home</a>
<a class="page-url" data-name="passbook" data-token="PBTOK">Passbook</a>
<a class="page-url" data-name="profile" data-token="PROFTOK">Profile</a>
"""

PAGE = (
    '<select id="selectfy"><option value="2026">2026-2027</option>'
    '<option value="2025">2025-2026</option></select>'
    '<script>$.post("/passbook/api/ajax/get-member-yearly-passbook-data?token=YEARTOK")</script>'
)


def test_parses_each_contribution_month():
    ledger = parse_yearly_passbook(FRAGMENT)
    assert [r.wage_month for r in ledger.rows] == [
        "May-2026", "Jun-2026", "Jul-2026", "Aug-2026"]
    assert all(r.employee_share == 3600.0 for r in ledger.rows)
    assert all(r.employer_share == 3600.0 for r in ledger.rows)
    assert all(r.epf_wages == 30000.0 for r in ledger.rows)


def test_binds_columns_to_the_transaction_header_not_the_summary_header():
    # The summary header also carries "Employee Share"; binding to it would
    # swap wages into the share columns and read every row wrong.
    ledger = parse_yearly_passbook(FRAGMENT)
    may = ledger.rows[0]
    assert may.epf_wages == 30000.0
    assert may.employee_share == 3600.0
    assert may.transaction_date == "01-06-2026"


def test_heading_carries_member_and_year():
    ledger = parse_yearly_passbook(FRAGMENT)
    assert ledger.member_id == "GNGGN00000000000000001"
    assert ledger.financial_year == "2026 - 2027"


def test_interest_line_is_not_counted_as_a_month():
    # "Int. Updated upto 31/03/2026" carries a date, so a search-anywhere month
    # test admits it as a payment row; and its first cell is "int.", which the
    # "interest" summary prefix does not match either.
    ledger = parse_yearly_passbook(FRAGMENT)
    assert len(ledger.rows) == 4
    assert not any("Int." in r.wage_month for r in ledger.rows)
    assert not any(r.epf_wages == 1200.0 for r in ledger.rows)


def test_interest_credited_is_surfaced_not_dropped():
    # Excluding it from rows must not lose it: the interest is real money that
    # the "Total Contributions" line does not include, so it has to appear in
    # the summary the CLI prints.
    ledger = parse_yearly_passbook(FRAGMENT)
    interest = [k for k in ledger.summary if "Int." in k]
    assert interest, ledger.summary
    assert ledger.summary[interest[0]] == ["\u20b9 1,200", "\u20b9 0", "\u20b9 0"]


def test_disclaimer_footer_is_not_counted_as_a_month():
    # It has the same cell count as a contribution row, so only a month-shaped
    # first cell distinguishes them.
    ledger = parse_yearly_passbook(FRAGMENT)
    assert len(ledger.rows) == 4
    assert not any("Disclaimer" in r.wage_month for r in ledger.rows)


def test_totals_keep_only_the_portal_total_lines():
    ledger = parse_yearly_passbook(FRAGMENT)
    assert ledger.totals["Total Contributions for the year [ 2026 ]"] == [
        "\u20b9 14,400", "\u20b9 14,400", "\u20b9 0"]


def test_withdrawal_type_is_recognised():
    ledger = parse_yearly_passbook(FRAGMENT)
    assert not any(r.is_withdrawal for r in ledger.rows)


def test_a_fragment_without_a_table_is_reported_not_guessed():
    ledger = parse_yearly_passbook("<div>nothing here</div>")
    assert ledger.rows == []
    assert ledger.notes


def test_nav_tokens_pair_name_with_token_regardless_of_attribute_order():
    assert nav_tokens(HOME) == {
        "home": "HOMETOK", "passbook": "PBTOK", "profile": "PROFTOK"}


def test_pdf_token_comes_from_the_fragment():
    assert extract_pdf_token(FRAGMENT) == "PDFTOKEN1"


def test_page_token_is_read_for_the_path_that_needs_it():
    from epfo.passbook import YEARLY_PATH
    assert extract_page_token(PAGE, YEARLY_PATH) == "YEARTOK"


# A UAN can hold several member ids, one per establishment. The home page
# carries two different identifiers per row, and they are not interchangeable.
HOME_WITH_MEMBERS = """
<div name="mid-list">
  <span name="mid-mid" data-mid="OPAQUETOKEN1" title="Member Id">
      GNGGN00000000000000001
  </span>
  <span name="mid-mid" data-mid="OPAQUETOKEN2" title="Member Id">
      PYKRP00000000000000002
  </span>
</div>
"""


def test_extracts_every_member_with_both_identifiers():
    members = extract_members(HOME_WITH_MEMBERS)
    assert [m.human_id for m in members] == [
        "GNGGN00000000000000001", "PYKRP00000000000000002"]
    assert [m.token for m in members] == ["OPAQUETOKEN1", "OPAQUETOKEN2"]


def test_member_ids_are_stripped_of_the_surrounding_whitespace():
    # The markup wraps the id in newlines and indentation; a raw capture would
    # carry them into the switch request.
    for member in extract_members(HOME_WITH_MEMBERS):
        assert member.human_id == member.human_id.strip()
        assert "\n" not in member.human_id


class FakeSession:
    def __init__(self):
        self.gets = []
        self.posts = []

    def get(self, path):
        self.gets.append(path)
        return PAGE

    def post_raw(self, path, data):
        self.posts.append((path, data))
        return FRAGMENT

    def post_json(self, path, data):
        self.posts.append((path, data))
        return {"success": True, "download": False}


def test_archived_years_adds_the_older_year_the_selector_hides():
    # The page's initial selector is incomplete. On the verified account it
    # offered 2026 and 2025 while the archive call added 2024 - a year the ledger
    # endpoint serves fine. Reading the selector alone silently drops that year
    # and the money in it.
    class ArchSession:
        def post_json(self, path, data):
            assert path.startswith("/passbook/api/ajax/get-member-arch-passbook-data")
            return {"success": True, "html":
                    "<select id='selectfy'><option value='2026'>a</option>"
                    "<option value='2025'>b</option>"
                    "<option value='2024'>c</option></select>"}

    assert LedgerClient(ArchSession()).archived_years("TOK") == [
        "2026", "2025", "2024"]


def test_archived_years_is_empty_when_the_portal_refuses():
    class RefusingSession:
        def post_json(self, path, data):
            return {"success": False, "otp": False, "download": False}

    assert LedgerClient(RefusingSession()).archived_years("TOK") == []


def test_switching_member_sends_the_human_id_not_the_opaque_token():
    # The opaque data-mid token answers "Member Not Found" here; the endpoint
    # switches on the human member id.
    session = FakeSession()
    LedgerClient(session).switch_member("GNGGN00000000000000001")
    path, body = session.posts[-1]
    assert path == "/passbook/api/ajax/change-member-id"
    assert body == {"mid": "GNGGN00000000000000001", "mnu": "passbook"}


def test_switch_member_posts_form_encoded():
    session = FakeSession()
    LedgerClient(session).switch_member("GNGGN00000000000000001", menu="profile")
    assert session.posts[-1][1]["mnu"] == "profile"


def test_ledger_client_fetches_the_yearly_fragment_for_a_year():
    session = FakeSession()
    client = LedgerClient(session)
    page, token = client.passbook_page("PBTOK")
    assert session.gets == ["/passbook?token=PBTOK"]
    assert token == "YEARTOK"
    assert client.years(page) == ["2026", "2025"]
    ledger = parse_yearly_passbook(client.yearly(token, "2026"))
    assert len(ledger.rows) == 4
    assert session.posts[0][1] == {"year": "2026"}
