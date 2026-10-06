"""Endpoint-discovery tests against the real login page and JS."""

import pytest

from epfo.discover import (
    Endpoint, is_asset, merge, scan_inline_params, scan_source,
)


def test_scan_source_finds_real_portal_paths_inside_javascript():
    script = (
        '$.ajax({ url: "/MemberPassBook/passbook/ajax/get-new-captcha" });\n'
        "fetch('/MemberPassBook/passbook/api/ajax/final/checkLogin');"
    )
    found = scan_source(script, "test")
    assert "/MemberPassBook/passbook/ajax/get-new-captcha" in found
    assert "/MemberPassBook/passbook/api/ajax/final/checkLogin" in found


def test_scan_source_ignores_data_and_javascript_scheme_urls():
    found = scan_source('url: "data:image/png;base64,AAAA"', "test")
    assert found == {}


def test_scan_inline_params_attributes_data_keys_to_the_nearest_call():
    html = (
        "<script>\n"
        '  url: "/MemberPassBook/passbook/api/ajax/final/checkLogin",\n'
        '  data["username"] = a; data["password"] = b; data["token"] = c\n'
        "</script>"
    )
    found = scan_inline_params(html, "inline:login")
    endpoint = found["/MemberPassBook/passbook/api/ajax/final/checkLogin"]
    assert set(endpoint.parameters) == {"username", "password", "token"}


def test_scan_of_the_real_login_page_recovers_the_login_endpoints(login_html):
    found = scan_inline_params(login_html, "inline:login")
    assert "/MemberPassBook/passbook/api/ajax/final/checkLogin" in found


def test_merge_unions_parameters_and_provenance_across_sources():
    a = {"/x": Endpoint(path="/x", parameters=("p",), sources=("a",))}
    b = {"/x": Endpoint(path="/x", parameters=("q",), sources=("b",))}
    merged = merge(a, b)
    assert len(merged) == 1
    assert set(merged[0].parameters) == {"p", "q"}
    assert set(merged[0].sources) == {"a", "b"}


def test_endpoint_normalization_strips_query_and_scheme():
    endpoint = Endpoint(path="https://host/MemberPassBook/x?y=1")
    assert endpoint.normalized == "/MemberPassBook/x"


# -- regression: assets were being reported as endpoints --------------------

def test_static_assets_are_not_endpoints():
    found = scan_source(
        'url: "/MemberPassBook/static/css/login.css"\n'
        'url: "/MemberPassBook/static/js/sha512.js"\n'
        'url: "/MemberPassBook/static/images/epfo_logo.png"\n'
        'url: "/MemberPassBook/passbook/ajax/get-new-captcha"', "test")
    assert list(found) == ["/MemberPassBook/passbook/ajax/get-new-captcha"]


@pytest.mark.parametrize("path,asset", [
    ("/MemberPassBook/static/js/sha512.js", True),
    ("/MemberPassBook/static/css/login.css", True),
    ("/x/logo.png", True),
    ("/x/font.woff2?v=2", True),
    ("/MemberPassBook/passbook/ajax/get-new-captcha", False),
    ("/MemberPassBook/passbook/api/ajax/final/checkLogin", False),
])
def test_is_asset_classifies_assets_and_apis(path, asset):
    assert is_asset(path) is asset


# -- regression: params above the URL literal were missed -------------------

def test_params_are_bound_to_a_url_that_follows_them():
    """On the live page the data[...] assignments precede the url: literal.

    A forward-only window finds zero parameters here, which is exactly the bug
    that hid the login contract.
    """
    html = (
        "<script>\n"
        '  data["username"] = $("#username").val();\n'
        '  data["password"] = $("#password1").val();\n'
        '  data["token"]    = $("input[name=\'login-token\']").val();\n'
        '  data["answer"]   = $("#captcha").val();\n'
        '  $.ajax({ url: "/MemberPassBook/passbook/api/ajax/final/checkLogin",\n'
        "           data: data });\n"
        "</script>"
    )
    found = scan_inline_params(html, "inline:test")
    endpoint = found["/MemberPassBook/passbook/api/ajax/final/checkLogin"]
    assert set(endpoint.parameters) == {"username", "password", "token", "answer"}


def test_the_real_login_page_exposes_the_four_login_parameters(login_html):
    """The discovery tool must independently reproduce session.login()'s contract."""
    found = scan_inline_params(login_html, "inline:login")
    endpoint = found["/MemberPassBook/passbook/api/ajax/final/checkLogin"]
    assert set(endpoint.parameters) == {"username", "password", "token", "answer"}


# -- post-login page: embedded per-endpoint tokens and member ids -----------

LOGIN_PAGE_SNIPPET = (
    "<script>\n"
    "var url = '/MemberPassBook/passbook/api/ajax/get-member-trans-passbook-data?token=AbC123xyz';\n"
    "var url = '/MemberPassBook/passbook/api/check-uan-profile-service?token=DeF456uvw';\n"
    "</script>\n"
    '<a href="/MemberPassBook/home?token=GhI789rst">home</a>\n'
    '<span name="mid-bal" data-mid="BKeA3IedD4geHxKeANTKwr0f1rFFfJ2q"></span>\n'
    '<span name="mid-bal" data-mid="SvfRxt903MPXjHQngwYsaFiOLyqpnUted"></span>\n'
)


def test_embedded_tokens_are_keyed_without_the_memberpassbook_prefix():
    from epfo.discover import embedded_tokens

    tokens = embedded_tokens(LOGIN_PAGE_SNIPPET)
    assert tokens[
        "/passbook/api/ajax/get-member-trans-passbook-data"] == "AbC123xyz"
    assert tokens["/passbook/api/check-uan-profile-service"] == "DeF456uvw"
    assert tokens["/home"] == "GhI789rst"


def test_embedded_token_lookup_by_suffix_never_returns_none():
    """Regression: a prefix mismatch made the token lookup silently return None."""
    from epfo.discover import embedded_tokens

    tokens = embedded_tokens(LOGIN_PAGE_SNIPPET)
    path = "/passbook/api/ajax/get-member-trans-passbook-data"
    assert tokens.get(path) is not None


def test_member_ids_are_extracted_and_deduplicated():
    from epfo.discover import member_ids

    ids = member_ids(LOGIN_PAGE_SNIPPET * 2)
    assert ids == ["BKeA3IedD4geHxKeANTKwr0f1rFFfJ2q",
                   "SvfRxt903MPXjHQngwYsaFiOLyqpnUted"]


def test_member_ids_absent_is_an_empty_list_not_an_error():
    from epfo.discover import member_ids

    assert member_ids("<html>no ids</html>") == []
