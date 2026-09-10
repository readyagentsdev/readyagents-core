"""Local publisher keyring under ``$READYAGENTS_HOME``. Fail closed if malformed."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from readyagents.atomic import atomic_write_text
from readyagents.errors import TrustError
from readyagents.permissions import restrict_dir, restrict_file

KEYRING_VERSION = 1
KEYRING_NAME = "keyring.json"

# RFC 8410 SubjectPublicKeyInfo prefix for Ed25519 (12 bytes) + 32-byte key.
_ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


@dataclass(frozen=True)
class TrustedKey:
    key_id: str
    name: str
    public_key: str
    added_at: str

    def raw_public_key(self) -> bytes:
        try:
            raw = base64.b64decode(self.public_key, validate=True)
        except (ValueError, TypeError) as exc:
            raise TrustError(
                f"keyring entry {self.key_id} has a malformed public_key",
                artifact=self.key_id,
                reason="malformed",
            ) from exc
        if len(raw) != 32:
            raise TrustError(
                f"keyring entry {self.key_id} is not a 32-byte Ed25519 public key",
                artifact=self.key_id,
                reason="malformed",
            )
        return raw

    def as_dict(self) -> dict[str, str]:
        return {
            "key_id": self.key_id,
            "name": self.name,
            "public_key": self.public_key,
            "added_at": self.added_at,
        }


@dataclass
class Keyring:
    keys: list[TrustedKey]
    path: Path | None = None

    def find(self, key_id: str) -> TrustedKey | None:
        for item in self.keys:
            if item.key_id == key_id:
                return item
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": KEYRING_VERSION,
            "keys": [item.as_dict() for item in self.keys],
        }


def keyring_path(home: Path | str | None = None) -> Path:
    if home is None:
        from readyagents.config import get_settings

        base = get_settings().home_path()
    else:
        base = Path(home)
    return Path(base) / KEYRING_NAME


def load_keyring(path: Path | str | None = None, *, home: Path | str | None = None) -> Keyring:
    dest = Path(path) if path is not None else keyring_path(home)
    if not dest.exists():
        return Keyring(keys=[], path=dest)
    if not dest.is_file():
        raise TrustError(
            f"keyring is not a file: {dest}",
            artifact=str(dest),
            reason="malformed",
        )
    try:
        text = dest.read_text(encoding="utf-8")
    except OSError as exc:
        raise TrustError(
            f"keyring unreadable: {dest}",
            artifact=str(dest),
            reason="unreadable",
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TrustError(
            f"keyring malformed: {dest}",
            artifact=str(dest),
            reason="malformed",
        ) from exc
    if not isinstance(data, dict):
        raise TrustError(
            f"keyring malformed: {dest}",
            artifact=str(dest),
            reason="malformed",
        )
    if data.get("version") != KEYRING_VERSION:
        raise TrustError(
            f"unsupported keyring version {data.get('version')!r}: {dest}",
            artifact=str(dest),
            reason="malformed",
        )
    raw_keys = data.get("keys")
    if not isinstance(raw_keys, list):
        raise TrustError(
            f"keyring malformed: {dest}",
            artifact=str(dest),
            reason="malformed",
        )
    keys: list[TrustedKey] = []
    for index, row in enumerate(raw_keys):
        if not isinstance(row, dict):
            raise TrustError(
                f"keyring malformed at index {index}: {dest}",
                artifact=str(dest),
                reason="malformed",
            )
        try:
            item = TrustedKey(
                key_id=str(row["key_id"]),
                name=str(row["name"]),
                public_key=str(row["public_key"]),
                added_at=str(row["added_at"]),
            )
        except KeyError as exc:
            raise TrustError(
                f"keyring malformed at index {index}: missing {exc}: {dest}",
                artifact=str(dest),
                reason="malformed",
            ) from exc
        if not item.key_id or not item.name:
            raise TrustError(
                f"keyring malformed at index {index}: empty key_id or name: {dest}",
                artifact=str(dest),
                reason="malformed",
            )
        _ = item.raw_public_key()
        keys.append(item)
    return Keyring(keys=keys, path=dest)


def save_keyring(ring: Keyring, path: Path | str | None = None) -> Path:
    dest = Path(path) if path is not None else ring.path or keyring_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    restrict_dir(dest.parent)
    payload = json.dumps(ring.as_dict(), sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(dest, payload, encoding="utf-8", newline="\n", restrict=True)
    restrict_file(dest)
    ring.path = dest
    return dest


def add_key(
    public_key: Path | str | bytes,
    *,
    name: str,
    home: Path | str | None = None,
    path: Path | str | None = None,
    added_at: str | None = None,
) -> TrustedKey:
    label = name.strip()
    if not label:
        raise TrustError("trusted key name must be non-empty", reason="malformed")
    dest = Path(path) if path is not None else keyring_path(home)
    ring = load_keyring(dest)
    raw = parse_public_key(public_key)
    from readyagents.trust.sign import key_id_for

    key_id = key_id_for(raw)
    encoded = base64.b64encode(raw).decode("ascii")
    stamp = added_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    existing = ring.find(key_id)
    if existing is not None:
        raise TrustError(
            f"key {key_id} is already trusted as {existing.name!r}",
            artifact=key_id,
            reason="duplicate",
        )
    entry = TrustedKey(key_id=key_id, name=label, public_key=encoded, added_at=stamp)
    ring.keys.append(entry)
    save_keyring(ring, dest)
    return entry


def remove_key(
    key_id: str,
    *,
    home: Path | str | None = None,
    path: Path | str | None = None,
) -> TrustedKey:
    dest = Path(path) if path is not None else keyring_path(home)
    ring = load_keyring(dest)
    token = key_id.strip()
    found = ring.find(token)
    if found is None:
        raise TrustError(
            f"trusted key not found: {token}",
            artifact=token,
            reason="missing",
        )
    ring.keys = [item for item in ring.keys if item.key_id != token]
    save_keyring(ring, dest)
    return found


def parse_public_key(source: Path | str | bytes) -> bytes:
    """PEM SPKI, raw 32 bytes, hex, or base64. Stdlib only — no crypto extra."""
    if isinstance(source, (bytes, bytearray)):
        blob = bytes(source)
        if len(blob) == 32:
            return blob
        label = "<bytes>"
    else:
        path = Path(source)
        if path.is_file():
            try:
                blob = path.read_bytes()
            except OSError as exc:
                raise TrustError(
                    f"public key unreadable: {path}",
                    artifact=str(path),
                    reason="unreadable",
                ) from exc
            if len(blob) == 32:
                return blob
            label = str(path)
        else:
            blob = str(source).encode("utf-8")
            label = str(source)
    stripped = blob.strip()
    if stripped.startswith(b"-----BEGIN"):
        return _from_pem(stripped, label)
    if len(stripped) == 32:
        return stripped
    text = stripped.decode("ascii", errors="strict") if _is_ascii(stripped) else ""
    hex_text = "".join(text.split())
    if len(hex_text) == 64 and all(c in "0123456789abcdefABCDEF" for c in hex_text):
        return bytes.fromhex(hex_text)
    try:
        raw = base64.b64decode(stripped, validate=True)
    except (ValueError, TypeError):
        raw = b""
    if len(raw) == 32:
        return raw
    if raw.startswith(_ED25519_SPKI_PREFIX) and len(raw) == 44:
        return raw[len(_ED25519_SPKI_PREFIX) :]
    raise TrustError(
        f"public key is not Ed25519 PEM, raw, hex, or base64: {label}",
        artifact=label,
        reason="malformed",
    )


def _from_pem(blob: bytes, label: str) -> bytes:
    lines = [
        line.strip()
        for line in blob.decode("ascii", errors="strict").splitlines()
        if line.strip() and not line.startswith("-----")
    ]
    try:
        der = base64.b64decode("".join(lines), validate=True)
    except (ValueError, TypeError) as exc:
        raise TrustError(
            f"public key PEM is malformed: {label}",
            artifact=label,
            reason="malformed",
        ) from exc
    if der.startswith(_ED25519_SPKI_PREFIX) and len(der) == 44:
        return der[len(_ED25519_SPKI_PREFIX) :]
    raise TrustError(
        f"public key is not Ed25519: {label}",
        artifact=label,
        reason="malformed",
    )


def _is_ascii(blob: bytes) -> bool:
    try:
        blob.decode("ascii")
        return True
    except UnicodeDecodeError:
        return False
