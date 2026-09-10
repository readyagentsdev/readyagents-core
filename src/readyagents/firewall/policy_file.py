"""Load and validate a declarative tool policy. Fail closed on errors."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from readyagents.errors import PolicyError

Action = Literal["allow", "gate", "deny"]


class _Forbid(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolRule(_Forbid):
    on_tainted: Action = "allow"
    on_description_change: Action | None = None
    allow_hosts: list[str] | None = None
    paths: list[str] | None = None
    quarantine: bool = False
    network: bool = False


class InjectionRule(_Forbid):
    threshold: float = 0.7
    on_match: Action = "gate"


class DetectionBlock(_Forbid):
    injection: InjectionRule = Field(default_factory=InjectionRule)


class NodeRule(_Forbid):
    require_approval: bool = False


class EgressBlock(_Forbid):
    allow_hosts: list[str] | None = None


class Policy(_Forbid):
    version: int = 1
    default: Literal["allow", "deny"] = "allow"
    egress: EgressBlock | None = None
    tools: dict[str, ToolRule] = Field(default_factory=dict)
    detection: DetectionBlock | None = None
    nodes: dict[str, NodeRule] = Field(default_factory=dict)
    require_signed: bool = False
    frozen: bool = False
    on_lock_mismatch: Action = "allow"
    source: str | None = None

    def tool_rule(self, name: str) -> tuple[str, ToolRule | None]:
        """Return (rule_id, rule). Longest glob wins after exact match."""
        if name in self.tools:
            return name, self.tools[name]
        matches: list[tuple[int, str, ToolRule]] = []
        for key, rule in self.tools.items():
            if _name_matches(key, name):
                matches.append((len(key), key, rule))
        if not matches:
            return "default", None
        matches.sort(reverse=True)
        _length, key, rule = matches[0]
        return key, rule


def _name_matches(pattern: str, name: str) -> bool:
    from fnmatch import fnmatch

    if pattern == name:
        return True
    if pattern in {"mcp:*", "mcp:**"} and "." in name:
        return True
    return fnmatch(name, pattern)


def resolve_policy(
    *,
    explicit: str | Path | None = None,
    workflow_dir: Path | None = None,
    env: dict[str, str] | None = None,
    stored: str | Path | None = None,
) -> Path | None:
    """`--policy`, then READYAGENTS_POLICY, then a stored run path, then beside the workflow."""
    environ = env if env is not None else os.environ
    if explicit is not None:
        path = Path(explicit)
        if not path.is_file():
            raise PolicyError(f"Policy file not found: {path}")
        return path
    raw = (environ.get("READYAGENTS_POLICY") or "").strip()
    if raw:
        path = Path(raw)
        if not path.is_file():
            raise PolicyError(f"READYAGENTS_POLICY file not found: {path}")
        return path
    if stored is not None and str(stored).strip():
        path = Path(str(stored).strip()).expanduser()
        if not path.is_file():
            raise PolicyError(f"Stored policy file not found: {path}")
        return path
    if workflow_dir is not None:
        beside = Path(workflow_dir) / "readyagents.policy.yaml"
        if beside.is_file():
            return beside
    return None


def load_policy(path: Path | str) -> Policy:
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError(f"Policy file unreadable: {file}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyError(f"Policy file is not valid YAML: {file}: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise PolicyError(f"Policy file must be a mapping: {file}")
    try:
        policy = Policy.model_validate(data)
    except ValidationError as exc:
        raise PolicyError(f"Policy file is invalid: {file}: {exc}") from exc
    if policy.version != 1:
        raise PolicyError(f"Unsupported policy version {policy.version} (expected 1)")
    if policy.default not in {"allow", "deny"}:
        raise PolicyError(f"Policy default must be allow or deny, not {policy.default!r}")
    policy.source = str(file.expanduser().resolve())
    return policy


def load_resolved(
    *,
    explicit: str | Path | None = None,
    workflow_dir: Path | None = None,
    env: dict[str, str] | None = None,
    stored: str | Path | None = None,
) -> Policy | None:
    path = resolve_policy(explicit=explicit, workflow_dir=workflow_dir, env=env, stored=stored)
    if path is None:
        return None
    return load_policy(path)
