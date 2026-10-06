"""Parsing of the portal's passbook HTML into typed records.

The portal serves HTML built from nested tables with no stable ids, so nothing
here assumes a fixed layout. Extraction is driven by header labels and by
label/value adjacency, and every field that could not be located is returned as
``None`` with a note, rather than being filled with a plausible guess.

Calibration: run ``epfo-cli export --dump-html`` to save the raw page. If the
portal is restructured, ``parse_passbook`` should be updated against that real
HTML - the ``tests/test_models.py`` fixtures are trimmed from live pages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from html import unescape


class _TableExtractor(HTMLParser):
    """Collect HTML tables as lists of rows of cell text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table_stack: list[list[list[str]]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "table":
            self._table_stack.append([])
        elif tag == "tr" and self._table_stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._table_stack:
            self.tables.append(self._table_stack.pop())
        elif tag == "tr" and self._row is not None and self._table_stack:
            self._table_stack[-1].append(self._row)
            self._row = None
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(_squash("".join(self._cell)))
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def extract_tables(html: str) -> list[list[list[str]]]:
    """Every table on the page, as rows of cell strings."""
    parser = _TableExtractor()
    parser.feed(html)
    return parser.tables


def extract_label_values(html: str) -> dict[str, str]:
    """Label/value pairs from the portal's key-value display blocks.

    The portal renders member details as ``<span>Label</span><span>Value</span>``
    style pairs inside definition-like tables. Rather than depend on that, any
    row of exactly two non-empty cells is treated as a pair.
    """
    pairs: dict[str, str] = {}
    for table in extract_tables(html):
        for row in table:
            cells = [c for c in row if c]
            if len(cells) == 2 and not cells[0].isdigit():
                label = cells[0].rstrip(":").strip()
                if label and label not in pairs:
                    pairs[label] = cells[1]
    return pairs


_MEMBER_KEYS = {
    "uan": ("uan", "universal account number"),
    "name": ("name", "member name", "employee name"),
    "member_id": ("member id", "member identification number", "memberid"),
    # Establishment name/id are intentionally absent: they live in `attributes`
    # and in parse_establishments(), and naming them here would require fields
    # that do not exist on MemberDetails.
    "date_of_birth": ("date of birth", "dob"),
    "date_of_exit": ("date of exit", "doe"),
    "aadhaar_linked": ("aadhaar", "aadhar"),
}


@dataclass
class MemberDetails:
    """Who the passbook belongs to, as reported by the portal itself."""

    uan: str | None = None
    name: str | None = None
    member_id: str | None = None
    date_of_birth: str | None = None
    date_of_exit: str | None = None
    aadhaar_linked: str | None = None
    establishments: list[str] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)

    def missing(self) -> list[str]:
        return [k for k in ("uan", "name", "member_id") if not getattr(self, k)]


@dataclass
class ContributionRow:
    """One passbook transaction line."""

    particulars: str
    wage_month: str | None = None
    employee_share: float | None = None
    employer_share: float | None = None
    pension_share: float | None = None
    epf_interest: float | None = None
    balance: float | None = None
    raw: list[str] = field(default_factory=list)


@dataclass
class MemberBalance:
    """One member id's balance, as returned by the portal's balance API.

    ``member_id`` is the opaque ``data-mid`` value taken straight from the
    post-login page. That value is what the balance API expects: four of them
    were sent live and each returned ``success: true`` with real figures, so no
    translation between page markup and API is needed.

    The API answers with HTTP 200 even when it fails, signalling that via
    ``success: false`` and an ``error`` string, so ``ok`` reflects the body and
    not the status code.
    """

    member_id: str = ""
    total: float | None = None
    employee_share: float | None = None
    employer_share: float | None = None
    raw_mid: str = ""
    ok: bool = False
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "member_id": self.member_id,
            "total": self.total,
            "employee_share": self.employee_share,
            "employer_share": self.employer_share,
            "ok": self.ok,
            "error": self.error,
        }


@dataclass
class Passbook:
    """A parsed passbook page."""

    member: MemberDetails
    contributions: list[ContributionRow] = field(default_factory=list)
    establishments: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


_AMOUNT_RE = re.compile(r"^-?[\d,]+(?:\.\d{1,2})?$")
_MONTH_RE = re.compile(r"(?:[A-Za-z]{3,9}[-/ ]\d{2,4})|(?:\d{2}[-/]\d{4})")


def _to_amount(text: str | None) -> float | None:
    if not text:
        return None
    cleaned = text.replace(",", "").replace("\u20b9", "").strip()
    if not _AMOUNT_RE.match(text.strip().replace("\u20b9", "")):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_member(html: str) -> MemberDetails:
    """Pull member identity fields out of a passbook page."""
    label_values = extract_label_values(html)
    lowered = {k.lower(): v for k, v in label_values.items()}
    member = MemberDetails(attributes=label_values)
    for attribute, aliases in _MEMBER_KEYS.items():
        for alias in aliases:
            for key, value in lowered.items():
                if alias in key:
                    setattr(member, attribute, value)
                    break
            if getattr(member, attribute):
                break
    return member


def parse_establishments(html: str) -> list[tuple[str, str]]:
    """Establishment code/name pairs offered as passbook tabs."""
    pairs: list[tuple[str, str]] = []
    for table in extract_tables(html):
        for row in table:
            cells = [c for c in row if c]
            if len(cells) >= 2 and re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3,}", cells[0]):
                pairs.append((cells[0], cells[-1]))
    return pairs


def parse_contributions(html: str) -> tuple[list[ContributionRow], list[str]]:
    """Parse the contribution table.

    The header is located by looking for a row that mentions both a wage month
    and a balance-like column. Column meaning is then bound positionally to that
    header, so a layout change is detectable rather than silently mis-parsed.
    """
    notes: list[str] = []
    rows: list[ContributionRow] = []
    header: list[str] | None = None
    for table in extract_tables(html):
        for row in table:
            cells = [c.lower() for c in row if c]
            joined = " ".join(cells)
            if ("wage month" in joined or "month" in joined) and \
                    ("balance" in joined or "employee share" in joined):
                header = cells
                break
        if header:
            for row in table:
                cells = [c for c in row if c]
                if [c.lower() for c in cells] == header:
                    continue
                if not any(_AMOUNT_RE.match(c.replace("\u20b9", "").strip())
                           or _MONTH_RE.search(c) for c in cells):
                    continue
                rows.append(_row_from(cells, header))
            break
    if header is None:
        notes.append("contribution table header not found; page layout differs "
                     "from the calibrated one")
    return rows, notes


def _row_from(cells: list[str], header: list[str]) -> ContributionRow:
    def pick(*needles: str) -> str | None:
        for index, column in enumerate(header):
            if any(n in column for n in needles):
                if index < len(cells):
                    return cells[index]
        return None

    particulars = cells[0] if cells else ""
    if "particular" not in header[0] if header else True:
        particulars = next((c for c in cells if not _AMOUNT_RE.match(c)), "")
    return ContributionRow(
        particulars=particulars,
        wage_month=pick("wage", "month"),
        employee_share=_to_amount(pick("employee")),
        employer_share=_to_amount(pick("employer")),
        pension_share=_to_amount(pick("pension")),
        epf_interest=_to_amount(pick("interest")),
        balance=_to_amount(pick("balance")),
        raw=cells,
    )


def parse_home_summary(html: str) -> dict[str, str]:
    """Label/value pairs from the post-login home page (UAN, name, status)."""
    return extract_label_values(html)


def parse_passbook(html: str) -> Passbook:
    """Top-level parse of a passbook page."""
    contribution_rows, notes = parse_contributions(html)
    return Passbook(
        member=parse_member(html),
        contributions=contribution_rows,
        establishments=parse_establishments(html),
        notes=notes,
    )


# -- the yearly passbook ----------------------------------------------------
#
# Verified live against the portal's own response. The endpoint
# ``get-member-yearly-passbook-data`` (post body ``year=YYYY``) answers an HTML
# fragment that the page drops straight into the DOM. Unlike the balance
# endpoint it really does carry the month-by-month ledger:
#
#     Wage Month | Transaction Date | Type | Particulars | EPF Wages |
#     EPS Wages | Employee Share (12%) | Employer Share (3.67%) |
#     Pension Share (8.33%)
#
# The fragment holds ONE table whose first row is a *summary* header
# ("Particulars | Employee Share | Employer Share | Pension Share"), with the
# real transaction header appearing several rows later. Binding columns to the
# first row that merely mentions a month therefore mis-parses every row - which
# is exactly the bug this parser was written to fix.


@dataclass
class YearlyRow:
    """One month's contribution line from the yearly passbook."""

    wage_month: str = ""
    transaction_date: str = ""
    transaction_type: str = ""
    particulars: str = ""
    epf_wages: float | None = None
    eps_wages: float | None = None
    employee_share: float | None = None
    employer_share: float | None = None
    pension_share: float | None = None
    raw: list[str] = field(default_factory=list)

    @property
    def is_withdrawal(self) -> bool:
        """True for a withdrawal, which the portal marks with a ``-`` type."""
        return self.transaction_type.strip() == "-"

    def as_dict(self) -> dict:
        return {
            "wage_month": self.wage_month,
            "transaction_date": self.transaction_date,
            "transaction_type": self.transaction_type,
            "particulars": self.particulars,
            "epf_wages": self.epf_wages,
            "eps_wages": self.eps_wages,
            "employee_share": self.employee_share,
            "employer_share": self.employer_share,
            "pension_share": self.pension_share,
        }


@dataclass
class YearlyPassbook:
    """A full financial year of a member's passbook."""

    member_id: str | None = None
    financial_year: str | None = None
    rows: list[YearlyRow] = field(default_factory=list)
    summary: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def totals(self) -> dict[str, list[str]]:
        """Only the portal's own TOTAL lines, keyed by their label."""
        return {k: v for k, v in self.summary.items()
                if k.lower().startswith("total")}


# The portal writes the heading as
#   Passbook for Member Id : [ <b class="color-primary">MID</b> ] [ <b>2026 - 2027</b> ]
_HEADING_RE = re.compile(
    r"Passbook for Member Id\s*:\s*\[\s*<b[^>]*>\s*([^<]+?)\s*</b>\s*\]"
    r"\s*\[\s*<b[^>]*>\s*([^<]+?)\s*</b>", re.I | re.S)

# A row whose first cell begins with one of these is a summary line, not a
# contribution: it must never be counted as a month's payment.
_SUMMARY_PREFIXES = ("ob int", "total", "interest", "closing balance", "opening balance")

# A contribution row's month cell *starts* with the month ("May-2026"). Matching a
# date anywhere in the cell is not enough: the interest line reads
# "Int. Updated upto 31/03/2026", whose embedded date made it parse as a month's
# payment, and "int." does not match the "interest" prefix either.
_WAGE_MONTH_RE = re.compile(
    r"^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-/ ]\d{2,4}"
    r"|^\d{1,2}[-/]\d{4}", re.I)


def parse_yearly_passbook(html: str) -> YearlyPassbook:
    """Parse the yearly passbook fragment into months plus the portal's totals."""
    result = YearlyPassbook()
    heading = _HEADING_RE.search(html)
    if heading:
        result.member_id = _squash(heading.group(1))
        result.financial_year = _squash(heading.group(2))
    else:
        result.notes.append(
            "the passbook heading was not found; member id and year are unknown")

    header: list[str] | None = None
    for table in extract_tables(html):
        for row in table:
            cells = [c.lower() for c in row if c]
            # "wage month" is the strong signal. A row that only says "month"
            # is the summary header, and binding to it mis-parses every row.
            if any("wage month" in c for c in cells):
                # Stored lowercased: `pick()` matches needles against these, and
                # a case-sensitive comparison against the portal's
                # "Wage Month (12%)" style headings silently binds nothing.
                header = [c for c in cells]
                break
        if header is None:
            continue
        seen_header = False
        for row in table:
            cells = [c for c in row if c]
            if not cells:
                continue
            if not seen_header:
                seen_header = [c.lower() for c in cells] == header
                continue
            if cells[0].lower().startswith(_SUMMARY_PREFIXES):
                result.summary[cells[0]] = cells[1:]
                continue
            candidate = _yearly_row(cells, header)
            # The table also carries a colspan'd disclaimer footer and an interest
            # line, both of which look like rows. A real contribution line names a
            # wage month, so that is what decides - not the cell count.
            if not _WAGE_MONTH_RE.match(candidate.wage_month):
                result.summary.setdefault(candidate.wage_month or " ".join(cells),
                                          cells[1:])
                continue
            result.rows.append(candidate)
        break

    if header is None:
        result.notes.append(
            "no transaction table was found in the yearly passbook; the portal "
            "layout may have changed")
    return result


def _yearly_row(cells: list[str], header: list[str]) -> YearlyRow:
    def pick(*needles: str) -> str:
        for index, column in enumerate(header):
            if all(n in column for n in needles):
                return cells[index] if index < len(cells) else ""
        return ""

    def pick_amount(*needles: str) -> float | None:
        return _to_amount(pick(*needles))

    return YearlyRow(
        wage_month=pick("wage", "month").strip(),
        transaction_date=pick("transaction", "date").strip(),
        transaction_type=pick("transaction", "type").strip(),
        particulars=pick("particular").strip(),
        epf_wages=pick_amount("epf", "wage"),
        eps_wages=pick_amount("eps", "wage"),
        employee_share=pick_amount("employee"),
        employer_share=pick_amount("employer"),
        pension_share=pick_amount("pension"),
        raw=cells,
    )


# -- the profile and service-history pages ----------------------------------
#
# Both are static server-rendered pages: the values are in the markup, so they
# need an HTML parse rather than another endpoint. They share one shape -
#
#     <p class="...bold...">LABEL</p>          <- the label cell
#     <p class="text-muted mb-0">VALUE</p>     <- the adjacent value cell
#
# -- so one regex pair serves both, given the right labels.

# A label/value pair. The value may wrap a <span> (badges, calendar icons), so
# the capture is de-tagged before use.
# Between the label's </p> and the value's <p> the portal may put any run of
# column divs, a rule or a break - and the run differs between a card field and a
# timeline field, so the whole run is skipped rather than one arrangement required.
_BETWEEN = r"(?:</?div[^>]*>\s*|<hr[^>]*>\s*|<br[^>]*>\s*)*"

# Label and value are told apart by their classes, which is the only thing that
# separates them reliably: the timeline's *employer* line is a bold <p> just like
# a label, so matching on "bold" paired the employer with the "Est Id" value and
# then swallowed the real Est Id field. A label is `mb-0` and neither of the value
# classes; a value carries `text-muted` or `text-md-start`.
_LABEL_VALUE_RE = re.compile(
    r'<p[^>]*class="(?![^"]*text-muted)(?![^"]*text-md-start)[^"]*mb-0[^"]*"[^>]*>'
    r'\s*([A-Za-z][A-Za-z /()\.\-]{1,40}?)\s*</p>\s*' + _BETWEEN +
    r'<p[^>]*class="[^"]*(?:text-muted|text-md-start)[^"]*"[^>]*>\s*(.*?)</p>',
    re.S)


def _clean(text: str) -> str:
    """Strip tags/entities and collapse whitespace from a captured value."""
    text = re.sub(r"<[^>]+>", " ", text)
    return _squash(unescape(text).replace("\xa0", " "))


def label_values(html: str) -> dict[str, str]:
    """Every ``LABEL</p> ... VALUE</p>`` pair on a page, in document order.

    Later duplicates win, which is what matters for a page that repeats a
    heading before its value.
    """
    pairs: dict[str, str] = {}
    for label, value in _LABEL_VALUE_RE.findall(html):
        label = _squash(unescape(label))
        if label and label not in pairs:
            pairs[label] = _clean(value)
    return pairs


@dataclass
class ServiceRecord:
    """One establishment's stint, from the service-history timeline."""

    employer: str = ""
    establishment_id: str = ""
    member_id: str = ""
    ncp_days: str = ""
    joining_date: str = ""
    exit_date: str = ""
    total_service: str = ""


@dataclass
class ServiceHistory:
    """UAN-level service summary plus each establishment's stint."""

    total_experience: str = ""
    date_of_joining: str = ""
    total_ncp_days: str = ""
    records: list[ServiceRecord] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "total_experience": self.total_experience,
            "date_of_joining": self.date_of_joining,
            "total_ncp_days": self.total_ncp_days,
            "establishments": [
                {"employer": r.employer, "establishment_id": r.establishment_id,
                 "member_id": r.member_id, "ncp_days": r.ncp_days,
                 "joining_date": r.joining_date, "exit_date": r.exit_date,
                 "total_service": r.total_service}
                for r in self.records],
        }


# One timeline entry: the number badge, the period, the employer, then the
# label/value grid inside it.
_TIMELINE_ITEM_RE = re.compile(
    r'<li class="timeline-item[^"]*">(.*?)</li>', re.S)
_TIMELINE_EMPLOYER_RE = re.compile(
    r'class="[^"]*bold[^"]*"[^>]*>\s*([^<]{3,80}?)\s*</p>')
_TIMELINE_PERIOD_RE = re.compile(r'class="tlp[^"]*"[^>]*>\s*([^<]+?)\s*</p>')


def parse_service_history(html: str) -> ServiceHistory:
    """Parse the service-history page.

    The per-establishment fields repeat the same labels in every timeline entry
    (``Est Id``, ``Member Id``, ``Joining Date`` ...), so the page is split into
    its timeline items first and each item parsed on its own - a page-wide
    label/value sweep would collapse four employers into one set of values.
    """
    result = ServiceHistory()
    overview = html.split('class="timeline"')[0]
    summary = label_values(overview)
    result.total_experience = summary.get("Total Experience", "")
    result.date_of_joining = summary.get("Date of Joining", "")
    result.total_ncp_days = summary.get("Total NCP Days", "")

    for item in _TIMELINE_ITEM_RE.findall(html):
        employer = _TIMELINE_EMPLOYER_RE.search(item)
        period = _TIMELINE_PERIOD_RE.search(item)
        fields = label_values(item)
        record = ServiceRecord(
            employer=_clean(employer.group(1)) if employer else "",
            establishment_id=fields.get("Est Id", ""),
            member_id=fields.get("Member Id", ""),
            ncp_days=fields.get("NCP Days", ""),
            joining_date=_date_only(fields.get("Joining Date", "")),
            exit_date=_date_only(fields.get("Exit Date", "")) or _period_end(period),
            total_service=fields.get("Total Service", ""),
        )
        if record.employer or record.member_id:
            result.records.append(record)

    if not result.records:
        result.notes.append(
            "no service-history timeline entries were found; the page layout "
            "may have changed")
    return result


_DATE_RE = re.compile(r"\d{2}-[A-Za-z]{3}-\d{4}")


def _date_only(text: str) -> str:
    """Drop the calendar icon and spacing the portal trails after a date."""
    match = _DATE_RE.search(text)
    return match.group(0) if match else text.strip()


def _period_end(period: re.Match | None) -> str:
    """The right-hand side of 'May 2026 - Present' when Exit Date is absent."""
    if not period:
        return ""
    _, _, end = period.group(1).partition(" - ")
    return end.strip() if end.strip().lower() != "present" else "Present"


# Labels that are navigation chrome rather than data. The profile page renders
# the nav inside the same label/value shape as the fields, so a naive sweep
# reports "Passbook" as if it were a profile field.
_PROFILE_CHROME = {
    "epfo", "home", "profile", "passbook", "claims", "service history",
    "calculators", "epf calculator", "edli calculator", "pension calculator",
    "logout", "visitor count", "uan",
}


def parse_profile(html: str) -> dict[str, str]:
    """The profile page's data fields, without the navigation labels.

    Scoped to the region that starts at the first details card: everything above
    it is the site nav and the member's name banner, both of which use the same
    markup as a real field.
    """
    start = html.find("Basic Details")
    end = html.find("Visitor Count")
    body = html[start:end] if start >= 0 else html[:end if end >= 0 else len(html)]
    return {label: value for label, value in label_values(body).items()
            if label.lower() not in _PROFILE_CHROME}
