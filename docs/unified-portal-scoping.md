# Scoping: the Unified Portal (`unifiedportal-mem.epfindia.gov.in`)

Reconnaissance only — **no credentialed login was attempted**, so everything below
is from public page/script fetches. The post-login flow (and whether claims demand an
OTP) is deliberately unverified.

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

## Unknowns, and the one that decides the project

1. **Does a claim submission require an OTP?** This is the gate. Login has no
   captcha, but a claim is a *write* against a retirement account and is very likely
   to require OTP/Aadhaar verification. If it does, the honest answer may be that a
   CLI can *prepare* a claim but a human must complete the OTP step — which is still
   useful (form-filling and pre-validation), but a different product from
   "raise a claim from the CLI".
2. **What the post-login token/session model is** — whether it shares the passbook
   portal's hostile one-shot tokens or is a normal session.
3. **Whether the concurrency guard blocks automation** (`isConcurrent`, the
   `concurrentSession()` handler).

These need a **credentialed login**, which I have not done: it would send an OTP to
the account holder's phone and risks the account's session state. That step is the
account holder's call, not something to run unattended.

## Recommended next step

A second scoping session, with the account holder present and the OTP phone in hand,
to log in once through a script and dump the post-login page and nav — the same move
that unlocked `epfo-cli`. Only after that is it worth writing any parser.

Do **not** start by writing a claim-submitting client. Establish first whether the
write path is reachable without an interactive OTP; if it is not, say so plainly and
scope what *is* reachable.
