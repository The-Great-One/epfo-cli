"""Parser tests against the portal's documented passbook structure."""

from dataclasses import fields

import pytest

from epfo.models import (
    _MEMBER_KEYS, MemberDetails, _to_amount, extract_label_values,
    extract_tables, parse_contributions, parse_establishments, parse_member,
    parse_passbook,
)


def test_extract_tables_finds_every_table(passbook_html):
    assert len(extract_tables(passbook_html)) == 3


def test_extract_label_values_pairs_two_cell_rows(passbook_html):
    pairs = extract_label_values(passbook_html)
    assert pairs["UAN"] == "100123456789"
    assert pairs["Member Name"] == "TEST MEMBER"


def test_parse_member_locates_the_identity_fields(passbook_html):
    member = parse_member(passbook_html)
    assert member.uan == "100123456789"
    assert member.name == "TEST MEMBER"
    assert member.member_id == "TESTMB001"
    assert member.missing() == []


def test_parse_member_leaves_unknown_fields_none_not_guessed():
    member = parse_member(
        "<html><table><tr><td>Unrelated</td><td>value</td></tr></table></html>")
    assert member.uan is None
    assert member.name is None
    assert set(member.missing()) == {"uan", "name", "member_id"}


def test_every_member_key_maps_to_a_real_field():
    """Regression guard: _MEMBER_KEYS must only name real dataclass fields.

    A key naming a non-existent attribute used to raise AttributeError deep in
    parse_member() rather than failing at a readable place.
    """
    real = {f.name for f in fields(MemberDetails)}
    assert not set(_MEMBER_KEYS) - real


def test_parse_establishments_returns_code_name_pairs(passbook_html):
    assert ("MHACM0012345000", "ACME PRIVATE LIMITED") in \
        parse_establishments(passbook_html)


def test_parse_contributions_binds_columns_to_the_header(passbook_html):
    rows, notes = parse_contributions(passbook_html)
    assert notes == []
    assert len(rows) == 5
    may = next(r for r in rows if r.wage_month == "05/2024")
    assert may.employee_share == 1800.0
    assert may.employer_share == 1800.0
    assert may.pension_share == 1250.0
    assert may.balance == 7200.0
    assert next(r for r in rows if r.particulars == "Interest").epf_interest == 642.75


def test_parse_contributions_reports_missing_header_instead_of_misreading():
    rows, notes = parse_contributions(
        "<html><table><tr><td>a</td></tr></table></html>")
    assert rows == []
    assert notes and "header not found" in notes[0]


def test_parse_passbook_assembles_all_sections(passbook_html):
    passbook = parse_passbook(passbook_html)
    assert passbook.member.uan == "100123456789"
    assert len(passbook.contributions) == 5
    assert passbook.establishments
    assert passbook.notes == []


@pytest.mark.parametrize("text,expected", [
    ("1,800", 1800.0), ("11,442.75", 11442.75), ("0", 0.0),
    ("", None), (None, None), ("not a number", None),
])
def test_to_amount_converts_or_returns_none(text, expected):
    assert _to_amount(text) == expected
