"""Signed static JSON package index. Unsigned or untrusted indexes are refused."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from readyagents.errors import PackageRefused, TrustError
from readyagents.package.layout import KIND_INDEX
from readyagents.trust.digest import digest_bytes

_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class _Forbid(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IndexEntry(_Forbid):
    name: str
    version: str
    url: str
    digest: str

    @field_validator("version")
    @classmethod
    def _ver(cls, value: str) -> str:
        if not _SEMVER.fullmatch(value.strip()):
            raise ValueError(f"index version is not semver: {value}")
        return value.strip()

    @field_validator("digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        text = value.strip()
        if not text.startswith("sha256:") or len(text) < 15:
            raise ValueError("index digest must be sha256:<hex>")
        return text


class PackageIndex(_Forbid):
    version: int = 1
    packages: list[IndexEntry] = Field(default_factory=list)

    @field_validator("version")
    @classmethod
    def _version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("unsupported index version")
        return value


def load_index_file(path: Path | str) -> PackageIndex:
    file = Path(path)
    if not file.is_file():
        raise PackageRefused(f"package index not found: {file}", reason="missing")
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as extra:
        raise PackageRefused(f"package index malformed: {file}", reason="malformed") from extra
    try:
        return PackageIndex.model_validate(data)
    except ValidationError as extra:
        msg = "; ".join(err["msg"] for err in extra.errors()[:4])
        raise PackageRefused(f"package index refused: {msg}", reason="malformed") from extra


def verify_index(
    path: Path | str,
    *,
    keyring: Any | None = None,
    require_signature: bool = True,
) -> dict[str, Any]:
    """Verify a detached Ed25519 signature on a static index. Fail closed."""
    file = Path(path)
    index = load_index_file(file)
    digest = digest_bytes(file.read_bytes())
    from readyagents.trust.sign import signature_path, verify_artifact

    sig = signature_path(file)
    if not sig.is_file():
        raise PackageRefused("unsigned package index refused", reason="unsigned")
    if not require_signature:
        # Still refuse unsigned; the flag cannot drop the check.
        pass
    try:
        verified = verify_artifact(file, kind=KIND_INDEX, keyring=keyring, digest=digest)
    except TrustError as extra:
        if extra.reason == "unsigned":
            raise PackageRefused("unsigned package index refused", reason="unsigned") from extra
        raise PackageRefused("package index signature mismatch", reason="forged") from extra
    return {
        "ok": True,
        "path": str(file),
        "digest": digest,
        "kind": KIND_INDEX,
        "packages": [row.model_dump() for row in index.packages],
        "signature": verified.get("signature") or "verified",
        "key_id": verified.get("key_id"),
    }
