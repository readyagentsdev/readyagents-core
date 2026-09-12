"""Detached Ed25519 signatures beside an artifact (``flow.yaml.sig``).

The signature binds the digest **and** the artifact kind so a workflow
signature cannot validate a pack. Private keys are read from an operator
path at sign time only — never stored, logged, or committed.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from readyagents.atomic import atomic_write_text
from readyagents.errors import TrustError
from readyagents.trust.digest import (
    KIND_INDEX,
    KIND_PACK,
    KIND_PACKAGE,
    KIND_RELEASE,
    KIND_SKILL,
    KIND_WORKFLOW,
    canonical_dumps,
    digest_pack_bytes,
    digest_workflow,
    prefixed,
)

SIG_VERSION = 1
SIG_ALGORITHM = "ed25519"
KIND_CHOICES = frozenset(
    {KIND_WORKFLOW, KIND_PACK, KIND_SKILL, KIND_PACKAGE, KIND_INDEX, KIND_RELEASE}
)


def signature_path(path: Path | str) -> Path:
    file = Path(path)
    return file.parent / f"{file.name}.sig"


def infer_kind(path: Path | str) -> str:
    file = Path(path)
    if file.suffix.lower() == ".py":
        return KIND_PACK
    if file.suffix.lower() == ".rapkg" or file.name.endswith(".rapkg"):
        return KIND_PACKAGE
    if file.name == "readyagents.index.json":
        return KIND_INDEX
    if file.name == "SKILL.md":
        return KIND_SKILL
    if file.is_dir() and (file / "SKILL.md").is_file():
        return KIND_SKILL
    return KIND_WORKFLOW


def digest_artifact(
    path: Path | str,
    *,
    kind: str,
    data: bytes | None = None,
    source: str | None = None,
) -> str:
    if kind in {KIND_PACK, KIND_PACKAGE, KIND_INDEX, KIND_RELEASE}:
        payload = data if data is not None else Path(path).read_bytes()
        return digest_pack_bytes(payload)
    if kind == KIND_SKILL:
        from readyagents.skills.digest import digest_skill_dir

        target = Path(path)
        folder = target.parent if target.is_file() else target
        return digest_skill_dir(folder)
    return digest_workflow(path, source=source)


def _skill_md_file(path: Path) -> Path:
    if path.is_dir():
        return path / "SKILL.md"
    if path.name == "SKILL.md":
        return path
    return path


def signed_message(*, digest: str, kind: str) -> bytes:
    """Canonical bytes Ed25519 signs. Binds digest and kind, not the file name."""
    body = {
        "algorithm": SIG_ALGORITHM,
        "digest": prefixed(digest),
        "kind": kind,
        "version": SIG_VERSION,
    }
    return canonical_dumps(body).encode("utf-8")


def sign_artifact(
    path: Path | str,
    *,
    key: Path | str,
    kind: str | None = None,
    out: Path | str | None = None,
    data: bytes | None = None,
) -> dict[str, Any]:
    file = Path(path)
    resolved_kind = kind or infer_kind(file)
    if resolved_kind not in KIND_CHOICES:
        raise TrustError(
            f"unsupported artifact kind {resolved_kind!r} for {file}",
            artifact=str(file),
            reason="malformed",
        )
    if resolved_kind == KIND_SKILL:
        file = _skill_md_file(file)
    if not file.is_file():
        raise TrustError(
            f"artifact not found: {file}",
            artifact=str(file),
            reason="missing",
        )
    digest = digest_artifact(file, kind=resolved_kind, data=data)
    private, public = _load_private_key(Path(key))
    key_id = key_id_for(public)
    signature = _ed25519_sign(private, signed_message(digest=digest, kind=resolved_kind))
    payload = {
        "version": SIG_VERSION,
        "algorithm": SIG_ALGORITHM,
        "key_id": key_id,
        "digest": digest,
        "kind": resolved_kind,
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    dest = Path(out) if out is not None else signature_path(file)
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(dest, text, encoding="utf-8", newline="\n")
    return payload


def load_signature(path: Path | str) -> dict[str, Any]:
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise TrustError(
            f"signature unreadable: {file}",
            artifact=str(file),
            reason="unreadable_signature",
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TrustError(
            f"signature malformed: {file}",
            artifact=str(file),
            reason="malformed",
        ) from exc
    if not isinstance(data, dict):
        raise TrustError(
            f"signature malformed: {file}",
            artifact=str(file),
            reason="malformed",
        )
    required = ("version", "algorithm", "key_id", "digest", "kind", "signature")
    for field in required:
        if field not in data:
            raise TrustError(
                f"signature missing {field!r}: {file}",
                artifact=str(file),
                reason="malformed",
            )
    if data.get("version") != SIG_VERSION:
        raise TrustError(
            f"unsupported signature version {data.get('version')!r} for {file}",
            artifact=str(file),
            reason="malformed",
        )
    if data.get("algorithm") != SIG_ALGORITHM:
        raise TrustError(
            f"unsupported signature algorithm {data.get('algorithm')!r} for {file}",
            artifact=str(file),
            reason="malformed",
        )
    if data.get("kind") not in KIND_CHOICES:
        raise TrustError(
            f"unsupported signature kind {data.get('kind')!r} for {file}",
            artifact=str(file),
            reason="malformed",
        )
    return data


def verify_artifact(
    path: Path | str,
    *,
    kind: str | None = None,
    data: bytes | None = None,
    keyring: Any | None = None,
    sig_path: Path | str | None = None,
    digest: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Verify a detached signature against the local keyring. Fail closed."""
    file = Path(path)
    resolved_kind = kind or infer_kind(file)
    if resolved_kind == KIND_SKILL:
        file = _skill_md_file(file)
    dest = Path(sig_path) if sig_path is not None else signature_path(file)
    if not dest.is_file():
        raise TrustError(
            f"unsigned artifact: {file}",
            artifact=str(file),
            reason="unsigned",
        )
    payload = load_signature(dest)
    if payload["kind"] != resolved_kind:
        raise TrustError(
            f"signature kind {payload['kind']!r} cannot validate {resolved_kind} {file}",
            artifact=str(file),
            reason="kind_mismatch",
        )
    if digest is None:
        digest = digest_artifact(file, kind=resolved_kind, data=data, source=source)
    else:
        digest = prefixed(digest)
    if prefixed(payload["digest"]) != digest:
        raise TrustError(
            f"tampered artifact: {file}",
            artifact=str(file),
            reason="tampered",
        )
    from readyagents.trust.keyring import Keyring, load_keyring

    ring: Keyring = keyring if keyring is not None else load_keyring()
    entry = ring.find(str(payload["key_id"]))
    if entry is None:
        raise TrustError(
            f"untrusted key {payload['key_id']} for {file}",
            artifact=str(file),
            reason="untrusted_key",
        )
    try:
        signature = base64.b64decode(str(payload["signature"]), validate=True)
    except (ValueError, TypeError) as exc:
        raise TrustError(
            f"signature malformed: {dest}",
            artifact=str(file),
            reason="malformed",
        ) from exc
    message = signed_message(digest=digest, kind=resolved_kind)
    if not _ed25519_verify(entry.raw_public_key(), message, signature):
        raise TrustError(
            f"tampered signature: {file}",
            artifact=str(file),
            reason="tampered",
        )
    return {
        "ok": True,
        "path": str(file),
        "digest": digest,
        "kind": resolved_kind,
        "key_id": entry.key_id,
        "algorithm": SIG_ALGORITHM,
        "signature": "verified",
    }


def key_id_for(public: bytes) -> str:
    import hashlib

    return hashlib.sha256(public).hexdigest()


def crypto_available() -> bool:
    try:
        _ed25519_types()
        return True
    except TrustError:
        return False


def _ed25519_types() -> tuple[Any, Any]:
    # CPython 3.11–3.14 has no stdlib Ed25519 signer. Optional extra only.
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
    except ImportError as exc:
        raise TrustError(
            "Ed25519 signing requires the optional 'sign' extra "
            "(pip install readyagentsdev[sign]). Core runs without it.",
            reason="missing_crypto",
        ) from exc
    return Ed25519PrivateKey, Ed25519PublicKey


def _load_private_key(path: Path) -> tuple[Any, bytes]:
    if not path.is_file():
        raise TrustError(
            f"signing key not found: {path}",
            artifact=str(path),
            reason="missing",
        )
    try:
        blob = path.read_bytes()
    except OSError as exc:
        raise TrustError(
            f"signing key unreadable: {path}",
            artifact=str(path),
            reason="unreadable",
        ) from exc
    Ed25519PrivateKey, _pub_cls = _ed25519_types()
    try:
        if b"-----BEGIN" in blob:
            from cryptography.hazmat.primitives.serialization import load_pem_private_key

            loaded = load_pem_private_key(blob, password=None)
            if not isinstance(loaded, Ed25519PrivateKey):
                raise TrustError(
                    f"signing key is not Ed25519: {path}",
                    artifact=str(path),
                    reason="malformed",
                )
            private = loaded
        elif len(blob) == 32:
            private = Ed25519PrivateKey.from_private_bytes(blob)
        else:
            raise TrustError(
                f"signing key is not an Ed25519 PEM or 32-byte seed: {path}",
                artifact=str(path),
                reason="malformed",
            )
    except TrustError:
        raise
    except Exception as exc:
        raise TrustError(
            f"signing key is not a usable Ed25519 key: {path}",
            artifact=str(path),
            reason="malformed",
        ) from exc
    public = private.public_key().public_bytes_raw()
    return private, public


def _ed25519_sign(private: Any, message: bytes) -> bytes:
    return private.sign(message)


def _ed25519_verify(public: bytes, message: bytes, signature: bytes) -> bool:
    _priv_cls, Ed25519PublicKey = _ed25519_types()
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, message)
        return True
    except Exception:
        return False
