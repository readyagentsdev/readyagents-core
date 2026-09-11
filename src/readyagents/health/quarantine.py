"""Fail-safe quarantine: gate to a human, never skip the node."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from readyagents.errors import ApprovalRequired, ConfigError
from readyagents.health.layout import BELOW_GATE, HARD_MAX_WINDOW
from readyagents.health.query import node_samples
from readyagents.health.score import score_node


@dataclass(frozen=True)
class QuarantineDecision:
    action: str
    node_id: str
    success_rate: float
    threshold: float
    fallback: str | None = None
    reason: str = ""


def evaluate_quarantine(
    store: Any,
    node: Any,
    *,
    workflow: str,
    current_run_id: str | None = None,
) -> QuarantineDecision | None:
    """Return a gate/fallback decision when health is below the declared threshold.

    ``below`` other than ``gate`` is refused. Untrusted (non-literal) thresholds
    are refused. Never returns skip/open.
    """
    recovery = getattr(node, "recovery", None)
    health = getattr(recovery, "health", None) if recovery is not None else None
    if health is None:
        return None
    below = str(getattr(health, "below", BELOW_GATE) or BELOW_GATE).strip().lower()
    if below != BELOW_GATE:
        raise ConfigError("health.below must be gate; skip/open would fail open")
    threshold = getattr(health, "min_success_rate", None)
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        raise ConfigError("health.min_success_rate must be a numeric literal")
    window = int(getattr(health, "window", 1) or 1)
    if window < 1 or window > HARD_MAX_WINDOW:
        raise ConfigError(f"health.window must be 1..{HARD_MAX_WINDOW}")
    fallback = getattr(health, "fallback", None)
    fallback_id = str(fallback).strip() if fallback else None
    samples = node_samples(
        store,
        workflow=workflow,
        node_id=str(node.id),
        window=window,
        exclude_run_id=current_run_id,
    )
    scored = score_node(samples, node_id=str(node.id), window=window)
    if scored.samples < 1:
        return None
    if scored.success_rate >= float(threshold):
        return None
    reason = (
        f"Node '{node.id}' success_rate {scored.success_rate} "
        f"< min_success_rate {float(threshold)} over window {window}"
    )
    if fallback_id:
        return QuarantineDecision(
            action="fallback",
            node_id=str(node.id),
            success_rate=scored.success_rate,
            threshold=float(threshold),
            fallback=fallback_id,
            reason=reason,
        )
    return QuarantineDecision(
        action="gate",
        node_id=str(node.id),
        success_rate=scored.success_rate,
        threshold=float(threshold),
        reason=reason,
    )


def raise_gate(decision: QuarantineDecision, state: Any) -> None:
    raise ApprovalRequired(
        decision.node_id,
        getattr(state, "run_id", "") or "",
        decision.reason,
        state=state,
        pause={
            "type": "approval",
            "reason": "health_gate",
            "success_rate": decision.success_rate,
            "threshold": decision.threshold,
        },
    )
