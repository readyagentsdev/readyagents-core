"""Declared per-class recovery. Nothing is inferred. Every adaptation is recorded."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from readyagents.health.fingerprint import classify_failure
from readyagents.health.layout import FAILURE_CLASSES, RECOVERY_ACTIONS
from readyagents.workflow.state import utc_now


@dataclass(frozen=True)
class RecoveryMatch:
    klass: str
    action: str
    max_tokens: int | None = None
    seconds: float | None = None
    max_repairs: int | None = None


def match_recovery(spec: Any, error: BaseException) -> RecoveryMatch | None:
    """Return the declared action for this failure class, or None (fall through)."""
    on = list(getattr(spec, "on", None) or [])
    klass = classify_failure(type(error).__name__, str(error))
    if klass not in FAILURE_CLASSES:
        return None
    for row in on:
        declared = str(getattr(row, "class_", None) or getattr(row, "klass", None) or "")
        if not declared:
            raw = getattr(row, "model_dump", None)
            data = raw(by_alias=True) if callable(raw) else {}
            declared = str(data.get("class") or "")
        if declared.strip().lower() != klass:
            continue
        action = str(getattr(row, "action", "") or "").strip().lower()
        if action not in RECOVERY_ACTIONS:
            return None
        seconds = getattr(row, "seconds", None)
        max_tokens = getattr(row, "max_tokens", None)
        max_repairs = getattr(row, "max_repairs", None)
        return RecoveryMatch(
            klass=klass,
            action=action,
            max_tokens=int(max_tokens) if max_tokens is not None else None,
            seconds=float(seconds) if seconds is not None else None,
            max_repairs=int(max_repairs) if max_repairs is not None else None,
        )
    return None


def apply_recovery(ctx: Any, match: RecoveryMatch) -> dict[str, Any]:
    """Mutate execution context for the next attempt. Recordable payload only."""
    note: dict[str, Any] = {
        "class": match.klass,
        "action": match.action,
        "at": utc_now(),
    }
    if match.action == "retry_with" and match.max_tokens is not None:
        current = getattr(ctx, "budget_tokens", None)
        ctx.budget_tokens = max(int(current or 0), int(match.max_tokens))
        note["max_tokens"] = int(match.max_tokens)
    elif match.action == "fallback":
        ctx.recovery_force_fallback = True
        note["fallback"] = True
    elif match.action == "repair":
        used = int(getattr(ctx, "recovery_repairs", 0) or 0)
        cap = int(match.max_repairs or 1)
        ctx.recovery_repairs = used + 1
        ctx.recovery_repair = ctx.recovery_repairs <= cap
        note["repair"] = ctx.recovery_repair
        note["max_repairs"] = cap
    elif match.action == "backoff":
        note["seconds"] = float(match.seconds or 0.0)
    return note


def record_adaptation(state: Any, node_id: str, note: dict[str, Any]) -> None:
    bucket = state.metadata.setdefault("recovery", [])
    if not isinstance(bucket, list):
        state.metadata["recovery"] = [note]
        return
    row = dict(note)
    row["node_id"] = node_id
    bucket.append(row)
