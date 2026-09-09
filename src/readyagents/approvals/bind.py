"""Loopback bind checks for the localhost approval UI.

Reject non-loopback hosts before a socket is opened. Independent of the MCP extra.
"""

from __future__ import annotations

import ipaddress

from readyagents.config import LOOPBACK_HOSTS
from readyagents.errors import ConfigError

DEFAULT_APPROVAL_HOST = "127.0.0.1"
DEFAULT_APPROVAL_PORT = 8766
MIN_PORT = 1
MAX_PORT = 65535


def assert_loopback_host(host: str) -> str:
    """Reject non-loopback bind hosts. Allow 127.0.0.1, localhost, and ::1."""
    if host is None or not str(host).strip():
        raise ConfigError("Approval UI bind host must be loopback, not empty.")
    raw = str(host).strip()
    if raw.startswith("[") and raw.endswith("]") and len(raw) > 2:
        raw = raw[1:-1]
    lowered = raw.lower()
    if lowered in {"0.0.0.0", "::", "[::]"}:
        raise ConfigError(
            f"Approval UI bind host '{host}' is not loopback. Use 127.0.0.1, localhost, or ::1."
        )
    if lowered in LOOPBACK_HOSTS:
        return raw
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise ConfigError(
            f"Approval UI bind host '{host}' is not loopback. Use 127.0.0.1, localhost, or ::1."
        ) from exc
    if ip.is_unspecified or not ip.is_loopback:
        raise ConfigError(
            f"Approval UI bind host '{host}' is not loopback. Use 127.0.0.1, localhost, or ::1."
        )
    return raw


def assert_port(port: int) -> int:
    value = int(port)
    if not (MIN_PORT <= value <= MAX_PORT):
        raise ConfigError(f"Approval UI port must be between {MIN_PORT} and {MAX_PORT}.")
    return value
