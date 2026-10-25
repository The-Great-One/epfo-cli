<div align="center">

<img src="icon.png" alt="epfo-cli logo" width="120" height="120" />

# epfo-cli

**Read-only CLI for the EPFO member passbook portal (`passbook.epfindia.gov.in`).**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![GitHub issues](https://img.shields.io/github/issues/The-Great-One/epfo-cli)](https://github.com/The-Great-One/epfo-cli/issues)
[![GitHub pull requests](https://img.shields.io/github/issues-pr/The-Great-One/epfo-cli)](https://github.com/The-Great-One/epfo-cli/pulls)
[![GitHub stars](https://img.shields.io/github/stars/The-Great-One/epfo-cli?style=social)](https://github.com/The-Great-One/epfo-cli/stargazers)

[Report Bug](https://github.com/The-Great-One/epfo-cli/issues/new?template=bug_report.md) · [Request Feature](https://github.com/The-Great-One/epfo-cli/issues/new?template=feature_request.md)

</div>

---

## Table of Contents

- [About](#about)
- [Features](#features)
- [Getting Started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Installation](#installation)
- [Usage](#usage)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)
- [Contact](#contact)
- [Acknowledgments](#acknowledgments)

---

## About

`epfo-cli` is a read-only command-line client for the EPFO member passbook
portal — a legacy JSP app with no API, so the client reproduces the portal's
own front-end protocol. Every claim was verified against the live site with a
real account; gaps are stated, not hidden.

It logs in for you (the captcha is read automatically) and reads your
**balances, the month-by-month ledger, employment history, profile and KYC**.
It reads only — it changes nothing.

## Features

- ✅ Balances for every member account under your UAN
- ✅ Month-by-month ledger across all financial years
- ✅ Employment / service history per establishment
- ✅ Profile and KYC fields
- ✅ Automatic captcha reading (ddddocr → tesseract)
- ✅ OS keychain password storage (never on disk, never in argv)
- ✅ Unattended runs (exit 0, no prompt)
- ✅ `--store` change detection with exit-code contract
- ✅ Read claims surface via real browser (CDP)
- 🚧 PDF generation (broken portal-side; use `ledger`)

## Getting Started

### Prerequisites

- Python 3.10+
- macOS/Linux
- `tesseract` (fallback captcha reader)

```bash
brew install tesseract   # macOS
```

### Installation

```bash
# 1. Clone the repository
git clone https://github.com/The-Great-One/epfo-cli.git

# 2. Navigate into the project directory
cd epfo-cli

# 3. Create a virtual environment
python3 -m venv .venv

# 4. Install dependencies
.venv/bin/python -m pip install -e '.[keychain,ddddocr,browser]'

# 5. Copy the environment variables template (optional)
cp .env.example .env
```

> Install into the venv, never a shared interpreter. Strip `PYTHONPATH` if it
> leaks in your shell (`env -u PYTHONPATH …`).

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
default 6); `--password-stdin` for unattended runs.

### Unattended runs

The captcha is read automatically and the password comes from the keychain, so
`epfo-cli passbook` completes with no human input at all (verified: exit 0, no
prompt). To pipe the password instead:

```bash
printf '%s' "$EPFO_PW" | epfo-cli passbook --password-stdin
```

### Change detection (`--store`)

`--store` records a read locally and reports what changed since last time.
The exit code is the contract: `0` = nothing changed, `1` = something did —
so a cron job alerts without parsing output.

## Roadmap

- [x] v0.1 – Passbook, ledger, service history, profile
- [x] v0.1 – Automatic captcha reading
- [x] v0.1 – Unified Portal claims reader
- [ ] v0.2 – Packaged release / PyPI
- [ ] v0.2 – CI on multiple Python versions

See the open issues for the full list of proposed features and known bugs.

## Contributing

Contributions are what make open source such a great place to learn and build.
Any contribution you make is greatly appreciated.

Please read [CONTRIBUTING.md](CONTRIBUTING.md) and follow our
[Code of Conduct](CODE_OF_CONDUCT.md) before submitting a pull request.

## License

Distributed under the MIT License. See [LICENSE](LICENSE) for more information.

## Contact

Sahil Goel – [@The-Great-One](https://github.com/The-Great-One) –
sahilgoel187@gmail.com

Project link: [https://github.com/The-Great-One/epfo-cli](https://github.com/The-Great-One/epfo-cli)

## Acknowledgments

- [EPFO Member Passbook Portal](https://passbook.epfindia.gov.in)
- [Contributor Covenant](https://www.contributor-covenant.org)
- [Keep a Changelog](https://keepachangelog.com)
- [Img Shields](https://shields.io)
