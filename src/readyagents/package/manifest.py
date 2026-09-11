"""Parse and validate ``readyagents.pkg.yaml``. Extra keys and secret values fail closed."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from readyagents.errors import PackageRefused
from readyagents.package.layout import MANIFEST_NAME, MAX_DESCRIPTION, MAX_NAME

_NAME = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_REL = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")


class _Forbid(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequiresSpec(_Forbid):
    readyagents: str | None = None
    packs: list[str] = Field(default_factory=list)
    connectors: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    mcp: list[str] = Field(default_factory=list)

    @field_validator("packs", "connectors", "skills", "mcp")
    @classmethod
    def _names(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for item in value:
            text = str(item).strip()
            if not text:
                raise ValueError("dependency name must be non-empty")
            if any(ch in text for ch in "\\:= \t"):
                raise ValueError(f"dependency name refused: {text}")
            out.append(text)
        return out

    @field_validator("readyagents")
    @classmethod
    def _core(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        if not re.fullmatch(r">=\d+(?:\.\d+){0,2}", text):
            raise ValueError(f"unsupported readyagents requirement {text!r}")
        return text


class PackageBudget(_Forbid):
    max_cost_usd: float | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, ge=0)


class PackageManifest(_Forbid):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    version: str
    description: str
    entry: str
    author: str | None = None
    license: str | None = Field(default=None, alias="licence")
    files: list[str] = Field(default_factory=list)
    requires: RequiresSpec = Field(default_factory=RequiresSpec)
    secrets: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)
    policy: str | None = None
    fixtures: str | None = None
    docs: str | None = None
    budget: PackageBudget | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        text = value.strip()
        if not _NAME.fullmatch(text) or len(text) > MAX_NAME:
            raise ValueError("name must be 1–64 lowercase a-z, digits, hyphens")
        return text

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        text = value.strip()
        if not _SEMVER.fullmatch(text):
            raise ValueError(f"version is not semver: {value}")
        return text

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        text = value.strip()
        if not text or len(text) > MAX_DESCRIPTION:
            raise ValueError("description must be 1–1024 characters")
        return text

    @field_validator("entry", "policy", "fixtures", "docs")
    @classmethod
    def _rel_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _relative(value)

    @field_validator("files")
    @classmethod
    def _files(cls, value: list[str]) -> list[str]:
        return [_relative(item) for item in value]

    @field_validator("secrets")
    @classmethod
    def _secrets(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for item in value:
            text = str(item).strip()
            if "=" in text or ":" in text:
                raise ValueError("secrets must be names only, never values")
            if not _SECRET_NAME.fullmatch(text):
                raise ValueError(f"secret name refused: {item}")
            out.append(text)
        return out

    @field_validator("inputs")
    @classmethod
    def _inputs(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for item in value:
            text = str(item).strip()
            if not text or any(ch in text for ch in "=/\\:"):
                raise ValueError(f"input name refused: {item}")
            out.append(text)
        return out

    def member_paths(self) -> list[str]:
        paths = [MANIFEST_NAME, self.entry, *self.files]
        for extra in (self.policy, self.fixtures, self.docs):
            if extra:
                paths.append(extra)
        seen: set[str] = set()
        ordered: list[str] = []
        for path in paths:
            if path not in seen:
                seen.add(path)
                ordered.append(path)
        return ordered


def load_manifest(path: Path | str) -> PackageManifest:
    file = Path(path)
    if file.is_dir():
        file = file / MANIFEST_NAME
    if not file.is_file():
        raise PackageRefused(f"package manifest not found: {file}", reason="missing")
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as extra:
        raise PackageRefused(f"package manifest unreadable: {file}", reason="unreadable") from extra
    return parse_manifest_text(text, source=file)


def parse_manifest_text(text: str, *, source: Path | str | None = None) -> PackageManifest:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as extra:
        raise PackageRefused(
            f"package manifest is not YAML: {extra}", reason="malformed"
        ) from extra
    return parse_manifest(data, source=source)


def parse_manifest(data: Any, *, source: Path | str | None = None) -> PackageManifest:
    if not isinstance(data, dict):
        raise PackageRefused("package manifest must be a mapping", reason="malformed")
    try:
        return PackageManifest.model_validate(data)
    except ValidationError as extra:
        shown = source if source is not None else MANIFEST_NAME
        msg = "; ".join(err["msg"] for err in extra.errors()[:4])
        reason = "unknown_key" if _unknown_key(extra) else "malformed"
        raise PackageRefused(f"package manifest refused ({shown}): {msg}", reason=reason) from extra


def _unknown_key(extra: ValidationError) -> bool:
    return any(err.get("type") == "extra_forbidden" for err in extra.errors())


def _relative(value: str) -> str:
    text = str(value).strip().replace("\\", "/")
    if not text:
        raise ValueError("path must be non-empty")
    if text.startswith("/") or text.startswith("~") or re.match(r"^[A-Za-z]:/", text):
        raise ValueError(f"absolute path refused: {value}")
    parts = Path(text).parts
    if ".." in parts or parts[:1] == (".",) and len(parts) == 1:
        raise ValueError(f"path refused: {value}")
    if not _REL.fullmatch(text):
        raise ValueError(f"path refused: {value}")
    return text


def parse_core_minimum(spec: str) -> tuple[int, int, int]:
    text = spec.strip()
    if text.startswith(">="):
        text = text[2:]
    bits = [int(p) for p in text.split(".")]
    while len(bits) < 3:
        bits.append(0)
    return bits[0], bits[1], bits[2]


def version_tuple(version: str) -> tuple[int, int, int]:
    bits = [int(p) for p in version.split(".")[:3]]
    while len(bits) < 3:
        bits.append(0)
    return bits[0], bits[1], bits[2]


def satisfies_core(have: str, spec: str | None) -> bool:
    if not spec:
        return True
    return version_tuple(have) >= parse_core_minimum(spec)
