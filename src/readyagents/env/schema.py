"""Environment config schema. Scopes only — secret values are refused."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from readyagents.errors import EnvRefused
from readyagents.workflow.schema import BudgetSpec

ENV_FILENAME = "readyagents.env.yaml"
_SECRET_KEY = re.compile(
    r"(password|secret|token|api[_-]?key|private[_-]?key|authorization)$", re.I
)
_SECRET_VALUE = re.compile(
    r"(sk-[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{8,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


class EnvRollbackSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _yaml_on_key(cls, value: Any) -> Any:
        """YAML 1.1 parses unquoted `on` as boolean True. Accept that spelling."""
        if isinstance(value, dict) and True in value and "on" not in value:
            data = dict(value)
            data["on"] = data.pop(True)
            return data
        return value


class EnvCanarySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    percent: int = Field(default=0, ge=0, le=100)


class EnvShadowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    budget: BudgetSpec | None = None


class EnvGatesSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eval: dict[str, Any] | None = None
    fixtures: dict[str, Any] | None = None
    benchmark: dict[str, Any] | None = None
    health: dict[str, Any] | None = None
    approval: dict[str, Any] | None = None


class EnvironmentSpec(BaseModel):
    """One named environment: configuration bundle, not a server."""

    model_config = ConfigDict(extra="forbid")

    policy: str | None = None
    routing: str | None = None
    budget: BudgetSpec | None = None
    secrets: str | None = Field(default=None, description="Secret *scope* name, never a value.")
    store: str | None = Field(default=None, description="Run-store subdirectory under home.")
    gates: EnvGatesSpec | None = None
    rollback: EnvRollbackSpec | None = None
    canary: EnvCanarySpec | None = None
    shadow: EnvShadowSpec | None = None


class EnvFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    environments: dict[str, EnvironmentSpec] = Field(default_factory=dict)

    @field_validator("version")
    @classmethod
    def _version(cls, value: int) -> int:
        if int(value) != 1:
            raise ValueError("readyagents.env.yaml version must be 1")
        return int(value)


def refuse_secrets(raw: Any, *, path: str = "") -> None:
    """Refuse secret *values* in env config. Scopes and paths are allowed."""
    if isinstance(raw, str):
        if _SECRET_VALUE.search(raw):
            raise EnvRefused(
                f"environment config {path or 'value'} contains a secret value",
                reason="secret",
            )
        return
    if isinstance(raw, dict):
        for key, item in raw.items():
            name = str(key)
            here = f"{path}.{name}" if path else name
            if _SECRET_VALUE.search(name):
                raise EnvRefused(
                    f"environment config {here} contains a secret value",
                    reason="secret",
                )
            if _SECRET_KEY.search(name) and isinstance(item, str) and _looks_like_secret(item):
                raise EnvRefused(
                    f"environment config {here} looks like a secret value; use a scope name",
                    reason="secret",
                )
            refuse_secrets(item, path=here)
        return
    if isinstance(raw, list):
        for index, item in enumerate(raw):
            refuse_secrets(item, path=f"{path}[{index}]")


def _looks_like_secret(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if "/" in text or text.endswith((".yaml", ".yml", ".json")):
        return False
    if _SECRET_VALUE.search(text):
        return True
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", text):
        return False
    return True


def load_env_file(path: Path | str | None = None, *, settings: Any = None) -> EnvFile | None:
    """Load env config if present. Missing file is None (no-env path)."""
    file = Path(path) if path is not None else _discover(settings)
    if file is None or not file.is_file():
        return None
    text = file.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if data is None:
        return EnvFile()
    if not isinstance(data, dict):
        raise EnvRefused("environment config must be a mapping", reason="malformed")
    refuse_secrets(data)
    try:
        return EnvFile.model_validate(data)
    except Exception as extra:
        raise EnvRefused(f"invalid environment config: {extra}", reason="malformed") from extra


def _discover(settings: Any) -> Path | None:
    from readyagents.config import get_settings

    cfg = settings or get_settings()
    workspace = cfg.workspace_path() if hasattr(cfg, "workspace_path") else Path.cwd()
    home = cfg.home_path() if hasattr(cfg, "home_path") else Path.cwd()
    for candidate in (workspace / ENV_FILENAME, home / ENV_FILENAME):
        if candidate.is_file():
            return candidate
    return None
