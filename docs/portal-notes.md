# Portal notes

Field notes on `passbook.epfindia.gov.in`, recorded so the quirks are not
rediscovered. Everything here was observed directly, on 2026-10-06.

## Login page: `GET /MemberPassBook/login`

Returns a JSP page carrying:

- a hidden `login-token` input (server-generated, session-bound);
- `#username` (UAN) and `#password` (visible, never submitted);
- `#password1` (hidden, receives the encoded password);
- `#captcha_id`, an `<img>` whose `src` is `data:image/jpg;base64,...`;
- one large inline `<script>` holding the whole login flow.

A `Content-Security-Policy` with a per-response `nonce` and `strict-dynamic` is
present, and `default-src 'self'`.

### The submit handler

```javascript
var data = {};
data["username"] = $("#username").val();
data["password"] = $("#password1").val();
data["token"]    = $('input[name="login-token"]').val();
data["answer"]   = $("#captcha").val();

$.ajax({
    type: "POST",
    contentType: "application/x-www-form-urlencoded; charset=UTF-8",
    url: '/MemberPassBook/passbook/api/ajax/final/checkLogin',
    data: data,
    dataType: 'json',
    timeout: 1000000,
    ...
```

Note the ordering: the `data[...]` assignments sit **above** the `url:` literal,
which is why a forward-only parameter scan finds nothing. This is the single
easiest way to mis-reverse-engineer this page.

On success the page does `window.location.replace('home2?token=' + t)`, so
`/home2` is the post-login landing page.

### The captcha refresher

```javascript
function getcap() {
    var url = '/MemberPassBook/passbook/ajax/get-new-captcha';
    $.ajax({
        type: "POST",
        contentType: "application/x-www-form-urlencoded; charset=UTF-8",
        url: url,
        dataType: 'json',
        success: function (data) {
            _html = data.html;
            let tok = data.object;
            if (data.success) {
                $('#captcha_id').attr('src', 'data:image/jpg;base64,' + _html);
                $('input[name="login-token"]').val(tok);
            }
        }
    });
}
```

So: `html` = base64 JPEG, `object` = the **new** login token, `success` = guard.

## Endpoint probes (unauthenticated)

| path | result | meaning |
|---|---|---|
| `/login` | 200 | login page |
| `/passbook/home` | 404 | does not exist |
| `/memberPassbook` | 404 | does not exist |
| `/dashboard` | 404 | does not exist |
| `/passbook/api/ajax/getPassBookData` | 404 | does not exist |
| `/passbook/ajax/passbook` | 404 | does not exist |
| `/home2` | hangs / redirects | **exists, token-guarded** |
| `/passbook` | hangs / redirects | **exists, token-guarded** |

Guarded paths that hang are not proof of an endpoint's exact role, only that
they are not absent. Do not read a hang as a confirmed API.

## Password encoding

```javascript
var passwordHash = CryptoJS.MD5(pass);
var fhashValue   = hex_sha512("kr9rk" + passwordHash.toString() + "kr9rk");
```

`kr9rk` is a hardcoded salt in the page. It is public by construction, which is
why `epfo/crypto.py` stores it as a named constant rather than a secret, and why
`tests/test_crypto.py` asserts its value: changing the portal constant must
break that test, loudly.

## Response shapes

`checkLogin` JSON keys: `success`, `message`, `object`, `otp`.
`get-new-captcha` JSON keys: `success`, `html`, `object`, `otp`, `download`.

A bogus login returns `{"success": false, "message": "Invalid Captcha", ...}` -
HTTP 200 with a structured failure, so the client must read the body, not the
status code.

## After login: the authenticated endpoints

Each page carries its **own** 128-char token in the logged-in page's inline
JavaScript; the login token is rejected by the data APIs. Ask for one page, then
re-read the nav from the page you got back — a harvested token is dead after the
navigation that consumed it.

```text
home2?token=<login token>                 read the nav menu HERE
  ├─ GET /service-history?token=<page token>   → employment history
  ├─ GET /profile?token=<page token>           → profile + KYC
  └─ nav  data-name="passbook" data-token=...  → the ledger page token
       └─ GET /passbook?token=<passbook token>
            ├─ POST /passbook/api/ajax/get-member-yearly-passbook-data?token=...
            │       body year=2026   → the transaction table (HTML, not JSON)
            ├─ POST /passbook/api/ajax/get-member-arch-passbook-data?token=...
            │       → older years the page selector omits
            └─ POST /passbook/api/ajax/final/generate-passbook-pdf?token=<from the fragment>
                    body year=2026   → the PDF (broken server-side; see README)
```

Fetching `/passbook` with the **login** token returns an empty shell — that is
what makes the ledger look unavailable. The PDF token appears only inside the
yearly fragment.

### One UAN, many member ids

A UAN holds one member id per establishment. The home page lists them, and each
row carries **two** identifiers that are not interchangeable:

```text
<span name="mid-mid" data-mid="OPAQUE-PER-SESSION-TOKEN"> GNGGN00000000000000001 </span>
                        ^ only the *balance* endpoint takes this   ^ change-member-id
```

The yearly endpoint serves **whichever member is currently selected** and ignores
a member id passed to it, so switch first with
`POST /passbook/api/ajax/change-member-id` (body `mid=<human id>&mnu=passbook`).
Read once and you report a fraction of the account.

### The year selector is incomplete

For one member the page offered FY 2026 and 2025 while
`…/get-member-arch-passbook-data` returned a replacement selector adding **FY
2024**, a year the ledger endpoint serves normally. A year omitted there is a
whole year of contributions dropped, so the two sources are unioned. The archive
payload also writes its attribute single-quoted (`value='2024'`) where the page
writes `value="2026"` — accept both quoting styles or you silently miss it.

### Two markup traps in the ledger fragment

- **Two headers, bind to the second.** A summary header
  (`Particulars | Employee Share | Employer Share | Pension Share`) sits above the
  real transaction header (`Wage Month | Transaction Date | ...`). Both contain
  "Employee Share"; bind on `"wage month"` or every row is mis-read.
- **A `colspan`'d disclaimer footer** has the same cell count as a data row. Only
  a month-shaped first cell (`May-2026`) distinguishes it — `^`-anchor the match.

The interest line (`Int. Updated upto 31/03/2026`) is row-shaped and carries a
date; it is surfaced as a summary, not a contribution row, because its money is
excluded from the "Total Contributions" lines.

## The session does not survive a process

```text
GET /home2?token=<token>  with saved cookies  ->  302
                                       location: .../login?error=session-exception
```

EPFO rejects a reused session, and redirects to plain `http://`, which a naive
client follows into a request that never answers — that is what makes `passbook`
appear to hang. So the client does **not** follow redirects (`NoRedirect`),
raises `SessionExpired` / `TokenExpired` from the `Location` header, logs in and
reads in one process, and keeps **no** cookie-restore path. A balance read is
idempotent, so `passbook`/`export` retry once on a lapse; a second lapse is a
failure, not a loop.

## Captcha reading

OCR-first, human as the guarantee. A reading must pass a plausibility gate
(alphanumeric, 4-8 chars) or it is rejected rather than submitted; any rejection
falls back to prompting, so the command still completes.

- **`ddddocr` is primary, tesseract the fallback.** On eight live captchas they
  agreed on four and disagreed on four; checking every disputed image showed
  ddddocr right in all four (8/8 vs 4/8), every error an `O`/`9` or `T`/`7`
  confusion. The CLI alternates readers across attempts so a retry is a new bet.
- **`--psm 8`, not `--psm 7`** (tesseract backend). Across six live captchas
  `--psm 7` returned an *empty* string on four; `--psm 8` read all six.
- **The tesseract binary, not `pytesseract`.** The Python binding resolved to a
  Pillow built for another interpreter here and silently disabled OCR.
  `epfo.captcha` execs the binary directly.

## Portals and transport

Two hosts, one stack (`x-powered-by: JSP/2.3`, same cert, same edge
`cookiesession1` cookie): `unifiedportal-mem.epfindia.gov.in` (EPFO's own DC) is
healthy; `passbook.epfindia.gov.in` (RailTel) charges a **flat stall per new TCP
connection** — `connect()` ≈ 35–75 s while a request *on* a warm socket is
milliseconds. It closes the connection after the page GETs. This is why a
multi-request command takes minutes: the fix is a **parallel pre-warmed socket
pool** (open N sockets concurrently; the stalls overlap), not keep-alive reuse
(the server closes the socket) and not HTTP/2 (it resets the stream after one
request). If `passbook`/`ledger` "hang", measure the connect time first — it is a
server-side condition, not a client bug.

Automating a login is a **write** on the Unified Portal but a **read** here; see
`docs/unified-portal-claims.md` for that host's browser-only, OTP-gated flow.
