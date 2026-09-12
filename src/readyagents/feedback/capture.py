"""Record human edits and weak implicit signals on the run. Additive."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from readyagents.errors import FeedbackRefused
from readyagents.feedback.diff import structured_diff
from readyagents.feedback.layout import HUMAN, IMPLICIT, SIGNALS
from readyagents.feedback.record import Correction
from readyagents.workflow.state import utc_now


def record_human_correction(
    state: Any,
    *,
    node: Any,
    actor_role: str,
    reason: str,
    original: Any,
    edited: Any | None,
    rating: int | None = None,
    label: str | None = None,
    decision_id: str,
    model: str = "",
) -> Correction:
    spec = getattr(node, "feedback", None)
    if spec is None:
        raise FeedbackRefused("gate has no feedback block", reason="feedback")
    if edited is not None and not bool(getattr(spec, "allow_edit", False)):
        raise FeedbackRefused("edits are not allowed on this gate", reason="edit")
    allowed_labels = [str(item) for item in list(getattr(spec, "labels", None) or [])]
    if label is not None:
        if label not in allowed_labels:
            raise FeedbackRefused(f"undeclared feedback label {label!r}", reason="label")
    rating_value = _parse_rating(rating, getattr(spec, "rating", None))
    text_original = "" if original is None else str(original)
    text_edited = text_original if edited is None else str(edited)
    corr = Correction(
        id=uuid4().hex,
        run_id=str(state.run_id),
        node_id=str(node.id),
        decision_id=str(decision_id),
        kind=HUMAN,
        original=text_original,
        diff=structured_diff(text_original, text_edited),
        role=str(actor_role or ""),
        reason=str(reason or ""),
        rating=rating_value,
        label=label,
        model=str(model or ""),
        ts=utc_now(),
        consent_scope=str(getattr(spec, "consent", None) or ""),
        workflow=str(state.workflow_name or ""),
        inputs=dict(state.inputs or {}),
        source=str((state.metadata or {}).get("source") or ""),
    )
    _append(state, corr)
    _record_consent(state, corr.consent_scope)
    return corr


def record_implicit(
    state: Any,
    *,
    node: Any,
    signal: str,
    reason: str,
    original: Any = None,
    model: str = "",
) -> Correction | None:
    kind = str(signal or "").strip()
    if kind not in SIGNALS:
        return None
    spec = getattr(node, "feedback", None)
    scope = str(getattr(spec, "consent", None) or "") if spec is not None else ""
    corr = Correction(
        id=uuid4().hex,
        run_id=str(state.run_id),
        node_id=str(getattr(node, "id", "") or ""),
        decision_id="",
        kind=IMPLICIT,
        original="" if original is None else str(original),
        diff=[],
        role="",
        reason=str(reason or ""),
        signal=kind,
        model=str(model or ""),
        ts=utc_now(),
        consent_scope=scope,
        workflow=str(state.workflow_name or ""),
        inputs=dict(state.inputs or {}),
        source=str((state.metadata or {}).get("source") or ""),
    )
    _append(state, corr)
    if scope:
        _record_consent(state, scope)
    return corr


def corrections_from_state(state: Any) -> list[Correction]:
    meta = state.metadata if isinstance(getattr(state, "metadata", None), dict) else {}
    rows = meta.get("feedback") or []
    out: list[Correction] = []
    for row in rows:
        if isinstance(row, dict):
            out.append(Correction.from_dict(row))
    return out


def _append(state: Any, corr: Correction) -> None:
    meta = state.metadata if isinstance(state.metadata, dict) else {}
    rows = list(meta.get("feedback") or [])
    rows.append(corr.as_dict())
    meta["feedback"] = rows
    state.metadata = meta


def _record_consent(state: Any, scope: str) -> None:
    if not scope:
        return
    meta = state.metadata if isinstance(state.metadata, dict) else {}
    policy = dict(meta.get("data_policy") or {})
    scopes = [str(item) for item in list(policy.get("scopes") or [])]
    if scope not in scopes:
        scopes.append(scope)
    policy["scopes"] = scopes
    policy["source"] = "recorded"
    meta["data_policy"] = policy
    state.metadata = meta


def _parse_rating(value: int | None, spec: Any) -> int | None:
    if value is None:
        return None
    if spec is None:
        raise FeedbackRefused("rating is not declared on this gate", reason="rating")
    scale = spec
    if hasattr(spec, "scale"):
        scale = spec.scale
    elif isinstance(spec, dict):
        scale = spec.get("scale")
    low, high = 1, 5
    text = str(scale or "1-5")
    if "-" in text:
        left, right = text.split("-", 1)
        try:
            low, high = int(left), int(right)
        except ValueError:
            low, high = 1, 5
    number = int(value)
    if number < low or number > high:
        raise FeedbackRefused(f"rating {number} is outside {low}-{high}", reason="rating")
    return number
