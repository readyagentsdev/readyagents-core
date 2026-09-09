"""HMAC-SHA256 raw-body signing for inbound decisions. No JSON parse, no resume."""

from __future__ import annotations

import hashlib
import hmac


def sign_body(secret: str, body: bytes) -> str:
    """Return hex HMAC-SHA256 of body with utf-8 secret."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def parse_signature_header(value: str | None) -> str | None:
    """Strip optional sha256= prefix; empty -> None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered.startswith("sha256="):
        text = text.split("=", 1)[1].strip()
    return text or None


def verify_signed_body(secret: str, body: bytes, signature: str | None) -> None:
    """Constant-time compare. Raise ValueError for unsigned or forged payloads.

    Does not parse JSON. Does not resume a run.
    """
    parsed = parse_signature_header(signature)
    if not parsed:
        raise ValueError("unsigned decision: missing signature")
    expected = sign_body(secret, body)
    try:
        matched = hmac.compare_digest(expected, parsed)
    except ValueError:
        matched = False
    if not matched:
        raise ValueError("forged decision: signature mismatch")
