"""Member passbook access, built on the portal's own AJAX API.

Contract read from the logged-in page's inline JavaScript and confirmed live:

    POST /passbook/api/ajax/get-member-trans-passbook-data?token=<page token>
    body: mid=<member id>
    -> {"success": true, "total": ..., "ee": ..., "er": ..., "error": ""}

Two things are easy to get wrong and are handled here:

* **The token is per endpoint, not global.** A logged-in page embeds a distinct
  128-character token for each AJAX endpoint. The token returned by the login
  step is not accepted by the API. ``discover.embedded_tokens`` extracts them.
* **The session is short-lived and bound to the live connection.** Reusing saved
  cookies from another process is rejected with ``session-exception``. Every
  balance must therefore be read in the same process that logged in.

The month-by-month ledger and the PDF are served from *other* pages, each behind
its own token:

    POST /passbook/api/ajax/get-member-yearly-passbook-data?token=<page token>
    body: year=2026
    -> an HTML fragment containing the transaction table plus a PDF token

    POST /passbook/api/ajax/final/generate-passbook-pdf?token=<fragment token>
    body: year=2026
    -> JSON with the generated PDF's location

The token for the passbook page itself is not the login token either - it is
published in the nav menu on the home page, which is why fetching ``/passbook``
with the login token returned an empty shell for so long.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .discover import embedded_tokens, member_ids
from .models import MemberBalance, parse_home_summary
from .session import EPFOError, EPFOSession

BALANCE_PATH = "/passbook/api/ajax/get-member-trans-passbook-data"
PROFILE_PATH = "/passbook/api/check-uan-profile-service"


@dataclass
class PassbookResult:
    """Everything a passbook read produced, plus what it could not read."""

    balances: list[MemberBalance] = field(default_factory=list)
    summary: dict[str, str] = field(default_factory=dict)
    home_html: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(b.total or 0.0 for b in self.balances)


class PassbookClient:
    """Reads a member's balances once a session has logged in."""

    def __init__(self, session: EPFOSession) -> None:
        self.session = session

    def home(self, token: str) -> str:
        """Fetch the post-login home page, which carries the per-API tokens."""
        return self.session.get(f"/home2?token={token}")

    def read(self, token: str) -> PassbookResult:
        """Fetch the home page and every member balance it references."""
        result = PassbookResult(home_html=self.home(token))
        result.summary = parse_home_summary(result.home_html)

        tokens = embedded_tokens(result.home_html)
        balance_token = tokens.get(BALANCE_PATH)
        if not balance_token:
            result.notes.append(
                "the page did not embed a token for the balance API; the portal "
                "layout may have changed")
            return result

        ids = member_ids(result.home_html)
        if not ids:
            result.notes.append("no member ids were present on the page")
            return result

        for mid in ids:
            try:
                payload = self.session.post_json(
                    f"{BALANCE_PATH}?token={balance_token}", {"mid": mid})
            except EPFOError as exc:
                result.balances.append(MemberBalance(member_id=mid,
                                                     error=str(exc)))
                continue
            result.balances.append(MemberBalance(
                member_id=mid,
                total=payload.get("total"),
                employee_share=payload.get("ee"),
                employer_share=payload.get("er"),
                error=payload.get("error") or "",
                ok=bool(payload.get("success")),
            ))
        return result


# -- the ledger pages -------------------------------------------------------

CHANGE_PATH = "/passbook/api/ajax/change-member-id"
YEARLY_PATH = "/passbook/api/ajax/get-member-yearly-passbook-data"
ARCH_PATH = "/passbook/api/ajax/get-member-arch-passbook-data"
PDF_PATH = "/passbook/api/ajax/final/generate-passbook-pdf"

# Nav menu entries are published as data-name/data-token pairs on the home page.
_NAV_RE = re.compile(
    r'data-name="([^"]+)"[^>]*data-token="([^"]+)"'
    r'|data-token="([^"]+)"[^>]*data-name="([^"]+)"')
# The page selector writes value="2026"; the archived-data payload writes
# value='2024' with single quotes. Matching only double quotes silently returned
# no years for the archive - which is how an entire older year went missing.
_YEARS_RE = re.compile(r'''<option[^>]*value=["'](\d{4})["']''')
_PDF_TOKEN_RE = re.compile(r"genpdf\s*\([^,]+,\s*'([A-Za-z0-9]+)'")


def nav_tokens(home_html: str) -> dict[str, str]:
    """Map each nav menu name to its page token.

    The portal publishes one token per page in the home page's nav markup, and
    the token is what ``window.location.replace(menu + '?token=' + token)``
    uses. Reading them from the markup is what makes the passbook page
    reachable at all.
    """
    tokens: dict[str, str] = {}
    for a, b, c, d in _NAV_RE.findall(home_html):
        name, token = (a, b) if a else (d, c)
        tokens.setdefault(name, token)
    return tokens


def extract_pdf_token(fragment: str) -> str | None:
    """The PDF token is embedded in the yearly fragment, not in the page."""
    match = _PDF_TOKEN_RE.search(fragment)
    return match.group(1) if match else None


# A member entry on the home page. The human id is what the ledger is keyed by
# and what ``change-member-id`` expects; the opaque token is only good for the
# balance endpoint, which accepts it as ``mid``.
_MEMBER_RE = re.compile(
    r'name="mid-mid"\s+data-mid="([^"]+)"[^>]*>\s*([^<]+?)\s*</span>')


@dataclass
class Member:
    """One member account under the UAN."""

    human_id: str
    token: str


def extract_members(home_html: str) -> list[Member]:
    """The member accounts listed on the home page, in display order.

    A UAN can hold several member ids (one per establishment). Each row carries
    two different identifiers and they are not interchangeable:
    ``data-mid`` is an opaque per-session token that only the balance endpoint
    understands, while the visible text is the real member id that
    ``change-member-id`` switches on.
    """
    return [Member(human_id=human, token=token)
            for token, human in _MEMBER_RE.findall(home_html)]


def extract_page_token(html: str, path: str) -> str | None:
    """Find the token this page embeds for a given AJAX path."""
    match = re.search(re.escape(path) + r"\?token=([A-Za-z0-9]+)", html)
    return match.group(1) if match else None


class LedgerClient:
    """Reads the yearly ledger and the generated PDF."""

    def __init__(self, session: EPFOSession) -> None:
        self.session = session

    def switch_member(self, human_id: str, menu: str = "passbook") -> Any:
        """Make ``human_id`` the account the passbook pages serve.

        The ledger endpoint reads whichever member is *currently selected*, so
        this must be called before each member's ledger is fetched. Note it takes
        the human member id - the opaque ``data-mid`` token is accepted here and
        answers ``Member Not Found``.
        """
        return self.session.post_json(CHANGE_PATH, {"mid": human_id, "mnu": menu})

    def passbook_page(self, nav_token: str) -> tuple[str, str | None]:
        """Fetch the passbook page and the token it carries for the ledger.

        Returns ``(page_html, yearly_token)``. The yearly token is ``None`` when
        the page did not embed one, which the caller reports rather than
        inventing a request that cannot succeed.
        """
        page = self.session.get(f"/passbook?token={nav_token}")
        return page, extract_page_token(page, YEARLY_PATH)

    def years(self, page_html: str) -> list[str]:
        """The financial years the portal offers in its own selector."""
        return _YEARS_RE.findall(page_html)

    def archived_years(self, token: str) -> list[str]:
        """Years revealed only after the archive button is used.

        The page's initial selector is *not* the full list: the front-end's
        archived-data call returns a replacement selector that adds the older
        years. On the verified account the page offered 2026 and 2025 while this
        added 2024 - a year the ledger endpoint serves happily. Reading the page
        selector alone silently drops it (and with it the money it carried).
        """
        payload = self.session.post_json(f"{ARCH_PATH}?token={token}", {})
        if not isinstance(payload, dict) or not payload.get("success"):
            return []
        return self.years(payload.get("html") or "")

    def yearly(self, token: str, year: str) -> str:
        """Fetch one financial year's passbook fragment (HTML, not JSON)."""
        return self.session.post_raw(f"{YEARLY_PATH}?token={token}", {"year": year})

    def arch(self, token: str) -> Any:
        """Fetch the archived/old passbook data."""
        return self.session.post_json(f"{ARCH_PATH}?token={token}", {})

    def pdf(self, token: str, year: str) -> Any:
        """Ask the portal to generate the passbook PDF and return its payload."""
        return self.session.post_json(f"{PDF_PATH}?token={token}", {"year": year})
