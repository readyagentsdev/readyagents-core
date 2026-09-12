"""Navigation allowlist and browser SSRF pin (reuse http_get IP blocking)."""

from __future__ import annotations

import ipaddress
import socket
from fnmatch import fnmatch
from typing import Any
from urllib.parse import urlparse, urlunparse

from readyagents.errors import BrowserAllowlist, BrowserSSRF

_METADATA_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata.google.com",
        "metadata.google.internal.",
        "instance-data",
    }
)


def check_url(url: str, patterns: list[str]) -> None:
    """Refuse off-allowlist and private/loopback/metadata destinations.

    IP literals are always SSRF-checked. DNS failure on a public hostname
    does not bypass an allowlist hostname match.
    """
    raw = str(url or "").strip()
    if not raw:
        raise BrowserAllowlist(raw or "<empty>")
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise BrowserAllowlist(raw)
    host = (parsed.hostname or "").lower()
    if not host:
        raise BrowserAllowlist(raw)
    if host.rstrip(".") in {h.rstrip(".") for h in _METADATA_HOSTS}:
        raise BrowserSSRF(raw)
    _check_ip_literal(raw, host)
    if not _allowlisted(raw, parsed, patterns):
        raise BrowserAllowlist(raw)
    _check_resolved(raw, host)


def check_snapshot_requests(snapshot: Any, patterns: list[str]) -> None:
    """Enforce allowlist on the resulting URL, redirects, and sub-resources."""
    url = str(getattr(snapshot, "url", "") or "")
    if url:
        check_url(url, patterns)
    for extra in list(getattr(snapshot, "redirects", None) or []):
        check_url(str(extra), patterns)
    for extra in list(getattr(snapshot, "subresources", None) or []):
        check_url(str(extra), patterns)


def _allowlisted(url: str, parsed: Any, patterns: list[str]) -> bool:
    if not patterns:
        return False
    origin = urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path or "/", "", "", ""))
    stripped = urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path or "/", "", "", ""))
    candidates = [url, origin, stripped]
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    for pattern in patterns:
        pat = str(pattern or "").strip()
        if not pat:
            continue
        for candidate in candidates:
            if fnmatch(candidate, pat) or fnmatch(candidate.lower(), pat.lower()):
                return True
        parsed_pat = urlparse(pat.rstrip("*") if pat.endswith("*") else pat)
        pat_host = (parsed_pat.hostname or "").lower()
        if pat_host and pat_host == host:
            pat_path = parsed_pat.path or "/"
            if pat.endswith("*"):
                if path.startswith(pat_path.rstrip("*") or "/"):
                    return True
            elif path == pat_path or (pat_path in {"", "/"} and path.startswith("/")):
                return True
    return False


def _check_ip_literal(url: str, host: str) -> None:
    ip = _as_ip(host)
    if ip is None:
        return
    if _ip_blocked(ip):
        raise BrowserSSRF(url)


def _check_resolved(url: str, host: str) -> None:
    if _as_ip(host) is not None:
        return
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return
    for info in infos:
        addr = info[4][0] if info[4] else None
        if not addr:
            continue
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _ip_blocked(ip):
            raise BrowserSSRF(url)


def _as_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    text = host.strip().strip("[]")
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        pass
    if text.isdigit():
        try:
            value = int(text)
        except ValueError:
            return None
        if 0 <= value <= 0xFFFFFFFF:
            return ipaddress.IPv4Address(value)
    return None


def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    from readyagents.mcp.builtin import _ip_is_blocked

    return _ip_is_blocked(ip)
