# Scoping: the Unified Portal (`unifiedportal-mem.epfindia.gov.in`)

**Status: the whole login + forced password change was completed end-to-end in a
real browser.** credentials → OTP → change-password (Aadhaar OTP) → *"Password has
been changed successfully."* What it taught: the OTP step **cannot** be completed by
a hand-rolled POST, the change-password step is driven by a JS `confirm()` that a
browser driver silently swallows unless dialogs are accepted, and the Aadhaar OTP is
mandatory (there is no old/new-only path). Full detail in "Verified live" below.

Recorded 2026-10-06, on the live site.

## Why this is a separate project

`epfo-cli` reads the **member passbook** portal (`passbook.epfindia.gov.in`). Claims,
transfers, KYC edits and the rest live on the **Unified Portal** — a different host,
a different application, a different auth scheme. Nothing from `epfo-cli` transfers
except the general approach.

## What the claim button actually does

On the passbook portal, `/claims` is disabled server-side: no form, no inputs, no
endpoint, and the text *"This module is temporarily unavailable"* (build
`Ver - 1.2.25`). The real claim flow is here, on the Unified Portal.

## The login page

`GET https://unifiedportal-mem.epfindia.gov.in/memberinterface/` → HTTP 200,
`Member Home`, 52,665 bytes. One form:

```html
<form id="AuthenticationForm" name="AuthenticationForm"
      action="/memberinterface/" method="post" autocomplete="off">
```

Fields:

| field | visible | notes |
|---|---|---|
| `userName` | yes | **decoy** — filled with `"r" × len(username)` |
| `hidUserName` | hidden | the real value: AES-GCM of the username |
| `password` | yes | **decoy** — filled with `"r" × len(password)` |
| `hidPassword` | hidden | the real value: the challenge hash |
| `challenge` | hidden | the challenge digits, comma-separated |
| `encrChallenge` | hidden | — |
| `isConcurrent` | hidden | concurrent-session flag |
| `_HDIV_STATE_` | hidden | HDIV request-integrity token |

**There is no captcha on this login.** That is the headline finding: the auth is
*easier* to automate than the passbook portal, which needs OCR.

## The scheme, read from the page's own JavaScript

From `scripts/security/authentication.js`:

```js
var a = md5(e.password),
    r = $("#lblChallange").html(),
    a = hex_sha512("kr9rk" + a.toString() + "kr9rk"),
    a = hex_sha512(r + a);
$("#hidPassword").val(a);              // the real submitted hash
```

so

```text
hidPassword = SHA-512( challenge + SHA-512( "kr9rk" + MD5(password) + "kr9rk" ) )
```

Note the **same `kr9rk` salt** as the passbook portal, with an extra challenge
round — so the existing `epfo/crypto.py` is a working starting point.

**Verified**: the page's shipped `crypto-utils.js` was run under Node and its
`md5`/`hex_sha512` agree byte-for-byte with Python's `hashlib` on known vectors, and
both produce the same `hidPassword` for the same inputs. So the password half needs
no guessing:

```text
md5("abc")        = 900150983cd24fb0d6963f7d28e17f72        (JS == Python)
hex_sha512("abc") = ddaf35a193617abacc417349ae20413112e6... (JS == Python)
```

Only the AES-GCM username half is still unverified (Node's WebCrypto is not
Python's), and that one is standard.

The username is encrypted separately:

```js
function encrypt(input) {
    var keyBytes = base64ToUint8Array(dataId);
    var iv = crypto.getRandomValues(new Uint8Array(12));   // 12-byte nonce
    ... crypto.subtle.encrypt({ name: 'AES-GCM', iv: iv }, key, encoded)
    $("#hidUserName").val(uint8ArrayToBase64(combined));   // iv ‖ ciphertext
}
```

so

```text
hidUserName = base64( iv[12] ‖ AES-GCM( key = base64decode(dataId), iv, username ) )
```

`dataId` is published inline in the page and **rotates per page load** (observed
`AMdKfKa6IHc9VUIO3MArog==`, a 16-byte key). The challenge also rotates per session
(observed `lblChallange = 675899237236867941`; the `challenge` input carried the same
digits comma-separated, `6,7,5,8,9,9,…`).

`scripts/common/crypto-utils.js` (16,403 bytes) is a dependency-free MD5 + SHA-512 +
WebCrypto-AES-GCM bundle, so the algorithm is fully specified in shipped code — no
guesswork.

## Verified: the login works, and it is OTP-gated

A scripted login was run once, with the real credentials. It **succeeds** — and the
result answers the question that decided this project.

```text
POST /memberinterface/            → 303 See Other
Location: /memberinterface/ekyc/otpLogin?_HDIV_STATE_=18-0-E37AFF…
```

On a correct `hidUserName` + `hidPassword` the portal does **not** land on a
dashboard. It redirects to an **eKYC OTP** step. So for this account **a logged-in
session is not reachable without an OTP** — the gate is at the *door*, not at the
claim form.

An earlier attempt, with the form fields as first guessed, answered
`302 → /error.jsp` instead. That is a useful pair of outcomes: the portal tells
"wrong credentials" (error.jsp) apart from "correct credentials, now prove OTP"
(ekyc/otpLogin), which is exactly how the two halves were told apart here.

**Consequence for the product idea**: a CLI cannot raise a claim unattended. Any
write against this portal needs a human OTP first, so the honest ceiling is a tool
that *prepares* a claim and stops for the OTP — and since the OTP is demanded before
you even see the dashboard, it may be that the browser is simply the right tool.

### Three details that would waste a session if rediscovered

- **`lblChallange` is not the submitted `challenge`.** The label holds plain digits
  (`-338163059477565763`); the hidden `challenge` input holds the *same* value
  **comma-separated** (`-,3,3,8,1,6,3,0,…`). The hash uses the label; the form
  submits the comma-separated field. Using the wrong one answered `error.jsp` while
  looking perfectly correct.
- **The visible `userName` is submitted empty**, not filled. The page sets it to
  `"r" × len`, then an async callback overwrites it with `""` while `hidUserName`
  carries the AES value. Only `password` keeps its `"r" × len` decoy.
- **The `Location` is plain `http://`,** which a naive follower hangs on forever —
  the same trap as the passbook portal. Do not follow it; read the header and force
  `https://`.

## Reproducibility

Everything needed is standard-library Python plus one AES implementation:

```text
hashlib.md5, hashlib.sha512          → the password hash
cryptography (or pycryptodome)       → AES-GCM for the username
the page's dataId + challenge        → parsed from the login page per session
```

No OCR, no captcha, no browser. The remaining work is the **post-login** half:
whether the app issues `session-exception`-style token churn, how OTP gates KYC/claim
submission, and whether it enforces one-session-per-user.

## Verified live: the login completes, and the account is blocked by a forced
## password change

A real Chrome (Playwright, `channel="chrome"`, headed) drove the whole flow:
type UAN + password into the **visible** fields, click **Sign in** and let the
page's own `authentication.js` do the hashing/AES, then type the OTP into the OTP
page and let its own `OtpLogin.js` encrypt and submit it.

```text
Member Home → click Sign in            → 303 → /memberinterface/ekyc/otpLogin
enter OTP (6 digits)                    → POST /memberinterface/ekyc/verifyMobile
                                        → /memberinterface/no_auth/changePass/changePassword?firstLogin=true
                                          title: "EPFO: Password Change"
                                          "Your Password is Expired. Kindly update your password."
```

So the OTP login **does** succeed — but the landing page is a **forced password
change**, not a dashboard. This account (`…8727`) cannot reach the member dashboard
until its password is changed.

### The OTP step rejects a hand-rolled POST; drive the page instead

The same OTP was tried three ways, on three different codes, and **only the browser
worked**:

| attempt | how the OTP was sent | result |
|---|---|---|
| 1 | plain 6 digits in `otpEnt.otp` | `302 → /error.jsp` |
| 2 | `base64(iv[12] ‖ AES-GCM(dataId, iv, otp))`, my own POST | `302 → /error.jsp` |
| 3 | typed into `#otp`, page's own `encryptOTP()` + click | **OK → password-change page** |

The encryption was not the difference — Python's AES-GCM matches the page's
`encryptAesGcm` byte-for-byte. What the browser added was the **submit-time request
the page actually makes**: the `otpEnt.otpId` / `otpEnt.userId` / `user.hidPassword`
/ `user.encrChallenge` hidden values, the exact `_HDIV_STATE_` for the submit, and
the page's own session cookies, all in flight together. Replaying those from a
script is possible in principle but was never made to work. **Conclusion: for the
OTP step, use a browser driver; do not re-derive the submit.**

`epfo-cli` has no browser driver (deliberate — the passbook portal did not need
one). Adding Playwright as an optional extra is the prerequisite for automating
this step at all.

### Two more traps on this portal

- **A page-load notice modal blocks every click.** `mainHomePageAlertModal`
  ("Dear EPF Members!!") auto-opens on `Member Home`; until it is dismissed
  (`#btnCloseModal`, or Escape) every `click()` is intercepted by the overlay. The
  concurrency modal (`#concurrenttSessionAlert` with `#loginHereButton`) is a
  *separate*, conditional one and is also always in the DOM.
- **Headless Chromium is WAF-blocked** (`The URL you requested has been blocked`).
  Real Chrome (`channel="chrome"`) passes; a plain `urllib` fetch with a normal
  User-Agent also passes. It is the headless fingerprint, not the IP.

### The forced password change needs an Aadhaar OTP — and it was completed

The change-password page shows Old / New / Confirm, a consent checkbox, and a single
action button **Get AADHAAR OTP** — there is **no old/new-only submit**. The sequence
is:

```text
fill old / new / confirm
tick the Aadhaar consent checkbox (#consentStatus)
click  Get AADHAAR OTP        → JS confirm("Are you sure to change password ?")
                              → POST → page swaps to an #aadhaarOtp field
                              → also fires a real OTP to the Aadhaar-linked mobile
enter the Aadhaar OTP, click  Change Password (#updatePassBtn)
                              → JS confirm(...) again
                              → "Password has been changed successfully."
```

**The trap that costs an attempt:** both buttons go through a native
`window.confirm("Are you sure to change password ? ")`. Playwright's default is to
**dismiss** dialogs, so the submit is swallowed and the page just sits there looking
untouched. Register `page.on("dialog", lambda d: d.accept())` or the change never
fires. (This is why three earlier clicks did nothing.)

The new-password rules, read off the page's own `#regex` value:

```text
regex = (?=^.{8,20}$)(?=(.*\d){2,})(?=(.*[A-Za-z]){4,})(?=.*[A-Z])(?=.*[a-z])(?=.*[!@#$%^&*?])(?!.*\s)
        → 8–20 chars, ≥2 digits, ≥1 upper, ≥1 lower, ≥1 symbol from !@#$%^&*?
```

**Done on 2026-10-06** with the account holder present (Aadhaar OTP in hand). The
portal itself says login now works with the new password:

> *"Password has been changed successfully. Kindly login with the new password."*

**Knock-on effect:** the **same UAN password is shared with the passbook portal**,
so `epfo-cli`'s keychain entry had to be updated on the same day. Any future
password change on this portal will silently break `epfo-cli` until the keychain is
refreshed — check `epfo-cli passbook` after any password change here.

## What this means for the product

- **A CLI cannot raise a claim unattended.** The OTP gate is at login, and the
  login now also demands a password reset (Aadhaar OTP). The honest ceiling is a
  tool that *prepares* a claim and stops for the human.
- **The realistic first client is a browser-driven one** (`prepare-a-claim`, fill
  and validate the form, stop at the OTP), not a headless HTTP client.
- **Before any of that**, the account must first clear the forced password change —
  a one-time interactive step with the Aadhaar OTP.

## Next step

1. ~~Change the EPFO password~~ **Done 2026-10-06.**
2. **Now do a browser-driven login with the new password** and dump the real
   dashboard/nav, to see which of claims / transfers / KYC is actually reachable for
   this UAN. The forced-reset page should no longer appear (firstLogin is cleared).
3. Only then decide whether a claim-preparation client is worth building — and it
   will be browser-driven (Playwright), not a headless HTTP client.
