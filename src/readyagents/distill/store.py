"""Distill config, dataset dirs, adapter index, and promoted pins."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from readyagents.atomic import atomic_write_text
from readyagents.config import Settings, get_settings
from readyagents.distill.schema import AdapterRecord, DistillConfig
from readyagents.errors import DistillRefused
from readyagents.permissions import restrict_file


def distill_dir(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    path = settings.home_path() / "distill"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_config(settings: Settings | None = None) -> DistillConfig:
    path = distill_dir(settings) / "config.yaml"
    if not path.is_file():
        return DistillConfig()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise DistillRefused("distill config must be a mapping", reason="malformed")
    try:
        return DistillConfig.model_validate(raw)
    except Exception as extra:
        raise DistillRefused(f"invalid distill config: {extra}", reason="malformed") from extra


def save_config(cfg: DistillConfig, settings: Settings | None = None) -> None:
    path = distill_dir(settings) / "config.yaml"
    atomic_write_text(
        path,
        yaml.safe_dump(cfg.model_dump(mode="python"), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )


def adapters_dir(settings: Settings | None = None) -> Path:
    path = distill_dir(settings) / "adapters"
    path.mkdir(parents=True, exist_ok=True)
    return path


def adapter_folder(adapter_id: str, settings: Settings | None = None) -> Path:
    token = str(adapter_id or "").strip()
    if not token or "/" in token or ".." in token:
        raise DistillRefused("invalid adapter id", reason="id")
    dest = adapters_dir(settings) / token
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def save_adapter(record: AdapterRecord, settings: Settings | None = None) -> Path:
    folder = adapter_folder(record.id, settings)
    path = folder / "record.json"
    atomic_write_text(
        path,
        json.dumps(record.model_dump(mode="python", by_alias=True), indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    restrict_file(path)
    return path


def load_adapter(adapter_id: str, settings: Settings | None = None) -> AdapterRecord:
    path = adapter_folder(adapter_id, settings) / "record.json"
    if not path.is_file():
        raise DistillRefused(f"unknown adapter {adapter_id}", reason="missing")
    raw = json.loads(path.read_text(encoding="utf-8"))
    return AdapterRecord.model_validate(raw)


def list_adapters(settings: Settings | None = None) -> list[AdapterRecord]:
    root = adapters_dir(settings)
    rows: list[AdapterRecord] = []
    for folder in sorted(root.iterdir()):
        rec = folder / "record.json"
        if not rec.is_file():
            continue
        try:
            rows.append(AdapterRecord.model_validate(json.loads(rec.read_text(encoding="utf-8"))))
        except Exception:  # noqa: BLE001
            continue
    return rows


def promoted_path(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.home_path() / "distill" / "promoted.json"


def load_promoted(settings: Settings | None = None) -> dict[str, Any]:
    path = promoted_path(settings)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return raw if isinstance(raw, dict) else {}


def save_promoted(pins: dict[str, Any], settings: Settings | None = None) -> None:
    path = promoted_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        json.dumps(pins, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def pin_for(node_id: str, settings: Settings | None = None) -> dict[str, Any] | None:
    pins = load_promoted(settings)
    row = pins.get(str(node_id))
    return row if isinstance(row, dict) else None
