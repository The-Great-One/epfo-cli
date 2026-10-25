# epfo-cli

Read-only CLI for the EPFO member passbook portal (`passbook.epfindia.gov.in`).

![icon](icon.png)

## What it does

Logs in for you (captcha read automatically) and reads **balances, the month-by-month ledger, employment history, profile and KYC**. Read-only — changes nothing.

## Commands

```text
epfo-cli doctor                       # connectivity + optional deps
epfo-cli config --uan 100123456789    # remember your UAN
epfo-cli passbook --store-password    # store password in keychain
epfo-cli passbook                     # balances for every member account
epfo-cli ledger --out ledger.csv      # month-by-month ledger, all members/years
epfo-cli ledger --member 070329       # one member account
epfo-cli ledger --year 2025           # a specific financial year
epfo-cli service-history              # employment history per establishment
epfo-cli profile                      # profile + KYC fields
epfo-cli status                       # stored state, last read (no login)
epfo-cli unified --out claims.json    # read claims surface (browser + OTP)
```

## Quirks

- Password is `SHA-512("kr9rk" + MD5(password) + "kr9rk")`; never sent in clear.
- Session does not survive a process (`session-exception`); every credentialed command logs in itself.
- `pdf` is broken portal-side (NullPointerException); use `ledger`.
- `unified` drives real Chrome over CDP, is read-only, refuses unattended.
- Captcha read automatically (ddddocr → tesseract); alternates across attempts.

## Install

```bash
git clone https://github.com/The-Great-One/epfo-cli && cd epfo-cli
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[keychain,ddddocr,browser]'
brew install tesseract
```

Password lives in the OS keychain only; never on disk, never in `argv`.
