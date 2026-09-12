"""Prompt only for missing declared fields. Never ask derived facts."""

from __future__ import annotations

from typing import Any

from readyagents.config import Settings, get_settings
from readyagents.errors import RegistryRefused
from readyagents.registry.schema import (
    DECLARED_FIELDS,
    OwnerRole,
    ReviewSpec,
    validate_data_classes,
    validate_tier,
)
from readyagents.registry.store import load_config, load_declared, save_declared
from readyagents.workflow.state import utc_now


def annotate(
    agent_id: str,
    updates: dict[str, Any] | None = None,
    *,
    settings: Settings | None = None,
    authorizer: Any = None,
    actor: str | None = None,
    snapshot: dict[str, Any] | None = None,
) -> list[str]:
    """Apply updates for missing declared fields only. Returns still-missing names."""
    settings = settings or get_settings()
    if authorizer is not None:
        authorizer.check(actor, "registry.annotate", agent_id)
    declared = load_declared(agent_id, settings)
    if declared is None:
        raise RegistryRefused(f"unknown agent {agent_id}", reason="missing")
    cfg = load_config(settings)
    incoming = dict(updates or {})
    for key in list(incoming):
        if key not in DECLARED_FIELDS:
            raise RegistryRefused(f"not a declared field: {key}", reason="field")
    missing = set(declared.missing_fields())
    data = declared.model_dump()
    applied_review = False
    for key, value in incoming.items():
        refresh_review = key == "review" and _has_last(value)
        if key not in missing and not refresh_review:
            continue
        if key in {"owner", "backup_owner"}:
            role = value.get("role") if isinstance(value, dict) else value
            data[key] = OwnerRole(role=str(role)).model_dump()
        elif key == "risk_tier":
            data[key] = validate_tier(str(value))
        elif key == "data_classes":
            raw = value if isinstance(value, list) else [v.strip() for v in str(value).split(",")]
            data[key] = validate_data_classes(
                [str(v) for v in raw if str(v).strip()], cfg.data_classes
            )
        elif key == "review":
            spec = _review_value(value)
            data[key] = spec
            applied_review = True
        else:
            data[key] = value
    if applied_review:
        data["review_snapshot"] = (
            snapshot if snapshot is not None else _current_snapshot(agent_id, settings)
        )
    updated = type(declared).model_validate(data)
    save_declared(updated, settings)
    return updated.missing_fields()


def _has_last(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(value.get("last"))
    return False


def _review_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        spec = ReviewSpec.model_validate(value)
        if not spec.last:
            spec = ReviewSpec(cadence=spec.cadence, last=utc_now()[:10])
        return spec.model_dump()
    return ReviewSpec(cadence=str(value), last=utc_now()[:10]).model_dump()


def _current_snapshot(agent_id: str, settings: Settings) -> dict[str, Any]:
    from readyagents.registry.scan import get_entry

    entry = get_entry(agent_id, settings=settings)
    if entry is None:
        return {}
    return entry.derived.drift_key()
