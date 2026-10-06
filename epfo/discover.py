"""Enumerate the portal's real endpoints from its own client-side JavaScript.

The portal guards every post-login path with a token, so the API surface cannot
be probed anonymously: an unauthenticated request to ``home2`` or ``passbook``
redirects to ``login?error=token-exception``. The only honest way to learn the
surface is to read the JavaScript the portal itself serves.

Two sources are used:

1. **Static scripts.** ``static/js/*.js`` and any inline ``<script>`` blocks on
   the pages we can reach without a session.
2. **Live browser capture.** When a CDP endpoint is available, every script the
   page actually loads is downloaded and scanned, which also picks up the
   post-login bundle that static fetching cannot reach.

This discovers endpoints; it does not attempt to call privileged ones.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

# Candidate endpoint shapes. The portal mixes jQuery $.ajax calls, fetch(), and
# plain string literals in attribute/url assignments.
_PATTERNS = (
    re.compile(r"""url\s*:\s*['"]([^'"]+)['"]"""),
    re.compile(r"""fetch\s*\(\s*['"]([^'"]+)['"]"""),
    re.compile(r"""\.(?:get|post|ajax)\s*\(\s*['"]([^'"]+)['"]"""),
    re.compile(r"""['"](/MemberPassBook/[A-Za-z0-9/._?=&{}$:-]+)['"]"""),
    re.compile(r"""action\s*=\s*['"]([^'"]+)['"]"""),
)

_PARAM_RE = re.compile(r"""data\s*\[\s*['"](\w+)['"]\s*\]\s*=""")
_NAME_RE = re.compile(r"""name\s*=\s*['"]([^'"]+)['"]""")

# Assets are served under /static/ or carry a file extension. They are not API
# surface, and reporting them as "endpoints" would bury the real ones.
_ASSET_RE = re.compile(
    r"(?:/static/|\.(?:js|css|png|jpe?g|gif|svg|ico|woff2?|ttf|map)(?:\?|$))",
    re.I)

_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)

# How far from a URL literal a `data["x"] = ...` assignment may sit and still
# belong to that call. Measured on the live login page, where the submit handler
# assigns username/password/token/answer 170-350 characters ABOVE the url:, so a
# forward-only window finds nothing.
_PARAM_RADIUS = 900


def is_asset(path: str) -> bool:
    """True when a path is a static asset rather than an API endpoint."""
    return bool(_ASSET_RE.search(path))


@dataclass
class Endpoint:
    """One endpoint observed in portal source, with how it was found."""

    path: str
    parameters: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()

    @property
    def normalized(self) -> str:
        parsed = urlparse(self.path)
        return parsed.path or self.path


def scan_source(text: str, label: str) -> dict[str, Endpoint]:
    """Extract endpoints and nearby form-field names from one script body."""
    found: dict[str, Endpoint] = {}
    for pattern in _PATTERNS:
        for match in pattern.finditer(text):
            raw = match.group(1).strip()
            if not raw or raw.startswith(("data:", "javascript:", "#")):
                continue
            if is_asset(raw):
                continue
            endpoint = Endpoint(path=raw)
            existing = found.get(endpoint.normalized)
            if existing:
                endpoint = Endpoint(
                    path=existing.path,
                    parameters=existing.parameters,
                    sources=tuple(sorted(set(existing.sources + (label,)))))
            else:
                endpoint.sources = (label,)
            found[endpoint.normalized] = endpoint
    return found


def scan_inline_params(html: str, label: str) -> dict[str, Endpoint]:
    """Scan a page's inline scripts, binding params to the call that uses them.

    Scope matters here. Matching URL literals across the whole document binds
    parameters to whichever occurrence of a path came first - and on the EPFO
    login page the first occurrence of the login path is the ``<form action>``,
    which carries no parameters at all, so the real submit call's
    ``data["username"]``/``data["password"]`` keys were missed. Scanning only
    inside ``<script>`` blocks and taking the parameters that follow each call
    within the same block fixes it.
    """
    found: dict[str, Endpoint] = {}
    for block in _SCRIPT_RE.findall(html):
        param_positions = [(m.start(), m.group(1))
                           for m in _PARAM_RE.finditer(block)]
        for pattern in _PATTERNS:
            for match in pattern.finditer(block):
                raw = match.group(1).strip()
                if not raw.startswith(("/", "http")) or is_asset(raw):
                    continue
                params = tuple(sorted({
                    name for position, name in param_positions
                    if abs(position - match.start()) <= _PARAM_RADIUS}))
                endpoint = Endpoint(path=raw, parameters=params, sources=(label,))
                existing = found.get(endpoint.normalized)
                if existing:
                    endpoint = Endpoint(
                        path=existing.path,
                        parameters=tuple(sorted(set(existing.parameters + params))),
                        sources=tuple(sorted(set(existing.sources + (label,)))))
                found[endpoint.normalized] = endpoint
    return found


def merge(*maps: dict[str, Endpoint]) -> list[Endpoint]:
    """Merge endpoint maps, unioning parameter and source provenance."""
    combined: dict[str, Endpoint] = {}
    for mapping in maps:
        for key, endpoint in mapping.items():
            existing = combined.get(key)
            if existing:
                combined[key] = Endpoint(
                    path=existing.path,
                    parameters=tuple(sorted(set(existing.parameters + endpoint.parameters))),
                    sources=tuple(sorted(set(existing.sources + endpoint.sources))))
            else:
                combined[key] = endpoint
    return sorted(combined.values(), key=lambda e: e.normalized)


def fetch_scripts(endpoint: str, target_id: str,
                  origins: list[str] | None = None) -> dict[str, str]:
    """Download every script the page loads, via CDP.

    Returns ``{url or label: source}``. This is what reaches the post-login
    bundle that anonymous static fetching cannot.
    """
    from .cdp import CDPClient

    with CDPClient(endpoint, target_id) as client:
        client.enable(["Page", "Network", "Runtime"])
        urls = client.script_urls(origins=origins)
        sources = {}
        for url in urls:
            try:
                sources[url] = client.fetch_text(url)
            except Exception as exc:  # a blocked script must not abort discovery
                sources[url] = f"<unavailable: {exc}>"
        return sources


_EMBEDDED_TOKEN_RE = re.compile(r"([A-Za-z0-9/_-]+)\?token=([A-Za-z0-9+/=_-]+)")
_MID_RE = re.compile(r"""data-mid=["']([A-Za-z0-9+/=_-]{20,})["']""")


def _normalize_endpoint_key(path: str) -> str:
    """Strip a leading /MemberPassBook so lookups are prefix-independent.

    The page writes one link as ``/MemberPassBook/home`` and API calls as
    ``/MemberPassBook/passbook/api/...``, while callers naturally ask for
    ``/passbook/api/...``. Normalizing here keeps those from silently missing.
    """
    prefix = "/MemberPassBook/"
    if path.startswith(prefix):
        return "/" + path[len(prefix):]
    return path if path.startswith("/") else "/" + path


def embedded_tokens(html: str) -> dict[str, str]:
    """Per-endpoint tokens the portal embeds in its own post-login pages.

    A logged-in page carries a *different* token for each AJAX endpoint inside
    its inline JavaScript. These are the tokens a client must reuse; the
    login-time token is not accepted by the API endpoints.
    """
    tokens: dict[str, str] = {}
    for path, token in _EMBEDDED_TOKEN_RE.findall(html):
        tokens.setdefault(_normalize_endpoint_key(path), token)
    return tokens


def member_ids(html: str) -> list[str]:
    """Member IDs (``data-mid``) present on a post-login page, deduplicated."""
    seen: list[str] = []
    for mid in _MID_RE.findall(html):
        if mid not in seen:
            seen.append(mid)
    return seen


def write_report(endpoints: list[Endpoint], path: Path) -> Path:
    path = Path(path)
    path.write_text(json.dumps(
        [{"path": e.normalized,
          "raw": e.path,
          "parameters": list(e.parameters),
          "sources": list(e.sources)} for e in endpoints], indent=2))
    return path
