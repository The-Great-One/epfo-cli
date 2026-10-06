"""Command line interface for the EPFO member passbook portal.

Every subcommand prints what it actually did. Where the portal refuses or the
client cannot be sure, the command exits non-zero with the portal's own message
instead of a success-shaped guess.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict
from getpass import getpass
from pathlib import Path
from urllib.request import Request

from . import __version__
from .captcha import (
    SOLVERS, CaptchaOCRError, auto_answer, available_solvers, ocr_available,
    solve,
)
from .config import (
    CONFIG_PATH, SERVICE_NAME, Profile, SecretStorageUnavailable,
    delete_password, load_password, load_profile, save_profile, store_password,
)
from .crypto import encode_password
from .discover import Endpoint, fetch_scripts, merge, scan_inline_params,     scan_source, write_report
from .store import Observation, Store
from .models import (parse_profile, parse_service_history,
                     parse_yearly_passbook)
from .passbook import (
    ARCH_PATH,
    LedgerClient,
    PassbookClient,
    extract_members,
    extract_page_token,
    extract_pdf_token,
    nav_tokens,
)
from .session import (
    BASE, CaptchaRequired, EPFOError, EPFOSession, SessionExpired, TokenExpired,
    plain_opener,
)

LOGIN_URL = f"{BASE}/login"
# The portal's own JS finishes a successful login with
# `window.location.replace('home2?token=' + t)`, so /home2 - not /passbook - is
# the landing page. Both live behind the token guard.
POST_LOGIN_LANDING = "/home2"
PASSBOOK_PATHS = ("/passbook", POST_LOGIN_LANDING)


# -- helpers --------------------------------------------------------------

def _session_from_profile(profile: Profile) -> EPFOSession:
    session = EPFOSession(cookies_path=Path(profile.cookies_path)
                          if profile.cookies_path else None)
    if profile.cookies_path and Path(profile.cookies_path).exists():
        try:
            session.load_cookies()
        except Exception as exc:
            print(f"note: could not restore cookies ({exc}); starting fresh",
                  file=sys.stderr)
    return session


def _answer_captcha(captcha, *, auto: bool, solver: str | None = None) -> str:
    """Return a captcha answer, reading it automatically when possible.

    Automatic reading is tried first because that is what makes the client
    drivable without a human. It is never trusted blindly: the reading must
    pass a plausibility gate, and ``solver`` lets the caller name which backend
    to use so that consecutive attempts can alternate between them. If reading
    is unavailable or implausible, this falls back to asking a person and the
    command still completes - the fallback is the guarantee, reading is the
    convenience.
    """
    target = Path.cwd() / "captcha.jpg"
    captcha.write_png(target)

    if auto:
        try:
            guess = auto_answer(captcha, solver=solver)
        except CaptchaOCRError as exc:
            print(f"note: automatic captcha failed ({exc}); asking a human",
                  file=sys.stderr)
        else:
            label = solver or "auto"
            print(f"captcha {guess} (read automatically via {label})")
            return guess

    print(f"captcha saved to {target.resolve()}")
    if sys.platform == "darwin":
        subprocess.run(["open", str(target)], check=False)
    elif os.environ.get("DISPLAY"):
        subprocess.run(["xdg-open", str(target)], check=False)
    try:
        return input("captcha answer: ").strip()
    except EOFError as exc:
        # Reached when OCR failed *and* stdin is closed (an unattended run with
        # no pipe). Without this, Python's own EOFError escaped as an unhandled
        # traceback instead of a diagnosable failure.
        raise EPFOError(
            "no captcha answer was available: automatic reading failed and "
            "stdin is closed, so a human cannot be asked. Run interactively, "
            "or install tesseract (`brew install tesseract`) for automatic "
            "reading.") from exc


def _password_from_stdin() -> str:
    """Read a password from a pipe, so credential commands can run unattended.

    A TTY is refused on purpose: silently consuming a terminal's first line
    would hang every interactive invocation. A piped password is the explicit,
    opt-in form (``--password-stdin``), which is what makes an unattended run
    possible without ever putting the secret in argv, where ``ps`` would show
    it.
    """
    if sys.stdin.isatty():
        raise EPFOError(
            "--password-stdin was given but stdin is a terminal; pipe the "
            "password in, e.g. `printf '%s' \"$PW\" | epfo-cli passbook "
            "--password-stdin`")
    value = sys.stdin.readline().strip()
    if not value:
        raise EPFOError("--password-stdin was given but stdin was empty")
    return value


def _authenticated_read(session: EPFOSession, uan: str, password: str, *,
                        auto_captcha: bool, attempts: int, read):
    """Log in, run ``read(token)``, and survive one lapsed session.

    The portal's session is short-lived and can lapse mid-read (it answers
    ``session-exception``). Retrying is *safe* here because a balance read is
    idempotent - there is no write to duplicate - so the client re-authenticates
    once and repeats the read rather than failing the whole command. A second
    lapse is reported as a real failure instead of looping.
    """
    token, code = _login(session, uan, password, auto_captcha=auto_captcha,
                         attempts=attempts)
    if token is None:
        return None, code

    try:
        return read(token), 0
    except (SessionExpired, TokenExpired) as exc:
        print(f"note: the session lapsed ({exc}); re-authenticating once",
              file=sys.stderr)

    token, code = _login(session, uan, password, auto_captcha=auto_captcha,
                         attempts=attempts)
    if token is None:
        return None, code
    try:
        return read(token), 0
    except (SessionExpired, TokenExpired) as exc:
        print(f"error: the session lapsed again after re-authentication: {exc}",
              file=sys.stderr)
        return None, 3


# -- commands -------------------------------------------------------------

def _resolve_credentials(args: argparse.Namespace, profile) -> tuple[str, str] | None:
    """Return (uan, password) or None after printing why it could not."""
    uan = args.uan or profile.uan
    if not uan:
        print("error: no UAN. Pass --uan or run `epfo-cli config --uan <UAN>`.",
              file=sys.stderr)
        return None
    if getattr(args, "password_stdin", False):
        try:
            return uan, _password_from_stdin()
        except EPFOError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return None
    try:
        password = load_password(uan)
    except SecretStorageUnavailable as exc:
        # A *broken* keychain must be visible. Silently falling through to a
        # prompt once hid a missing dependency and made --store-password look
        # like it had simply not saved anything.
        print(f"note: {exc}", file=sys.stderr)
        password = None
    if not password:
        password = getpass(f"EPFO password for UAN {uan}: ")
    if args.store_password:
        try:
            store_password(uan, password)
            print(f"password stored in the OS keychain (service 'epfo-cli', "
                  f"account '{uan}')")
        except SecretStorageUnavailable as exc:
            # A failed *store* must not abort the *use*: the password is already
            # in memory, so the command can still proceed. Aborting here made
            # `passbook --store-password` exit 2 on a machine without keyring,
            # which is a worse outcome than simply not persisting the secret.
            print(f"note: could not store the password ({exc}); continuing "
                  "with this run only", file=sys.stderr)
        except Exception as exc:
            print(f"note: could not store the password ({exc}); continuing "
                  "with this run only", file=sys.stderr)
    return uan, password


def _login(session, uan: str, password: str, *, auto_captcha: bool,
           attempts: int) -> tuple[str | None, int]:
    """Log in, returning (session_token, exit_code). exit_code 0 means success.

    A fresh token is fetched alongside every captcha, because the portal rotates
    the token on each captcha request - a retry with a stale token cannot
    succeed.
    """
    try:
        captcha = session.start()
    except EPFOError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None, 1

    # Alternate readers across attempts. Benchmarked against eight live
    # captchas, ddddocr and tesseract disagreed on four - every time an O/9 or
    # T/7 confusion - so a second attempt with the other reader is a genuinely
    # new bet, not a repeat of the one that just failed.
    readers = SOLVERS if auto_captcha else (None,)

    for attempt in range(1, attempts + 1):
        solver = readers[(attempt - 1) % len(readers)]
        answer = _answer_captcha(captcha, auto=auto_captcha, solver=solver)
        try:
            result = session.login(uan, password, answer)
        except EPFOError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return None, 1

        if result.ok and not result.otp_required:
            return result.raw.get("object"), 0

        if result.otp_required:
            panel = Path.cwd() / "otp-panel.html"
            panel.write_text(result.otp_html, encoding="utf-8")
            print("the portal is asking for an OTP (one-time password).")
            print(f"its OTP panel was saved to {panel.resolve()}")
            print("this client detects OTP challenges but does not submit OTPs.")
            return None, 4

        print(f"attempt {attempt}/{attempts} refused: {result.message}")
        if attempt < attempts:
            try:
                captcha = session.refresh_captcha()
            except EPFOError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return None, 1
    return None, 5


def cmd_login(args: argparse.Namespace) -> int:
    profile = load_profile()
    resolved = _resolve_credentials(args, profile)
    if resolved is None:
        return 2
    uan, password = resolved

    session = _session_from_profile(profile)
    token, code = _login(session, uan, password, auto_captcha=args.auto_captcha,
                         attempts=args.attempts)
    if token is None:
        return code

    profile.uan = uan
    save_profile(profile)
    print("login ok. The session is live in this process only.")
    print("note: EPFO rejects a reused session (session-exception), so the "
          "authenticated commands log in themselves rather than restoring "
          "cookies. Run `epfo-cli passbook`, not `login` then `passbook`.")
    return 0


def cmd_passbook(args: argparse.Namespace) -> int:
    """Log in, then read the member balances over the portal's own API."""
    profile = load_profile()
    resolved = _resolve_credentials(args, profile)
    if resolved is None:
        return 2
    uan, password = resolved

    session = _session_from_profile(profile)
    client = PassbookClient(session)
    # _authenticated_read logs in itself. An earlier version also called _login
    # here, so every passbook run authenticated twice - two captchas and two
    # login POSTs per read, visible as a duplicated captcha line in the output.
    result, code = _authenticated_read(
        session, uan, password, auto_captcha=args.auto_captcha,
        attempts=args.attempts, read=client.read)
    if result is None:
        return code

    for note in result.notes:
        print(f"note: {note}", file=sys.stderr)

    if args.dump_html:
        target = Path(args.dump_html)
        target.write_text(result.home_html, encoding="utf-8")
        print(f"raw home page saved to {target.resolve()}")

    if args.json:
        print(json.dumps({
            "summary": result.summary,
            "balances": [b.as_dict() for b in result.balances],
            "total": result.total,
            "notes": result.notes,
        }, indent=2, default=str))
        return 0

    for label in ("UAN", "Member Name", "Date of Birth"):
        if result.summary.get(label):
            print(f"{label}: {result.summary[label]}")
    print()
    print(f"{'member id':<18}{'total':>14}{'employee':>14}{'employer':>14}")
    for balance in result.balances:
        if not balance.ok:
            print(f"{balance.member_id[-12:]:<18}{'--':>14}  {balance.error}")
            continue
        print(f"{balance.member_id[-12:]:<18}"
              f"{(balance.total or 0):>14,.2f}"
              f"{(balance.employee_share or 0):>14,.2f}"
              f"{(balance.employer_share or 0):>14,.2f}")
    print(f"{'TOTAL':<18}{result.total:>14,.2f}")
    print()
    print("Balances are the portal's own figures. This is the summary page only; "
          "run `epfo-cli ledger` for the month-by-month passbook.")
    return 0


def _record_and_report(accounts, keep=None) -> int:
    """Store the observation, print what changed, and exit non-zero on change.

    The exit code is the point of the whole thing: a cron job can run
    ``ledger --store`` and alert on a non-zero exit, so "tell me when a
    contribution lands" needs no output parsing.
    """
    import datetime

    seen_on = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    changes = Store().record(
        [Observation(member_id=ledger.member_id or label,
                     financial_year=ledger.financial_year or year,
                     rows=ledger.rows)
         for label, ledgers in accounts.items()
         for year, ledger in ledgers.items()],
        seen_on=seen_on, keep=keep)
    if not changes:
        print("no change since the last stored run")
        return 0
    print(f"{len(changes)} change(s) since the last stored run:")
    for change in changes:
        where = f"{change.member_id} {change.financial_year} {change.wage_month}"
        print(f"  {change.kind:<8} {where:<48} {change.detail}")
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    """Report what is in the local store - no login, no portal request."""
    store = Store()
    rows = store.summary()
    if not rows:
        print("nothing is stored yet; run `epfo-cli ledger --store` first")
        return 0
    print(f"stored ledger  ({store.path})")
    print(f"  last read {store.last_seen() or '(unknown)'}")
    print(f"  years     {', '.join(reversed(store.years())) or '(none)'}")
    print()
    width = max(len(m) for m in rows)
    for member_id, count in rows.items():
        print(f"  {member_id:<{width}}  {count:>3} row(s)")
    return 0


def cmd_ledger(args: argparse.Namespace) -> int:
    """Log in and print (or write) the month-by-month ledger.

    This is the whole passbook, not just the balances: the portal serves it from
    a separate page behind its own token, and that page is only reachable by
    reading the nav menu the home page publishes.
    """
    profile = load_profile()
    resolved = _resolve_credentials(args, profile)
    if resolved is None:
        return 2
    uan, password = resolved

    session = _session_from_profile(profile)
    client = LedgerClient(session)

    def read(token: str) -> dict[str, dict[str, object]]:
        """Return {member_id: {year: YearlyPassbook}} for every member account.

        A UAN can hold several member ids, one per establishment, and the ledger
        endpoint serves whichever member is currently selected. So each member is
        switched to and read in turn - reading once and assuming it covers the
        account reports a fraction of the money.
        """
        home = PassbookClient(session).home(token)
        nav_token = nav_tokens(home).get("passbook")
        if not nav_token:
            raise EPFOError(
                "the home page did not publish a passbook page token; the nav "
                "menu may have changed")

        members = extract_members(home)
        if args.member:
            members = [m for m in members if m.human_id.endswith(args.member)]
            if not members:
                raise EPFOError(
                    f"no member id ending in {args.member!r} is linked to this UAN")
        if not members:
            # Layout change: fall back to whichever member the portal has
            # selected, and say so rather than pretending this is the only one.
            print("note: no member list found on the home page; reading the "
                  "currently selected member only", file=sys.stderr)
            members = [None]

        accounts: dict[str, dict[str, object]] = {}
        for member in members:
            if member is not None:
                switched = client.switch_member(member.human_id)
                if not isinstance(switched, dict) or not switched.get("success"):
                    print(f"note: could not switch to {member.human_id}: "
                          f"{switched}", file=sys.stderr)
                    continue
            page, yearly_token = client.passbook_page(nav_token)
            if not yearly_token:
                raise EPFOError(
                    "the passbook page did not embed a token for the yearly "
                    "ledger; the page layout may have changed")
            if args.year:
                years = [args.year]
            else:
                # The initial selector is incomplete: the archive call returns a
                # selector that adds the older years, so ask for it and union.
                arch_token = extract_page_token(page, ARCH_PATH)
                years = sorted(
                    set(client.years(page))
                    | set(client.archived_years(arch_token) if arch_token else []),
                    reverse=True)
                years = years or [str(time.localtime().tm_year)]
            label = member.human_id if member is not None else "selected"
            accounts[label] = {
                year: parse_yearly_passbook(client.yearly(yearly_token, year))
                for year in years}
        return accounts

    accounts, code = _authenticated_read(
        session, uan, password, auto_captcha=args.auto_captcha,
        attempts=args.attempts, read=read)
    if accounts is None:
        return code

    # With --store the exit code carries the answer: 0 = nothing changed,
    # 1 = something did. That is what lets a cron job alert without parsing
    # output. It is computed here and returned at the end, after the ledger has
    # printed, because the table is still useful in a terminal.
    change_code = _record_and_report(accounts, keep=args.keep) if args.store else 0

    if args.out:
        target = Path(args.out)
        if target.suffix == ".csv":
            with target.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["member_id", "financial_year", "wage_month",
                                 "transaction_date", "transaction_type",
                                 "particulars", "epf_wages", "eps_wages",
                                 "employee_share", "employer_share",
                                 "pension_share"])
                for label, ledgers in accounts.items():
                    for ledger in ledgers.values():
                        for row in ledger.rows:
                            writer.writerow([ledger.member_id or label,
                                             ledger.financial_year, row.wage_month,
                                             row.transaction_date,
                                             row.transaction_type, row.particulars,
                                             row.epf_wages, row.eps_wages,
                                             row.employee_share, row.employer_share,
                                             row.pension_share])
        else:
            target.write_text(json.dumps({
                label: {
                    year: {
                        "member_id": ledger.member_id,
                        "financial_year": ledger.financial_year,
                        "rows": [r.as_dict() for r in ledger.rows],
                        "totals": ledger.totals,
                        "summary": ledger.summary,
                        "notes": ledger.notes,
                    } for year, ledger in ledgers.items()
                } for label, ledgers in accounts.items()
            }, indent=2, default=str), encoding="utf-8")
        print(f"wrote ledger to {target.resolve()}")

    for label, ledgers in accounts.items():
        for year, ledger in ledgers.items():
            for note in ledger.notes:
                print(f"note: {note}", file=sys.stderr)
            print(f"Passbook {ledger.financial_year or year}  "
                  f"member {ledger.member_id or label}")
            if not ledger.rows:
                # A year can hold interest only. Saying so and still printing the
                # summary is the honest report: the closing balance is real money
                # even when no contribution was made.
                print("  no contribution rows were returned")
            else:
                print(f"  {'wage month':<11}{'txn date':<12}{'epf wages':>12}"
                      f"{'employee':>12}{'employer':>12}{'pension':>10}")
                for row in ledger.rows:
                    print(f"  {row.wage_month:<11}{row.transaction_date:<12}"
                          f"{(row.epf_wages or 0):>12,.0f}"
                          f"{(row.employee_share or 0):>12,.2f}"
                          f"{(row.employer_share or 0):>12,.2f}"
                          f"{(row.pension_share or 0):>10,.2f}")
            # Print every summary line, not only the "Total" ones: the interest
            # credited and the closing balance live there, and dropping them
            # silently would hide money that the totals do not include.
            for title, values in ledger.summary.items():
                print(f"  {title}: {', '.join(values)}")
            print()
    return change_code


def _fetch_nav_page(session, name: str, home_token: str) -> str:
    """Fetch one of the portal's nav pages by the token the home page gives it.

    The tokens are the portal's own: each is published in the nav markup of the
    page you are on and is consumed by navigating with it. Reading the token off
    the home page and fetching the target in one hop is what works; reusing a
    token harvested from a *different* page answers ``invalid-token``.
    """
    home = PassbookClient(session).home(home_token)
    token = nav_tokens(home).get(name)
    if not token:
        raise EPFOError(
            f"the home page did not publish a token for {name!r}; the nav menu "
            "may have changed")
    return session.get(f"/{name}?token={token}")


def cmd_service_history(args: argparse.Namespace) -> int:
    """Print the employment history: each establishment and the stint in it."""
    profile = load_profile()
    resolved = _resolve_credentials(args, profile)
    if resolved is None:
        return 2
    uan, password = resolved

    session = _session_from_profile(profile)

    def read(token: str):
        return parse_service_history(
            _fetch_nav_page(session, "service-history", token))

    history, code = _authenticated_read(
        session, uan, password, auto_captcha=args.auto_captcha,
        attempts=args.attempts, read=read)
    if history is None:
        return code

    if _emit(args, history.as_dict(), "service history"):
        return 0
    for note in history.notes:
        print(f"note: {note}", file=sys.stderr)
    for label, value in (("Total experience", history.total_experience),
                         ("Date of joining", history.date_of_joining),
                         ("Total NCP days", history.total_ncp_days)):
        if value:
            print(f"{label}: {value}")
    print()
    if not history.records:
        print("no establishment records were returned")
        return 0
    # The longest service string is 24 characters ("2 Years 4 Months 28 Days"),
    # so the column is padded wider than that or it runs into the ncp column.
    print(f"  {'employer':<42}{'joining':<13}{'exit':<13}"
          f"{'service':<26}{'ncp':<8}")
    for record in history.records:
        print(f"  {record.employer[:41]:<42}{record.joining_date or '-':<13}"
              f"{record.exit_date or '-':<13}{record.total_service or '-':<26}"
              f"{record.ncp_days or '-':<8}")
    return 0


def cmd_profile(args: argparse.Namespace) -> int:
    """Print the profile page's fields (personal and KYC details)."""
    profile = load_profile()
    resolved = _resolve_credentials(args, profile)
    if resolved is None:
        return 2
    uan, password = resolved

    session = _session_from_profile(profile)

    def read(token: str):
        return parse_profile(_fetch_nav_page(session, "profile", token))

    fields, code = _authenticated_read(
        session, uan, password, auto_captcha=args.auto_captcha,
        attempts=args.attempts, read=read)
    if fields is None:
        return code

    if _emit(args, fields, "profile"):
        return 0
    if not fields:
        print("no profile fields were returned")
        return 0
    width = max(len(k) for k in fields)
    for label, value in fields.items():
        print(f"  {label:<{width}}  {value}")
    return 0


def cmd_pdf(args: argparse.Namespace) -> int:
    """Ask the portal to generate the passbook PDF and save it."""
    profile = load_profile()
    resolved = _resolve_credentials(args, profile)
    if resolved is None:
        return 2
    uan, password = resolved

    session = _session_from_profile(profile)
    client = LedgerClient(session)

    def read(token: str):
        home = PassbookClient(session).home(token)
        nav_token = nav_tokens(home).get("passbook")
        if not nav_token:
            raise EPFOError("the home page did not publish a passbook token")
        page, yearly_token = client.passbook_page(nav_token)
        years = client.years(page) or [str(time.localtime().tm_year)]
        year = args.year or years[0]
        fragment = client.yearly(yearly_token, year)
        pdf_token = extract_pdf_token(fragment)
        if not pdf_token:
            raise EPFOError("the yearly passbook did not carry a PDF token")
        return year, client.pdf(pdf_token, year)

    result, code = _authenticated_read(
        session, uan, password, auto_captcha=args.auto_captcha,
        attempts=args.attempts, read=read)
    if result is None:
        return code
    year, payload = result
    body = payload if isinstance(payload, dict) else {}
    if body.get("status") == 0:
        print(f"PDF generated for {year}: {json.dumps(body.get('object'), default=str)}")
        return 0
    # The portal answers status 1 with the server's own exception text. Pass it
    # through verbatim: this endpoint has been observed throwing a
    # NullPointerException (uanServicePageList is null) for a token and year it
    # otherwise accepts, so the cause is the portal's, not the request's - and
    # saying "failed: <server text>" is the only honest thing to print.
    print(f"PDF generation for {year} failed on the portal's side: "
          f"{body.get('message') or json.dumps(payload, default=str)}",
          file=sys.stderr)
    return 1


def cmd_discover(args: argparse.Namespace) -> int:
    sources: dict[str, str] = {}

    static = [f"{BASE}/login",
              f"{BASE}/static/js/sha512.js",
              f"{BASE}/static/js/crypto-js.min.js"]
    opener = plain_opener()
    for url in static:
        try:
            request = Request(url, headers={"User-Agent": "epfo-cli"})
            with opener.open(request, timeout=60) as response:
                sources[url] = response.read().decode("utf-8", "replace")
        except Exception as exc:
            print(f"note: could not fetch {url}: {exc}", file=sys.stderr)

    if args.endpoint:
        try:
            sources.update(fetch_scripts(args.endpoint, args.target,
                                         origins=args.origins))
        except Exception as exc:
            print(f"note: CDP capture failed ({exc}); using static sources only",
                  file=sys.stderr)

    maps = [scan_source(body, label) for label, body in sources.items()]
    if f"{BASE}/login" in sources:
        maps.append(scan_inline_params(sources[f"{BASE}/login"],
                                       "inline:login"))

    endpoints = merge(*maps)
    if not endpoints:
        print("error: no endpoints discovered; the portal may be unreachable.",
              file=sys.stderr)
        return 1

    print(f"discovered {len(endpoints)} endpoints from {len(sources)} sources\n")
    for endpoint in endpoints:
        params = f"  params: {', '.join(endpoint.parameters)}" if endpoint.parameters else ""
        print(f"  {endpoint.normalized}{params}")
        for source in endpoint.sources:
            print(f"      from {source}")

    if args.out:
        write_report(endpoints, Path(args.out))
        print(f"\nreport written to {Path(args.out).resolve()}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Log in and write the member balances to a file.

    This replaces an older implementation that called ``session.get`` on a path
    and tried to parse a contributions table from the raw HTML. That was dead
    on arrival twice over: it needed a saved session (which the portal rejects),
    and it parsed the *summary* page with ``parse_passbook``, which has never
    seen a real page. The month-by-month ledger lives in ``ledger``
    (``parse_yearly_passbook``); this command reports the balances the home page
    actually serves.
    """
    profile = load_profile()
    resolved = _resolve_credentials(args, profile)
    if resolved is None:
        return 2
    uan, password = resolved

    session = _session_from_profile(profile)
    client = PassbookClient(session)
    result, code = _authenticated_read(
        session, uan, password, auto_captcha=args.auto_captcha,
        attempts=args.attempts, read=client.read)
    if result is None:
        return code

    target = Path(args.out)
    rows = [b.as_dict() for b in result.balances]
    if target.suffix == ".csv":
        with target.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["member_id", "total", "employee_share",
                             "employer_share", "ok", "error"])
            for row in rows:
                writer.writerow([row["member_id"], row["total"],
                                 row["employee_share"], row["employer_share"],
                                 row["ok"], row["error"]])
    else:
        target.write_text(json.dumps({
            "summary": result.summary,
            "balances": rows,
            "total": result.total,
            "notes": result.notes,
        }, indent=2, default=str), encoding="utf-8")
    print(f"exported {len(rows)} balances to {target.resolve()}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    profile = load_profile()
    changed = False
    if args.uan:
        profile.uan = args.uan
        changed = True
    if args.cookies:
        profile.cookies_path = str(Path(args.cookies).expanduser())
        changed = True
    if args.delete_password:
        print("password removed from keychain" if delete_password(profile.uan)
              else "no stored password to remove")
    if changed:
        path = save_profile(profile)
        print(f"saved to {path}")
    print(f"profile: {profile.display}")
    print(f"  cookies: {profile.cookies_path or '<unset>'}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from .config import keyring_available

    checks: list[tuple[str, bool, str]] = []
    try:
        request = Request(LOGIN_URL, headers={"User-Agent": "epfo-cli"})
        with plain_opener().open(request, timeout=45) as response:
            checks.append(("portal reachable", response.status == 200,
                           f"HTTP {response.status} from {LOGIN_URL}"))
    except Exception as exc:
        checks.append(("portal reachable", False, str(exc)))

    checks.append(("OS keychain", keyring_available(),
                   "passwords can be stored securely" if keyring_available()
                   else "no keychain: the password will be prompted per run"))
    found = available_solvers()
    checks.append(("captcha readers", bool(found),
                   f"available: {', '.join(found)}; captchas are read "
                   "automatically and the readers alternate across attempts"
                   if found else
                   "none available - install ddddocr (pip install ddddocr) or "
                   "tesseract (brew install tesseract), or captchas will be "
                   "asked for on each run"))

    for name, ok, detail in checks:
        print(f"[{'ok ' if ok else 'no '}] {name}: {detail}")
    return 0 if all(ok for name, ok, _ in checks[:1]) else 1


# -- parser ---------------------------------------------------------------

def _add_credential_flags(parser: argparse.ArgumentParser) -> None:
    """The flags every credentialed command shares.

    The switch is ``--no-auto-captcha`` rather than ``--ocr`` because reading
    the captcha automatically is now the default. The flag exists to turn that
    *off* when a human would rather read the image themselves; ``--attempts``
    still bounds the retries either way.
    """
    parser.add_argument("--auto-captcha", action=argparse.BooleanOptionalAction,
                        default=True,
                        help="read the captcha automatically (default); "
                             "--no-auto-captcha asks you instead")
    parser.add_argument("--attempts", type=int, default=6,
                        help="captcha retries before giving up (automatic "
                             "reading is imperfect, so the default is generous)")
    parser.add_argument("--store-password", action="store_true",
                        help="save the password in the OS keychain")
    parser.add_argument("--password-stdin", action="store_true",
                        help="read the password from stdin (unattended runs)")


def _add_output_flags(parser: argparse.ArgumentParser) -> None:
    """The flags the read-only reporting commands share."""
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--out", help="write the JSON report here")


def _emit(args: argparse.Namespace, payload, what: str) -> bool:
    """Write ``--out`` and/or print ``--json``; True when JSON was printed.

    The commands that use this read a *page*, not the ledger, so the
    human-readable table is drawn by each command. Returning True on ``--json``
    lets them skip it - otherwise a ``--json`` run would print JSON *and* the
    table, which nothing can parse.
    """
    if args.out:
        target = Path(args.out)
        target.write_text(json.dumps(payload, indent=2, default=str),
                          encoding="utf-8")
        print(f"wrote {what} to {target.resolve()}")
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        return True
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="epfo-cli",
        description="Command line client for the EPFO member passbook portal.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="authenticate and cache a session")
    login.add_argument("--uan", help="UAN; defaults to the saved profile")
    _add_credential_flags(login)
    login.set_defaults(func=cmd_login)

    discover = sub.add_parser(
        "discover", help="enumerate the portal's endpoints from its own JS")
    discover.add_argument("--endpoint", help="CDP endpoint, e.g. 127.0.0.1:9333")
    discover.add_argument("--target", help="CDP page target id")
    discover.add_argument("--origins", nargs="*",
                          help="only keep scripts from these origins")
    discover.add_argument("--out", help="write a JSON report here")
    discover.set_defaults(func=cmd_discover)

    passbook = sub.add_parser("passbook", help="fetch and parse the passbook")
    passbook.add_argument("--uan", help="UAN; defaults to the saved profile")
    _add_credential_flags(passbook)
    passbook.add_argument("--json", action="store_true", help="print JSON")
    passbook.add_argument("--limit", type=int, default=12)
    passbook.add_argument("--dump-html",
                          help="save the raw HTML for calibration")
    passbook.set_defaults(func=cmd_passbook)

    ledger = sub.add_parser(
        "ledger", help="fetch the month-by-month passbook (the full ledger)")
    ledger.add_argument("--uan")
    ledger.add_argument("--year",
                        help="financial year as the portal shows it, e.g. 2026")
    ledger.add_argument("--member",
                        help="only this member id (or its last few digits)")
    ledger.add_argument("--out", help="write .json or .csv here")
    _add_credential_flags(ledger)
    ledger.add_argument("--store", action="store_true",
                        help="remember this read locally and report what changed")
    ledger.add_argument("--keep", type=int, metavar="YEARS",
                        help="with --store, keep only the N most recent years")
    ledger.set_defaults(func=cmd_ledger)

    status = sub.add_parser(
        "status", help="report the locally stored ledger (no login)")
    status.set_defaults(func=cmd_status)

    history = sub.add_parser(
        "service-history", help="print the employment history per establishment")
    history.add_argument("--uan")
    _add_credential_flags(history)
    _add_output_flags(history)
    history.set_defaults(func=cmd_service_history)

    who = sub.add_parser("profile", help="print profile / KYC fields")
    who.add_argument("--uan")
    _add_credential_flags(who)
    _add_output_flags(who)
    who.set_defaults(func=cmd_profile)

    pdf = sub.add_parser("pdf", help="ask the portal to generate the passbook PDF")
    pdf.add_argument("--uan")
    pdf.add_argument("--year")
    _add_credential_flags(pdf)
    pdf.set_defaults(func=cmd_pdf)

    export = sub.add_parser("export", help="export contributions to a file")
    export.add_argument("--uan")
    _add_credential_flags(export)
    export.add_argument("--out", required=True, help=".json or .csv")
    export.set_defaults(func=cmd_export)

    config = sub.add_parser("config", help="manage the saved profile")
    config.add_argument("--uan")
    config.add_argument("--cookies")
    config.add_argument("--delete-password", action="store_true")
    config.set_defaults(func=cmd_config)

    doctor = sub.add_parser("doctor",
                            help="check connectivity and optional dependencies")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except EPFOError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
