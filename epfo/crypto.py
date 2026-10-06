"""Password encoding for the EPFO member passbook portal.

The portal does NOT send your password. `login.html` defines:

    var passwordHash = CryptoJS.MD5(pass);
    var fhashValue = hex_sha512("kr9rk" + passwordHash.toString() + "kr9rk");
    $("#password1").val(fhashValue);

so the wire value is SHA-512("kr9rk" + MD5(password_hex) + "kr9rk"), and the
field carrying it is named `password` (the plain `#password` input is never
transmitted; only the hidden `#password1` is).

Verified two ways (2026-10-06):
  * Python hashlib reproduces the browser formula.
  * The portal's own `static/js/sha512.js` executed in Node produces byte
    identical output for the same input.

The ``kr9rk`` salt is a hardcoded constant in the page's JavaScript. It is
therefore public, not a secret, and is reproduced here rather than treated as
a credential.
"""

from __future__ import annotations

import hashlib

SALT = "kr9rk"


def md5_hex(password: str) -> str:
    """MD5 of the password, lowercase hex - mirrors CryptoJS.MD5(pass)."""
    return hashlib.md5(password.encode("utf-8")).hexdigest()


def encode_password(password: str) -> str:
    """Produce the value the portal expects in its `password` form field."""
    return hashlib.sha512(f"{SALT}{md5_hex(password)}{SALT}".encode("utf-8")).hexdigest()
