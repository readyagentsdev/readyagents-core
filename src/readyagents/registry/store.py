"""Registry config, index, and declared metadata on disk."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from readyagents.atomic import atomic_write_text
from readyagents.config import Settings, get_settings
from readyagents.errors import RegistryRefused
from readyagents.registry.schema import TIERS, DeclaredAgent, RegistryConfig, TierRequirements


def registry_dir(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    path = settings.home_path() / "registry"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path(settings: Settings | None = None) -> Path:
    return registry_dir(settings) / "config.yaml"


def index_path(settings: Settings | None = None) -> Path:
    return registry_dir(settings) / "index.json"


def load_config(settings: Settings | None = None) -> RegistryConfig:
    path = config_path(settings)
    if not path.is_file():
        return RegistryConfig()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise RegistryRefused("registry config must be a mapping", reason="malformed")
    try:
        cfg = RegistryConfig.model_validate(raw)
    except Exception as extra:
        raise RegistryRefused(f"invalid registry config: {extra}", reason="malformed") from extra
    if not cfg.tiers:
        cfg.tiers = {
            "high": TierRequirements(
                approval_gate=True,
                evidence_pack=True,
                signed_release=True,
                min_review_days=90,
            ),
            "medium": TierRequirements(min_review_days=180),
            "low": TierRequirements(),
        }
    for name in cfg.tiers:
        if name not in TIERS:
            raise RegistryRefused(f"unknown risk tier {name!r} in config", reason="tier")
    return cfg


def save_config(cfg: RegistryConfig, settings: Settings | None = None) -> None:
    path = config_path(settings)
    atomic_write_text(
        path,
        yaml.safe_dump(cfg.model_dump(mode="python"), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )


def load_index(settings: Settings | None = None) -> dict[str, Any]:
    path = index_path(settings)
    if not path.is_file():
        return {"agents": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"agents": {}}
    if not isinstance(raw, dict):
        return {"agents": {}}
    agents = raw.get("agents")
    if not isinstance(agents, dict):
        raw["agents"] = {}
    return raw


def save_index(index: dict[str, Any], settings: Settings | None = None) -> None:
    path = index_path(settings)
    atomic_write_text(
        path,
        json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def agent_file(agent_id: str, settings: Settings | None = None) -> Path:
    token = str(agent_id or "").strip()
    if not token or "/" in token or ".." in token:
        raise RegistryRefused("invalid agent id", reason="id")
    folder = registry_dir(settings) / "agents"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{token}.yaml"


def load_declared(agent_id: str, settings: Settings | None = None) -> DeclaredAgent | None:
    path = agent_file(agent_id, settings)
    if not path.is_file():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise RegistryRefused(f"declared metadata for {agent_id} is malformed", reason="malformed")
    return DeclaredAgent.model_validate(raw)


def save_declared(declared: DeclaredAgent, settings: Settings | None = None) -> Path:
    path = agent_file(declared.agent_id, settings)
    atomic_write_text(
        path,
        yaml.safe_dump(declared.model_dump(mode="python"), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )
    return path
