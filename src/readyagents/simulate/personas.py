"""Opt-in model-assisted personas. Metered, capped, refused in sovereign mode."""

from __future__ import annotations

from typing import Any

from readyagents.errors import SimulateRefused
from readyagents.simulate.generate import SimCase
from readyagents.workflow.schema import WorkflowSpec


def generate_personas(
    workflow: WorkflowSpec,
    *,
    model: str,
    personas: list[str],
    llm: Any,
    max_spend: float | None,
    sovereign: bool,
) -> tuple[list[SimCase], float]:
    if sovereign:
        raise SimulateRefused(
            "model-assisted simulation is refused in sovereign mode",
            reason="sovereign",
        )
    if llm is None:
        raise SimulateRefused("model-assisted simulation requires an LLM", reason="missing")
    names = list(workflow.required_inputs) or list(workflow.input_defaults().keys())
    out: list[SimCase] = []
    spend = 0.0
    for persona in personas:
        token = str(persona).strip() or "user"
        if max_spend is not None and spend >= max_spend:
            break
        prompt = (
            f"Persona {token}. Propose JSON object inputs for workflow "
            f"{workflow.name} with keys {names}. Reply with JSON only."
        )
        result = llm.complete(_messages(prompt), model=model)
        usage = getattr(result, "usage", None) or {}
        spend += _cost_usd(usage)
        text = getattr(result, "text", None) or "{}"
        parsed = _parse_json_object(text)
        if parsed is None:
            parsed = {names[0]: f"{token}:{text[:80]}"} if names else {}
        out.append(SimCase(name=f"persona-{token}", inputs=parsed, tag="persona"))
    return out, spend


def _messages(prompt: str) -> list[Any]:
    from readyagents.llm.base import Message

    return [Message(role="user", content=prompt)]


def _cost_usd(usage: Any) -> float:
    if not isinstance(usage, dict):
        return 0.0
    if usage.get("cost_usd") is not None:
        return float(usage["cost_usd"])
    micros = usage.get("cost_micros")
    if micros is not None:
        return float(micros) / 1_000_000.0
    return 0.0


def _parse_json_object(text: str) -> dict[str, Any] | None:
    import json

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
