"""Optional signed workload identity. Private key never enters records or whoami."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from readyagents.errors import IdentityError
from readyagents.workflow.state import utc_now

ENV_SUBJECT = "READYAGENTS_WORKLOAD_SUBJECT"
ENV_KEY = "READYAGENTS_WORKLOAD_KEY"
ENV_KID = "READYAGENTS_WORKLOAD_KID"
ENV_PUB = "READYAGENTS_WORKLOAD_PUB"


def workload_configured(*, env: dict[str, str] | None = None) -> bool:
    import os

    environ = env if env is not None else os.environ
    return bool((environ.get(ENV_SUBJECT) or "").strip() and (environ.get(ENV_KEY) or "").strip())


def whoami(*, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Subject, key id, fingerprint. Never the private key."""
    import os

    environ = env if env is not None else os.environ
    subject = (environ.get(ENV_SUBJECT) or "").strip()
    key_path = (environ.get(ENV_KEY) or "").strip()
    kid = (environ.get(ENV_KID) or "").strip() or "workload"
    if not subject or not key_path:
        return {
            "configured": False,
            "subject": None,
            "key_id": None,
            "fingerprint": None,
        }
    pub = (environ.get(ENV_PUB) or "").strip()
    fingerprint = _fingerprint(Path(key_path), Path(pub) if pub else None)
    return {
        "configured": True,
        "subject": subject,
        "key_id": kid,
        "fingerprint": fingerprint,
    }


def sign_assertion(
    *,
    audience: str,
    ttl_seconds: int = 60,
    env: dict[str, str] | None = None,
) -> str:
    """Short-lived JWT identifying this agent. Requires the jwt extra."""
    import os
    import time

    from readyagents.identity.verify import require_jwt_lib

    jwt = require_jwt_lib()
    environ = env if env is not None else os.environ
    subject = (environ.get(ENV_SUBJECT) or "").strip()
    key_path = (environ.get(ENV_KEY) or "").strip()
    kid = (environ.get(ENV_KID) or "").strip() or "workload"
    if not subject or not key_path:
        raise IdentityError("workload identity is not configured")
    pem = _read_private(Path(key_path))
    now = int(time.time())
    payload = {
        "sub": subject,
        "iss": subject,
        "aud": audience,
        "iat": now,
        "nbf": now,
        "exp": now + max(1, int(ttl_seconds)),
        "jti": hashlib.sha256(f"{subject}:{now}:{utc_now()}".encode()).hexdigest()[:16],
    }
    try:
        return jwt.encode(payload, pem, algorithm="RS256", headers={"kid": kid})
    except Exception as exc:  # noqa: BLE001
        raise IdentityError(f"workload assertion failed: {exc}") from exc


def _read_private(path: Path) -> bytes:
    if not path.is_file():
        raise IdentityError(f"workload key not found: {path}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise IdentityError(f"workload key unreadable: {path}: {exc}") from exc
    if b"PRIVATE KEY" not in data:
        raise IdentityError(f"workload key {path} is not a PEM private key")
    return data


def _fingerprint(key_path: Path, pub_path: Path | None) -> str:
    if pub_path is not None and pub_path.is_file():
        material = pub_path.read_bytes()
        return "sha256:" + hashlib.sha256(material).hexdigest()
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
    except ImportError as exc:
        raise IdentityError(
            "JWT extra is not installed; cannot derive a public fingerprint from a private key. "
            "Set READYAGENTS_WORKLOAD_PUB or pip install 'readyagentsdev[jwt]'."
        ) from exc
    pem = _read_private(key_path)
    try:
        key = load_pem_private_key(pem, password=None)
    except Exception as exc:  # noqa: BLE001
        raise IdentityError(f"workload key is unusable: {exc}") from exc
    public = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return "sha256:" + hashlib.sha256(public).hexdigest()
