"""Consent is a recorded run policy, never a CLI flag."""

from __future__ import annotations

from typing import Any


def recorded_scopes(state: Any) -> list[str]:
    meta = state.metadata if isinstance(getattr(state, "metadata", None), dict) else {}
    policy = meta.get("data_policy") or {}
    if not isinstance(policy, dict):
        return []
    return [str(item) for item in list(policy.get("scopes") or []) if str(item).strip()]


def permits(state: Any, scope: str | None) -> bool:
    scopes = recorded_scopes(state)
    if not scopes:
        return False
    if not scope:
        return True
    return str(scope) in scopes
