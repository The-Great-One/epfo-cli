"""epfo-cli - a command line client for the EPFO member passbook portal.

The portal (passbook.epfindia.gov.in) is a legacy JSP application. This package
keeps every finding about how it actually behaves in exactly one place, so the
quirks are auditable instead of scattered through the code:

* ``crypto``     - the real password encoding (MD5 -> salted SHA-512).
* ``session``    - cookies, login-token rotation, token-guarded post-login paths.
* ``captcha``    - OCR as an opt-in convenience, human entry as the default.
* ``discover``   - enumerating the endpoint surface from the portal's own JS.
* ``cdp``        - a stdlib Chrome DevTools Protocol client for live capture.
* ``models``     - HTML -> typed records, calibrated rather than assumed.

Nothing in this package invents data. Where the portal's behaviour is unknown,
the code fails with an explicit error instead of returning a guess.
"""

__version__ = "0.1.0"
