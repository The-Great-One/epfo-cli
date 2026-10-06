# epfo-cli

A command line client for the EPFO member passbook portal
(`passbook.epfindia.gov.in`) — a legacy JSP application with no API.

Reverse-engineered with [REA](https://github.com/morluto/rea) for endpoint capture,
then verified against the live portal with a real account. Every claim below is
something a test or a live run actually exercised; the gaps are stated at the
bottom instead of being papered over.

## Status

| Area | State |
|---|---|
| Password encoding | **Verified** — matches the portal's own `sha512.js` run under Node |
| Login page token/captcha extraction | **Verified live** |
| Captcha rotation / retry semantics | **Verified** against the portal's own source |
| Login request/response contract | **Verified live with a real account** |
| Member balances | **Verified live** — four real balances retrieved |
| Session reuse across processes | **Verified impossible** — server answers `session-exception` |
| Automatic captcha reading | **Verified live** — unattended run, exit 0, no prompt |
| Automatic session refresh | **Implemented + unit-tested** — re-auths once on a lapse |
| Credential persistence | **Verified live** — stored, reloaded, no password prompt |
| Per-month transaction rows | **Verified live** — the yearly ledger, month by month |
| Passbook PDF generation | **Endpoint identified and called correctly; the portal itself answers with a NullPointerException** |
| Install from scratch | **Verified** in a clean venv on Homebrew Python 3.13 |
| TLS against the portal | **Verified**, including on a Python with no CA trust store |

## Install

```bash
cd ~/Desktop/Projects/epfo-cli
python3 -m venv .venv               # use a venv: see the PYTHONPATH note below
env -u PYTHONPATH .venv/bin/python -m pip install -e .
env -u PYTHONPATH .venv/bin/python -m pip install -e '.[keychain]'   # password in the OS keychain
env -u PYTHONPATH .venv/bin/python -m pip install -e '.[ddddocr]'    # the better captcha reader
brew install tesseract                                               # the fallback reader
```

Verified in a clean virtualenv: the only mandatory package is `certifi` and
`epfo-cli` itself.

> **`env -u PYTHONPATH` is not decoration.** This machine exports `PYTHONPATH`
> pointing at another toolchain's `python3.14` site-packages. It leaks into
> every interpreter, so `pip` decides Pillow/numpy are already installed and
> skips them, and the imports that remain resolve to wrong-interpreter builds
> (`ImportError: cannot import name '_imaging' from 'PIL'`). Strip it whenever
> you install or run here. Do **not** install these into a shared or runtime
> interpreter — ddddocr pulls in ~200 MB of numpy/onnxruntime/opencv and can
> move a pinned numpy version out from under another application.

Day to day, just use the venv's own command:

```bash
.venv/bin/epfo-cli passbook
```

## Usage

```bash
epfo-cli doctor                       # connectivity + optional deps
epfo-cli config --uan 100123456789    # remember your UAN (not a secret)
epfo-cli passbook                     # log in, then print balances
epfo-cli passbook --json              # machine-readable
epfo-cli passbook --dump-html p.html  # save the raw page for calibration
epfo-cli passbook --no-auto-captcha   # read the captcha yourself instead
epfo-cli passbook --attempts 10       # more captcha retries
epfo-cli ledger                       # the whole passbook: every member, every year
epfo-cli ledger --out ledger.csv      # ...as CSV (.json also supported)
epfo-cli ledger --year 2025           # a specific financial year
epfo-cli ledger --member 070329       # one member account (id or trailing digits)
epfo-cli service-history              # employment history per establishment
epfo-cli service-history --json       # ...as JSON (--out to write a file)
epfo-cli profile                      # profile + KYC fields
epfo-cli pdf                          # ask the portal for the passbook PDF
epfo-cli export --out pf.json         # write balances to a file
epfo-cli discover --out endpoints.json
```

### The other pages (`service-history`, `profile`)

Both are static server-rendered pages — every value is in the markup, so they
need an HTML parse, not another endpoint. They are reached the same way as the
passbook page, but note **each nav token is single-use**: navigating consumes the
tokens the page you were holding.

```text
home2?token=<login token>            read the nav menu HERE
  └─ GET /service-history?token=<that page's token>   → employment history
  └─ GET /profile?token=<that page's token>           → profile + KYC
```

Harvesting every token from the home page up front and then fetching them all
returns `invalid-token` for every page after the first. Ask for one page, then
re-read the nav from the page you got back.

`service-history` prints the UAN-level totals plus one row per establishment:

```text
  employer                                  joining      exit         service                 ncp
  ACME TECHNOLOGIES PRIVATE LIMITED                01-May-2026  Present      0 Years 5 Months 5 Days 0 Days
  GLOBEX SERVICES PRIVATE LIMITED                21-Apr-2026  30-Apr-2026  0 Years 0 Months 9 Days 0 Days
  INITECH SOLUTIONS PRIVATE LIMITED 09-Dec-2024  17-Apr-2026  1 Years 4 Months 8 Days 0 Days
  PINNACLE FINANCE LIMITED                     04-Jul-2022  02-Dec-2024  2 Years 4 Months 28 Days 0 Days
```

Each timeline entry repeats the same labels (`Est Id`, `Member Id`, `Joining
Date`), so the page is split into its timeline items before parsing — a
page-wide label sweep would collapse every employer into one set of values.

`profile` prints the personal and KYC fields. The nav entries use the same
label/value markup as the data fields, so the parse is scoped to the details
region and the chrome labels are excluded by name.

### The full ledger (`ledger`, `pdf`)

`passbook` prints balances because that is what the home page serves. The
**month-by-month ledger lives on a different page**, and that page is only
reachable by a token the home page publishes in its nav menu:

```text
home2?token=<login token>
  └─ nav menu:  data-name="passbook" data-token=<passbook page token>
       └─ GET  /passbook?token=<passbook page token>
            ├─ POST /passbook/api/ajax/get-member-yearly-passbook-data?token=<page token>
            │       body year=2026  ->  the transaction table (HTML fragment)
            ├─ POST /passbook/api/ajax/get-member-arch-passbook-data?token=<page token>
            │       ->  archived years
            └─ POST /passbook/api/ajax/final/generate-passbook-pdf?token=<token from the fragment>
                    body year=2026  ->  the generated PDF
```

Fetching `/passbook` with the *login* token returns an empty shell — that is
what made the ledger look unavailable. Each page carries its own token, and the
PDF's token appears only inside the yearly fragment.

**The PDF endpoint is broken on the portal's side — but only for years that have
data.** Asked for FY 2025 or 2024 (no data) it answers a clean
`{"status":1,"message":"Passbook Not Available"}`; asked for FY 2026 — the one year
with contributions — it throws the NullPointerException below. So the handler is
reachable and the failure is specific to rendering an actual passbook.

`epfo-cli pdf` sends
exactly what the page's own `genpdf()` sends — same path, same
`?token=<128-char fragment token>`, same `year=2026` body, same jQuery
`X-Requested-With` header — and the portal answers

```json
{"status": 1, "message": "java.lang.NullPointerException: Cannot invoke \"java.util.ArrayList.iterator()\" because \"uanServicePageList\" is null", "download": false}
```

The request is demonstrably accepted: the same endpoint answers `invalid-token`
for the yearly token, `Invalid Year` for `year=2026-2027`, and a clean
`Passbook Not Available` for empty years — so the token and year both validate
before the handler dereferences a null member list. The portal's own "Download as
PDF" button hits the same code path, so the feature is broken for this member in
the browser too. The CLI reports the portal's own message rather than pretending
the download worked; `ledger` needs no PDF and returns the same figures as JSON or
CSV.

**A UAN usually holds more than one member id — one per establishment — and the
ledger is per member.** The home page lists them, and each row carries *two*
identifiers that are not interchangeable:

```text
<span name="mid-mid" data-mid="OPAQUE-PER-SESSION-TOKEN"> GNGGN00000000000000001 </span>
                       ^ only the balance endpoint takes this    ^ change-member-id
```

`epfo-cli ledger` switches to each member in turn (`POST
/passbook/api/ajax/change-member-id` with `mid=<human id>&mnu=passbook`) and reads
that member's years. It has to: the yearly endpoint serves **whichever member is
currently selected** and ignores a member id passed to it — read once and you
report a fraction of the account. On the verified account that is the difference
between one member's ₹28,800 and all four totalling ₹1,72,800 — and the four
closing balances reconcile with the balance page's four rows exactly
(14,400+14,400 / 3,000+3,000 / 45,000+45,000 / 24,000+10,000+14,000 pension),
which is what proves the walk is complete rather than merely plausible.

The years offered differ per member, so `--year` (when omitted) is resolved per
member rather than once for the run — and the page's own selector is **not** the
full list. For one member the page offered FY 2026 and 2025 while
`…/get-member-arch-passbook-data` returned a replacement selector adding **FY
2024**, a year the ledger endpoint serves normally. A year omitted there is a
whole year of contributions silently dropped, so the two sources are unioned.

The ledger rows bind to the transaction header, **not** the summary header that
sits above it (both say "Employee Share"; the summary one is not a row
template). The portal also emits a `colspan`'d disclaimer footer that has the
same cell count as a real row — a month-shaped first cell is what tells them
apart:

```text
wage month txn date       epf wages    employee    employer   pension
May-2026   01-06-2026        30,000    3,600.00    3,600.00      0.00
Jun-2026   17-09-2026        30,000    3,600.00    3,600.00      0.00
Jul-2026   17-09-2026        30,000    3,600.00    3,600.00      0.00
Aug-2026   17-09-2026        30,000    3,600.00    3,600.00      0.00
Total Contributions for the year [ 2026 ]: ₹ 14,400, ₹ 14,400, ₹ 0
```

### Unattended runs

By default the captcha is read automatically and the password comes from the
keychain, so `epfo-cli passbook` completes with **no human input at all** — the
verified run exited 0 and never prompted. To supply a password from a pipe
instead of the keychain:

```bash
printf '%s' "$EPFO_PW" | epfo-cli passbook --password-stdin
```

`--password-stdin` refuses a terminal rather than silently consuming its first
line, and the password never appears in `argv` (where `ps` would show it).

`passbook` and `export` **log in themselves** and prompt for the password. That
is not a convenience: see "The session does not survive" below.

### Storing the password once

```bash
epfo-cli config --uan 100123456789      # remember the UAN (not a secret)
epfo-cli passbook --store-password      # after a successful login
```

After that, `epfo-cli passbook` prompts for nothing but the captcha. Where the
secret lives:

* the **password** goes to the OS keychain (`keyring`, service `epfo-cli`), never
  to a file;
* `~/.epfo-cli/config.json` holds only the UAN and is written `0600`.

A keychain that is present but *unreadable* is reported as a note and the CLI
falls back to prompting. That distinction matters: a stored password silently
failing to load would otherwise look identical to never having saved one.

`pip install -e '.[keychain]'` is required for this — `keyring` **and**
`importlib_metadata`, which `keyring` imports on some interpreters.

## The portal's real API

Verified by logging in and reading the page the portal serves afterwards.

**The password is encoded, not sent.** `login.html` computes

```javascript
var passwordHash = CryptoJS.MD5(pass);
var fhashValue   = hex_sha512("kr9rk" + passwordHash.toString() + "kr9rk");
$("#password1").val(fhashValue);
```

so the wire value is `SHA-512("kr9rk" + MD5(password) + "kr9rk")`, sent in the
field named `password`. The visible `#password` input is never transmitted —
only the hidden `#password1`. `epfo/crypto.py` reproduces this and
`tests/test_crypto.py` pins it to a vector produced by the portal's own JS.

**Login is a four-field POST** to
`/MemberPassBook/passbook/api/ajax/final/checkLogin`:
`username`, `password`, `token`, `answer`. It answers HTTP 200 with JSON even on
failure (`{"success": false, "message": "Invalid Captcha"}`), so the status code
is meaningless — the body is the contract. On success the page redirects to
`home2?token=<object>`.

**The login token rotates.** Every call to `get-new-captcha` returns a new token
in its `object` field, which the page writes straight back into the hidden input.
A retry with a stale token cannot succeed, so `login` re-fetches both.

**Balances come from a different endpoint, with a different token.**

```
POST /passbook/api/ajax/get-member-trans-passbook-data?token=<page token>
body: mid=<member id>
->   {"success": true, "total": ..., "ee": ..., "er": ..., "error": ""}
```

Each AJAX endpoint carries its **own** 128-character token, embedded in the
logged-in page's inline JavaScript. The token returned by the login step is
rejected by the API. `epfo/passbook.py` reads them with
`discover.embedded_tokens`.

**The session does not survive.** This is the single most important discovery
here, and it is why the architecture is what it is:

```
GET /home2?token=<token>   with saved cookies   ->  302
                                                  location: .../login?error=session-exception
```

EPFO rejects a session reused from another process. It also redirects to plain
`http://`, which a naive client follows into a request that never answers — that
redirect is what made `passbook` appear to hang forever. So:

* `passbook` and `export` log in and read in **one process**;
* `EPFOSession` does **not follow redirects** (`NoRedirect`), and raises
  `SessionExpired` / `TokenExpired` from the `Location` header instead of
  silently returning a login page that parses as "no data".

**Redirects are reported, not followed.** `login` alone is therefore of limited
use; it exists to validate credentials and surfaces a note saying so.

**A lapsed session is re-authenticated automatically.** `passbook` and `export`
retry once on a `session-exception`, because a balance read is idempotent —
there is no write to duplicate — so repeating it is safe. A *second* lapse is
reported as a failure rather than retried in a loop.

**The session does not survive, so nothing tries to make it.** There is no
cookie-restore path in the credentialed commands. A stale cookie jar on disk
cannot produce a subtle wrong answer, because it is never consulted.

## Reading the captcha automatically

Automatic reading is the default; `--no-auto-captcha` asks a person instead.
The design is OCR-first with the human as the guarantee:

* OCR runs first, and the answer it returns must pass a plausibility gate
  (alphanumeric, 4-8 characters — a stray glyph or a line of prose is rejected
  rather than submitted);
* a rejected reading, or OCR being unavailable, falls back to prompting, so the
  command still completes;
* `--attempts` (default **6**, not 3) bounds the retries, because automatic
  reading is imperfect and the portal rotates its token on every failed attempt.

Two readers are wired up and **ddddocr is primary**, because the gap was
measured on eight live captchas: they agreed on four and disagreed on four, and
inspecting every disputed image showed **ddddocr was right in all four**
(8/8 vs tesseract's 4/8). Every disagreement was an `O`/`9` or `T`/`7`
confusion, which is exactly the kind of systematic error a single reader never
recovers from. The CLI alternates readers across login attempts, so a second
attempt is a different bet rather than a repeat.

Two further findings are load-bearing and were measured, not assumed:

1. **`--psm 8`, not `--psm 7`.** Across six live captchas, `--psm 7` returned an
   *empty* string on four of them while `--psm 8` read all six. An earlier
   version hardcoded `--psm 7`, which is precisely why automatic reading failed.
   Both are tried, 8 first. (This applies to the tesseract backend;
   `ddddocr` needs none of it.)
2. **Tesseract binary, not `pytesseract`.** On this machine the
   `pytesseract`/`Pillow` route resolved to a Pillow built for a different
   interpreter (`ImportError: cannot import name '_imaging' from 'PIL'`) and
   silently disabled OCR. `epfo.captcha` execs the `tesseract` binary directly
   and needs no Python packages. Install the system tool:
   `brew install tesseract`.

Upscaling uses `sips` (macOS) or ImageMagick when present and is skipped when
not. Measured across 400/800/1400 px, tesseract's readings were identical, so no
accuracy is lost without a scaler.

## Two quirks worth knowing

* A 200 with a failure body. Both AJAX endpoints do this, so every caller reads
  `success` from JSON rather than trusting the status code.
* The portal's JS writes one URL **with** a `/MemberPassBook` prefix and others
  without. A URL copied from the address bar used to become
  `/MemberPassBook/MemberPassBook/...` and 404. `absolute()` and the token key
  normalisation both collapse it.

## Discovery

```bash
epfo-cli discover --endpoint 127.0.0.1:9333 --target <id> \
                  --origins https://passbook.epfindia.gov.in --out endpoints.json
```

Reads the scripts the page actually loads (via a stdlib-only Chrome DevTools
Protocol client in `epfo/cdp.py`) and extracts endpoints plus the parameters each
call sends. It independently recovers the four-field login contract from the live
page, which is how the extraction was validated. It does not call privileged
endpoints.

## Tests

```bash
python -m pytest -q     # 121 tests
```

The discovery and session tests run against the portal's **real** login page,
trimmed to its structural parts. The passbook-client tests use response shapes
copied from live captures. `tests/fixtures_passbook.html` is **synthetic** and is
labelled as such.

## Honest gaps

1. **Per-month transaction rows are not retrievable.** Balances are the portal's
   own figures, but the endpoint that serves the month-by-month ledger was not
   identified. `epfo/models.parse_passbook` — the parser for such a page — is
   therefore **not used by the CLI**, is calibrated against a synthetic fixture,
   and has never seen a real page. Treat it as unverified.
2. **OTP is not submitted.** On an unrecognised device the portal returns an OTP
   panel instead of a session. The client detects this and exits 4 rather than
   pretending it logged in.
3. **OCR accuracy is bounded.** Tesseract is right often enough to be useful
   and wrong often enough to matter — one live unattended run needed two
   captcha attempts before it succeeded. This is why the retry bound is
   generous, the reading is never trusted silently, and the human path remains.
4. **Login is short-lived by design.** Every credentialed command reads a fresh
   captcha. That is the portal's behaviour, not a limitation this client can
   remove.
