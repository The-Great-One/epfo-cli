"""Shared test fixtures."""

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent


@pytest.fixture
def login_html() -> str:
    """The portal's real login page, trimmed to its structural parts."""
    return (FIXTURES / "fixtures_login.html").read_text(encoding="utf-8")


@pytest.fixture
def passbook_html() -> str:
    """A synthetic passbook page with the portal's documented layout."""
    return (FIXTURES / "fixtures_passbook.html").read_text(encoding="utf-8")
