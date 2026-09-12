"""Candidate generation. The only optimize path that constructs a provider."""

from __future__ import annotations

import json
from typing import Any

from readyagents.errors import OptimizeRefused, OptimizeStopSpend
from readyagents.llm.base import Message
from readyagents.optimize.record import ScoreSnapshot
from readyagents.policy import Redactor
from readyagents.prompts.safety import bound_candidate, is_config_shaped
from readyagents.replay.record import redact_value


def generate_candidates(
    *,
    current: str,
    failures: list[dict[str, Any]],
    n: int,
    llm: Any,
    model: str,
    redactor: Any | None,
    spend_usd: float,
    max_spend: float | None,
) -> tuple[list[str], float]:
    """Reflect on redacted failing cases. Never sends a blocked secret."""
    if llm is None:
        raise OptimizeRefused("candidate generation needs a provider", reason="model")
    if max_spend is not None and spend_usd >= max_spend:
        raise OptimizeStopSpend(f"spend budget exhausted ({spend_usd} >= {max_spend})")
    scrubber = redactor if redactor is not None else Redactor()
    payload = redact_value(
        scrubber,
        {
            "current_prompt": current,
            "failing_cases": failures,
            "n": n,
            "instruction": (
                "Propose n improved prompt candidates as a JSON object "
                '{"candidates": ["...", "..."]}. Text only. Not workflow YAML.'
            ),
        },
    )
    messages = [Message(role="user", content=json.dumps(payload, ensure_ascii=False))]
    result = llm.complete(messages, model=model)
    usage = getattr(result, "usage", None) or {}
    spent = _cost_usd(usage, model=model)
    new_spend = spend_usd + spent
    text = getattr(result, "text", None) or ""
    names = _parse_candidates(text, n=n)
    return names, new_spend


def bind_generator(llm: Any, model: str | None, settings: Any) -> tuple[Any, str]:
    """Construct a provider only here. Scoring never calls this."""
    if llm is not None:
        return llm, model or "scripted"
    if not model:
        raise OptimizeRefused(
            "candidate generation needs --model (scoring is cassette/eval only)",
            reason="model",
        )
    from readyagents.llm.registry import get_provider

    provider, model_id = get_provider(model, settings=settings)
    return provider, model_id


def failure_payload(snapshot: ScoreSnapshot) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in snapshot.failed_names:
        rows.append({"name": name, "reason": snapshot.reasons.get(name, "")})
    return rows


def _parse_candidates(text: str, *, n: int) -> list[str]:
    blob = text.strip()
    start = blob.find("{")
    end = blob.rfind("}")
    items: list[Any] = []
    if start >= 0 and end > start:
        try:
            data = json.loads(blob[start : end + 1])
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            items = list(data.get("candidates") or [])
        elif isinstance(data, list):
            items = data
    if not items:
        arr_start = blob.find("[")
        arr_end = blob.rfind("]")
        if arr_start >= 0 and arr_end > arr_start:
            try:
                parsed = json.loads(blob[arr_start : arr_end + 1])
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                items = parsed
    out: list[str] = []
    for raw in items:
        if not isinstance(raw, str):
            continue
        bounded = bound_candidate(raw)
        if is_config_shaped(bounded):
            # still stored as data later; keep it so tests can prove inert storage
            out.append(bounded)
        else:
            out.append(bounded)
        if len(out) >= n:
            break
    return out


def _cost_usd(usage: Any, *, model: str = "") -> float:
    if not isinstance(usage, dict):
        return 0.0
    if usage.get("cost_usd") is not None:
        return float(usage["cost_usd"])
    if "cost_micros" in usage:
        return float(usage.get("cost_micros") or 0) / 1_000_000.0
    from readyagents.llm.resilience import normalize_usage

    normalized = normalize_usage(usage, model=model)
    return float(normalized.get("cost_micros") or 0) / 1_000_000.0
