# The Unified Portal claims surface, mapped live

Recorded 2026-10-06, on the live site, the operator's own UAN, driven with a real Chrome
(Playwright `channel="chrome"`, REA capture). Everything below is **observed**
unless marked *(inferred)*.

## How to reach it

1. `GET https://unifiedportal-mem.epfindia.gov.in/memberinterface/` — the login page.
   An alert modal `#mainHomePageAlertModal` auto-opens and intercepts clicks until
   dismissed (`#btnCloseModal`).
2. Fill the **visible** `#userName` + `#password`, click **Sign in**. The page's own
   `authentication.js` builds the hidden twins and POSTs the form.
3. `303 → /memberinterface/ekyc/otpLogin` — a 6-digit OTP is sent to the
   Aadhaar-linked mobile (`XXXXXX9105`). The page shows the **OTP-ID**.
4. Enter the OTP into `#otp` and submit `#submitButton` (the page's own
   `OtpLogin.js` encrypts and sends it to `POST /memberinterface/ekyc/verifyMobile`).
5. Landing page = the member dashboard (title `EPFO: Home`).

**Session fragility — three traps that cost several logins:**

- **`goto()` to any in-app URL kills the session** (`Session Error` /
  `error.jsp`). The page's own JS `click()` on a nav link is the *only* navigation
  that works. Drive by clicking; never re-navigate by URL.
- **Nav tokens are single-use.** A harvested `?_HDIV_STATE_=...` is dead on the next
  request. Always read the link from the page you are currently on and click it.
- **A second login while one is still live raises the concurrent-session alert**
  (`#concurrenttSessionAlert` + a visible `#loginHereButton`) instead of the OTP
  page. It is not an error: clicking that button is the portal's own "take over
  this session" action, and the OTP page then appears. A client that treats it as
  a failed login will loop. Close it with a real click, then continue.

## The verified CLI boundary (why claims cannot be a plain HTTP client)

The login POST was reproduced in Python over `urllib`: `hidUserName =
base64(iv[12] ‖ AES-GCM(base64decode(dataId), iv, UAN))`, `hidPassword =
SHA-512(label + SHA-512("kr9rk" + MD5(pw) + "kr9rk"))`. The password hash was
**verified byte-for-byte identical to the page's own** (`MATCH: True`).

It still failed — `302 → error.jsp` — even when the **exact captured POST body and
the browser's cookies** were replayed verbatim over plain HTTP.

*(inferred)* The gate is a browser-level signal, not the crypto: the flow carries a
`_sec_sess_id` "security session" cookie and a `cookiesession1` WAF cookie, and the
server distinguishes the real browser (TLS/JS fingerprint) from a scripted client.
**Consequence: the Unified Portal cannot be logged into by a pure CLI.** Any client
must be browser-driven (Playwright, `channel="chrome"` — headless Chromium is
WAF-blocked). Combined with the mandatory OTP, there is no unattended path.

A second correction to the earlier scoping note: the page submits the visible
`userName` as **`"r" × len(UAN)`** (the decoy), *not* empty. Only the visible
`password` decoy and the hidden `hidUserName`/`hidPassword` matter, but the decoy
username must not be blanked.

## The full nav (every route)

| Menu | Page | Endpoint |
|---|---|---|
| View | Profile | `/memberinterface/member/profile/personalDetails` |
| View | UAN Card | `/memberinterface/member/profile/showUANCard` |
| View | Passbook Lite | `/memberinterface/memberPassbook/home` |
| View | Passbook | `https://passbook.epfindia.gov.in/...` (separate host) |
| Manage | Joint Declaration | `/memberinterface/jointDeclaration/loadMemberDetails` |
| Manage | Contact Details | `/memberinterface/member/profile/changeContactDetails` |
| Manage | **KYC** | `/memberinterface/kyc/viewKYCRegistrationForm` |
| Manage | e-Nomination | `/memberinterface/eNomination/geteNominationPage` |
| Manage | Mark Exit | `/memberinterface/exit/employment` |
| Account | Change Password | `/memberinterface/account/changePass/changePassword` |
| Online | Scheme Certificate Surrender | `/memberinterface/schemecertificate/dashboard` |
| Online | Member Service History | `/memberinterface/memberServiceHistoryNew/loadMemberServiceHistory` |
| Online | **Claim (Form-31,19 &10C)** | `/memberinterface/cenonline/claim/getReceipt` |
| Online | Request for transfer of account | `/memberinterface/cenOtcpMemberInterface/loadTxClaimHome` |
| Online | Track Claim Status | `/memberinterface/cenonline/claim/onlineClaimStatus` |
| Online | Track Claim Status (OLD) | `/memberinterface/online/claim/onlineClaimStatus` |
| Online | Claim (Form 10-D) | `/memberinterface/form10D/claimstatus/getMemberDetails` |
| PMVBRY | Dashboard / FLC | `/memberinterface/eli/...` |

## The claim flow — and its blocker

`/cenonline/claim/getReceipt` ("Collect Online Claim Receipt") opens a
**Certificate of Undertaking** with `#yes` ("I agree to the terms and conditions")
and `#no` buttons, then the claim-type form. **On this account it never renders:**
the page shows only

> *1. PLEASE UPDATE YOUR LATEST BANK ACCOUNT NUMBER AND VALID IFSC DETAILS (USING
> MENU MANAGE >> KYC). 2. AVAILABLE IFSC (INVALIDIFSC) IS INVALID. PLEASE UPDATE
> VALID IFSC WITH LATEST BANK ACCOUNT NUMBER IN KYC DETAILS.*

So a claim **cannot be filed** while KYC holds no valid bank account + IFSC. The
`#yes`/`#no` buttons exist in the DOM but have zero size — the undertaking is
gated behind the KYC check. Fixing KYC is a **write** operation requiring an Aadhaar
OTP (`Note : An OTP will be sent to your AADHAAR linked mobile while submitting KYC`).

## KYC page (`/kyc/viewKYCRegistrationForm`)

- `Add KYC` → document types: Bank, PAN, Passport.
- **Currently Active KYC**: one row — `PAN`, status `Approved` (DSC-signed,
  employer (name withheld)). **No bank document.**
- **KYC Pending for Approval**: empty.
- Adding Bank KYC triggers an Aadhaar-linked OTP on submit.

## Member Service History (`/memberServiceHistoryNew/loadMemberServiceHistory`)

Columns: UAN, Member ID, Date of Joining, Date of Exit, Reason for Leaving,
PF Last Transferred into MID, PF Status, PF Balance (EPF/EPS/FPS), Service Status.
One establishment row on this UAN (member id `<member-id>`).

## Track Claim Status (`/cenonline/claim/onlineClaimStatus`)

Two sections: **Claim Status** (Final Settlement, Advances, Withdrawal Benefit,
Scheme Certificate) → *"Claim Record Not Found"*; **Transfer Claim Status** →
*"No Claim Details Found"*.

## Request for Transfer of Account (`/cenOtcpMemberInterface/loadTxClaimHome`)

Shows Personal Information (name, masked mobile/email, bank a/c, IFSC, masked
Aadhaar) and the present establishment. Transfer is a form-driven flow.

## What this means for a CLI

- **Read (passbook host):** fully automatable headlessly — already in `epfo-cli`.
- **Unified Portal (claims, KYC, transfers, nomination):** **browser-only** (WAF/TLS
  fingerprint) **and** OTP-gated at login. The honest ceiling is a browser-backed
  assistant that logs in, pauses for the human OTP, and can *read* the mapped pages
  — it cannot submit a claim unattended, and a claim is blocked by KYC regardless.
