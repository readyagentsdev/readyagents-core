"""Per-tool credential grants. Separate from the firewall policy file."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from readyagents.errors import ConfigError

ENV_CREDENTIALS = "READYAGENTS_CREDENTIALS"
MAX_FILE_BYTES = 1_048_576


class _Forbid(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolGrant(_Forbid):
    secrets: list[str] = Field(default_factory=list)
    ttl: str | None = None

    @field_validator("secrets")
    @classmethod
    def _secrets(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in value:
            name = str(raw).strip()
            if not name:
                raise ValueError("secret names must be non-empty")
            if name in seen:
                continue
            seen.add(name)
            out.append(name)
        return out


class CredentialsPolicy(_Forbid):
    version: int = 1
    credentials: dict[str, ToolGrant] = Field(default_factory=dict)
    source: str | None = None

    def grant_for(self, tool: str) -> ToolGrant:
        if tool in self.credentials:
            return self.credentials[tool]
        from fnmatch import fnmatch

        matches = [
            (len(key), grant) for key, grant in self.credentials.items() if fnmatch(tool, key)
        ]
        if not matches:
            return ToolGrant(secrets=[])
        matches.sort(reverse=True)
        return matches[0][1]

    def managed_names(self) -> set[str]:
        names: set[str] = set()
        for grant in self.credentials.values():
            names.update(grant.secrets)
        return names


def resolve_credentials_path(
    *,
    explicit: str | Path | None = None,
    workflow_dir: Path | None = None,
    env: dict[str, str] | None = None,
) -> Path | None:
    environ = env if env is not None else os.environ
    if explicit is not None:
        path = Path(explicit)
        if not path.is_file():
            raise ConfigError(f"Credentials file not found: {path}")
        return path
    raw = (environ.get(ENV_CREDENTIALS) or "").strip()
    if raw:
        path = Path(raw)
        if not path.is_file():
            raise ConfigError(f"{ENV_CREDENTIALS} file not found: {path}")
        return path
    if workflow_dir is not None:
        beside = Path(workflow_dir) / "readyagents.credentials.yaml"
        if beside.is_file():
            return beside
    return None


def load_credentials_policy(path: Path | str) -> CredentialsPolicy:
    file = Path(path)
    try:
        size = file.stat().st_size
    except OSError as exc:
        raise ConfigError(f"Credentials file unreadable: {file}: {exc}") from exc
    if size > MAX_FILE_BYTES:
        raise ConfigError(f"Credentials file {file} is {size} bytes; max is {MAX_FILE_BYTES}")
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Credentials file unreadable: {file}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as extra:
        raise ConfigError(f"Credentials file {file} is not valid YAML: {extra}") from extra
    if not isinstance(data, dict):
        raise ConfigError(f"Credentials file {file} must be a mapping")
    try:
        policy = CredentialsPolicy.model_validate(data)
    except ValidationError as extra:
        raise ConfigError(f"Credentials file {file} is malformed: {extra}") from extra
    if policy.version != 1:
        raise ConfigError(
            f"Credentials file {file}: unsupported version {policy.version} (expected 1)"
        )
    policy.source = str(file)
    return policy
