"""Tests for the Unified Portal command's non-browser parts.

The browser half needs a live Chrome and a human OTP, so it is exercised
end-to-end by hand (see docs/unified-portal-claims.md). What is unit-testable is
the pure logic: the claim-blocker classifier and the page list.
"""

from epfo.cli import _claim_blocker
from epfo.unified import CLAIM_PAGES


def test_claim_pages_covers_the_claim_surface():
    fragments = {frag for _, frag in CLAIM_PAGES}
    assert "cenonline/claim/getReceipt" in fragments
    assert "kyc/viewKYCRegistrationForm" in fragments
    assert "cenOtcpMemberInterface/loadTxClaimHome" in fragments


def test_claim_blocker_flags_a_missing_kyc_bank():
    text = ("1. PLEASE UPDATE YOUR LATEST BANK ACCOUNT NUMBER AND VALID IFSC "
            "DETAILS (USING MENU MANAGE >> KYC). 2. AVAILABLE IFSC (INVALIDIFSC) "
            "IS INVALID.")
    note = _claim_blocker("claim", text)
    assert note is not None and "KYC" in note


def test_claim_blocker_is_silent_on_other_pages():
    # The INVALIDIFSC banner is specific to the claim page; another page that
    # merely mentions IFSC must not be reported as blocked.
    assert _claim_blocker("kyc", "AVAILABLE IFSC (INVALIDIFSC) IS INVALID") is None


def test_claim_blocker_passes_a_clean_claim_page():
    assert _claim_blocker("claim", "Certificate of Undertaking I agree to the "
                          "terms and conditions.") is None
