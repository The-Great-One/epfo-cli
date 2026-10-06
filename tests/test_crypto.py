"""Password-encoding tests.

The vector below was produced by the portal's own JavaScript (``sha512.js``
executed in Node, plus ``CryptoJS.MD5`` semantics) and independently reproduced
with Python's hashlib. It is the contract the portal validates against.
"""

import hashlib

import pytest

from epfo.crypto import SALT, encode_password, md5_hex

# Produced by running the portal's own static/js/sha512.js under Node for the
# input below, and confirmed byte-for-byte against Python hashlib.
PASSWORD = "Sahil@Test123"
EXPECTED_MD5 = "8acb4a14b0655ee090aadaa4d53ab533"
EXPECTED_ENCODED = (
    "398468e0d2f88c450e6c82458b327dc371c426c8df62f468e31f073ca23a57f0"
    "f1b55a9a961dd10837222da5e398a92ee23448e30feacbe455177abf10cb9be0")


def test_md5_hex_matches_cryptojs():
    assert md5_hex(PASSWORD) == EXPECTED_MD5


def test_encode_password_matches_portal_javascript():
    assert encode_password(PASSWORD) == EXPECTED_ENCODED


def test_encode_password_is_the_salted_sha512_of_the_md5():
    expected = hashlib.sha512(
        f"{SALT}{EXPECTED_MD5}{SALT}".encode()).hexdigest()
    assert encode_password(PASSWORD) == expected


def test_encode_password_never_contains_the_plaintext():
    assert PASSWORD not in encode_password(PASSWORD)


def test_salt_is_the_constant_hardcoded_in_the_portal_page():
    # If the portal ever changes its salt, this test and encode_password must
    # change together - failing here is the signal, not a nuisance.
    assert SALT == "kr9rk"


@pytest.mark.parametrize("password", ["a", "with space", "unicode-\u00e9\u20b9", "x" * 200])
def test_encoding_is_deterministic_and_hex(password):
    encoded = encode_password(password)
    assert len(encoded) == 128
    assert all(c in "0123456789abcdef" for c in encoded)
    assert encoded == encode_password(password)
