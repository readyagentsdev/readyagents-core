"""Local trust-anchor files. Malformed/unreadable is a hard failure, never a skip."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from readyagents.errors import IdentityError

MAX_ANCHOR_BYTES = 1_048_576
ENV_TRUST = "READYAGENTS_TRUST_ANCHORS"


class _Forbid(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IssuerAnchor(_Forbid):
    issuer: str = Field(min_length=1)
    audience: str | list[str] = "readyagents"
    jwks_file: str = Field(min_length=1)
    actor_claim: str = "sub"
    role_claims: list[str] = Field(default_factory=list)
    role_map: dict[str, str] = Field(default_factory=dict)
    max_skew_seconds: int = Field(default=60, ge=0, le=3600)
    replay: str = "never"
    allow_jwks_fetch: bool = False

    @field_validator("issuer")
    @classmethod
    def _issuer(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("issuer must be non-empty")
        return text

    @field_validator("replay")
    @classmethod
    def _replay(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned not in {"never", "run"}:
            raise ValueError("replay must be 'never' or 'run'")
        return cleaned

    def audiences(self) -> list[str]:
        if isinstance(self.audience, str):
            return [self.audience]
        return [str(item) for item in self.audience]


class TrustAnchors(_Forbid):
    version: int = 1
    require: bool = False
    issuers: list[IssuerAnchor] = Field(default_factory=list)
    source: str | None = None

    def issuer_for(self, issuer: str) -> IssuerAnchor | None:
        for item in self.issuers:
            if item.issuer == issuer:
                return item
        return None


def resolve_trust_path(
    *,
    explicit: str | Path | None = None,
    home: Path | None = None,
    env: dict[str, str] | None = None,
) -> Path | None:
    """``--trust-anchors``, then ``READYAGENTS_TRUST_ANCHORS``, then home/trust.yaml."""
    environ = env if env is not None else os.environ
    if explicit is not None:
        path = Path(explicit)
        if not path.is_file():
            raise IdentityError(f"Trust-anchor file not found: {path}")
        return path
    raw = (environ.get(ENV_TRUST) or "").strip()
    if raw:
        path = Path(raw)
        if not path.is_file():
            raise IdentityError(f"{ENV_TRUST} file not found: {path}")
        return path
    if home is not None:
        beside = Path(home) / "trust.yaml"
        if beside.is_file():
            return beside
    return None


def load_trust_anchors(path: Path | str) -> TrustAnchors:
    file = Path(path)
    try:
        size = file.stat().st_size
    except OSError as exc:
        raise IdentityError(f"Trust-anchor file unreadable: {file}: {exc}") from exc
    if size > MAX_ANCHOR_BYTES:
        raise IdentityError(f"Trust-anchor file {file} is {size} bytes; max is {MAX_ANCHOR_BYTES}")
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise IdentityError(f"Trust-anchor file unreadable: {file}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise IdentityError(f"Trust-anchor file {file} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise IdentityError(f"Trust-anchor file {file} must be a mapping")
    try:
        anchors = TrustAnchors.model_validate(data)
    except ValidationError as exc:
        raise IdentityError(f"Trust-anchor file {file} is malformed: {exc}") from exc
    if anchors.version != 1:
        raise IdentityError(
            f"Trust-anchor file {file}: unsupported version {anchors.version} (expected 1)"
        )
    if not anchors.issuers:
        raise IdentityError(f"Trust-anchor file {file}: issuers list is empty")
    anchors.source = str(file)
    return anchors


def load_jwks_file(path: Path | str, *, base: Path | None = None) -> dict[str, Any]:
    file = Path(path)
    if not file.is_absolute() and base is not None:
        file = base / file
    try:
        size = file.stat().st_size
    except OSError as exc:
        raise IdentityError(f"JWKS file unreadable: {file}: {exc}") from exc
    if size > MAX_ANCHOR_BYTES:
        raise IdentityError(f"JWKS file {file} is {size} bytes; max is {MAX_ANCHOR_BYTES}")
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise IdentityError(f"JWKS file unreadable: {file}: {exc}") from exc
    import json

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IdentityError(f"JWKS file {file} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
        raise IdentityError(f"JWKS file {file} must be an object with a 'keys' array")
    if not data["keys"]:
        raise IdentityError(f"JWKS file {file} has no keys")
    return data
