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
