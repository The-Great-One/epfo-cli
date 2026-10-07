# epfo-cli

A read-only CLI for the EPFO member passbook portal
(`passbook.epfindia.gov.in`) — a legacy JSP app with no API, so the client
reproduces the portal's own front-end protocol. Every claim below was verified
against the live site with a real account; the gaps are stated, not hidden.

It logs in for you (the captcha is read automatically) and reads your
**balances, the month-by-month ledger, employment history, profile and KYC**.
It reads only — it changes nothing.

## Is it production-ready?

**Yes, for its intended use: a personal, read-only CLI that runs unattended on
your own account.** No, for anything beyond that.

- **Reliable where it matters.** `passbook`, `ledger`, `service-history` and
  `profile` run end-to-end with **no human input** (exit 0) and are covered by
  184 passing tests. `ledger` walks every member account under your UAN and every
  financial year the portal serves, then reconciles the totals.
- **Safe by construction.** Read-only; one process per run; the password lives
  only in the OS keychain (never on disk, never in `argv`); no account data is
  checked into the repo.
- **Not a service.** It is a single-user CLI, not a hosted or multi-user API.
  The session is per-process and the portal rate-limits logins — do not put it
  behind a shared server.
- **Not packaged for distribution.** `0.1.0`, no CI, no tagged release, no PyPI.
  Install it from source into its own venv.

### Honest limits (portal-side, not fixable here)

- **Claims cannot be filed by any client.** The passbook portal's claims module
  is switched off, and the real claim flow (Unified Portal) is browser-only and
  OTP-gated. `epfo-cli unified` *reads* that surface; it cannot submit.
- **`pdf` is broken on the portal's side** — it throws a NullPointerException for
  any year that actually has data. `ledger` returns the same figures.
- **OTP devices are unsupported on the passbook portal.** If the portal returns
  an OTP panel, the client detects it and exits 4 rather than pretend to log in.

## Install

Requires Python 3.10+.

```bash
git clone https://github.com/The-Great-One/epfo-cli && cd epfo-cli
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[keychain,ddddocr,browser]'
brew install tesseract                     # fallback captcha reader (macOS)
```

`certifi` is the only mandatory dependency. The extras are independent and
optional — `keychain` stores the password in the OS keychain, `ddddocr` is the
primary captcha reader, `browser` adds Playwright for the `unified` command.

> **Install into the venv, never a shared interpreter.** `.[ddddocr]` pulls in
> ~200 MB of `numpy`/`onnxruntime`/`opencv`. If your shell exports `PYTHONPATH`,
> strip it for these commands (`env -u PYTHONPATH …`) — a leaked `PYTHONPATH`
> makes `pip` believe packages are already installed and skip them.

Then use the venv's own command: `.venv/bin/epfo-cli passbook`

## Usage

```bash
epfo-cli doctor                       # connectivity + optional deps
epfo-cli config --uan 100123456789    # remember your UAN (not a secret)
epfo-cli passbook --store-password    # store the password in the keychain
epfo-cli passbook                     # balances for every member account
epfo-cli ledger --out ledger.csv      # month-by-month ledger, all members/years
epfo-cli ledger --member 070329       # one member account (id or trailing digits)
epfo-cli ledger --year 2025           # a specific financial year
epfo-cli service-history              # employment history per establishment
epfo-cli profile                      # profile + KYC fields
epfo-cli status                       # what's stored, last read (no login)
epfo-cli unified --out claims.json    # read the claims surface (browser + OTP)
```

Common flags: `--json` on any read command; `--attempts N` (captcha retries,
default **6**); `--password-stdin` for unattended runs.

### Unattended runs

The captcha is read automatically by default and the password comes from the
keychain, so `epfo-cli passbook` completes with **no human input at all**
(verified: exit 0, no prompt). To pipe the password instead:

```bash
printf '%s' "$EPFO_PW" | epfo-cli passbook --password-stdin
```

`--password-stdin` refuses a terminal, and the password never appears in `argv`
(where `ps` would show it).

### Watching it change (`--store`)

`--store` records a read locally and reports **what changed since last time**.
**The exit code is the contract:** `0` = nothing changed, `1` = something did —
so a cron job alerts without parsing output. State lives in
`~/.epfo-cli/ledger.sqlite` (`0600`); `status` reports it without logging in.

### Reading the claims surface (`unified`)

`unified` drives a real Chrome (over CDP) to the separate **Unified Portal**,
pauses for your OTP, and reads the claim / KYC / transfer pages it can reach. It
is read-only, requires the `.[browser]` extra, and refuses to run unattended.
The full page map and why a plain HTTP client cannot do this are in
[`docs/unified-portal-claims.md`](docs/unified-portal-claims.md).

## How it talks to the portal

Two facts shape the whole design:

- **The password is encoded, not sent.** The wire value is
  `SHA-512("kr9rk" + MD5(password) + "kr9rk")` in a hidden field; the visible
  input is never transmitted. `epfo/crypto.py` reproduces it and a test pins it
  to a vector from the portal's own JavaScript.
- **The session does not survive a process.** EPFO answers a reused cookie with
  `session-exception`, so every credentialed command logs in and reads in one
  process, does not follow redirects, and never restores a cookie jar.

The endpoints, per-page tokens, parsing quirks and captcha benchmarks are in
[`docs/portal-notes.md`](docs/portal-notes.md).

## Scope

A **read-only** client for **your own** account. It changes nothing and does not
touch the separate Unified Portal beyond reading its pages. No account data is
checked in — every UAN, member id, employer and figure in the tests and docs is
a placeholder. Automating a login may be restricted by the portal's terms; use it
on accounts you own.

## Tests

```bash
env -u PYTHONPATH .venv/bin/python -m pytest -q     # 184 tests
```
