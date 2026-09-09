"""Accuracy / error-band checks for TokenOps estimates vs recorded usage."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from readyagents.cost.estimate import estimate_workflow_file
from readyagents.cost.tokens import HEURISTIC_BAND, MEASURED_BAND
from readyagents.testing import ScriptedLLM
from readyagents.workflow.runner import run_workflow_file

ROOT = Path(__file__).resolve().parents[1]
COST_DOC = ROOT / "docs" / "cost.md"


def _write_workflow(tmp_path: Path, spec: dict, name: str = "wf.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    return path


def _simple_agent_pair() -> dict:
    return {
        "name": "tokenops-accuracy",
        "start": "a",
        "nodes": [
            {
                "id": "a",
                "type": "agent",
                "prompt": "first prompt for pricing",
                "model": "openai:gpt-4o-mini",
                "next": "b",
            },
            {
                "id": "b",
                "type": "agent",
                "prompt": "second prompt for pricing",
                "model": "openai:gpt-4o-mini",
            },
        ],
    }


def _assert_recorded_inside_estimate(recorded_total: int, floor: int, ceiling: int) -> None:
    if floor <= recorded_total <= ceiling:
        return
    raise AssertionError(
        f"recorded token total {recorded_total} falls outside estimate range "
        f"[{floor}, {ceiling}] (floor_tokens / ceiling_tokens from "
        f"estimate_workflow_file)"
    )


def test_documented_error_bands_match_constants() -> None:
    assert HEURISTIC_BAND == 0.50
    assert MEASURED_BAND == 0.10
    text = COST_DOC.read_text(encoding="utf-8")
    assert "±50%" in text or "+-50%" in text
    assert "±10%" in text or "+-10%" in text
    assert "provider invoice is" in text.lower() and "authoritative" in text.lower()


def test_cost_doc_honest_numbers() -> None:
    text = COST_DOC.read_text(encoding="utf-8")
    lower = text.lower()
    assert "provider invoice is" in lower and "authoritative" in lower
    assert "not accurate to the cent" in lower
    assert "unpriced" in lower
    assert "never a silent" in lower and "$0" in text
    assert "unpriced is not free" in lower or "unpriced is not zero" in lower
    assert "tokenizer" in lower and "tiktoken" in lower
    assert (
        "not** in `all`" in text
        or "not in `all`" in lower
        or ("tokenizer" in lower and "**not** in `all`" in text)
    )


def test_estimate_assumptions_carry_documented_band(tmp_path: Path) -> None:
    path = _write_workflow(tmp_path, _simple_agent_pair())
    result = estimate_workflow_file(path)
    band = MEASURED_BAND if result.measured else HEURISTIC_BAND
    pct = int(band * 100)
    assert any(f"±{pct}%" in item for item in result.assumptions)
    assert any("invoice is authoritative" in item for item in result.assumptions)
    assert result.floor_tokens > 0
    assert result.ceiling_tokens >= result.floor_tokens


def test_recorded_scripted_usage_inside_estimate_range(tmp_path: Path, tmp_settings) -> None:
    """Known ScriptedLLM usage must land in the shipped estimate [floor, ceiling]."""
    path = _write_workflow(tmp_path, _simple_agent_pair())
    estimate = estimate_workflow_file(path)
    floor = estimate.floor_tokens
    ceiling = estimate.ceiling_tokens
    assert ceiling >= floor >= 1

    # Recorded prompt/completion totals chosen to sit inside the preflight range
    # (floor assumes small completions; ceiling assumes large ones / retries).
    per_call = {"prompt_tokens": 40, "completion_tokens": 80, "total_tokens": 120}
    llm = ScriptedLLM()
    llm.enqueue("alpha", model="gpt-4o-mini", usage=per_call)
    llm.enqueue("beta", model="gpt-4o-mini", usage=per_call)

    state = run_workflow_file(path, llm=llm, settings=tmp_settings, persist=False)
    assert state.status == "succeeded"
    recorded = int(state.usage.get("total_tokens") or 0)
    if recorded == 0:
        recorded = int(state.usage.get("prompt_tokens") or 0) + int(
            state.usage.get("completion_tokens") or 0
        )
    assert recorded == 240
    _assert_recorded_inside_estimate(recorded, floor, ceiling)


def test_out_of_band_recorded_usage_fails_clearly(tmp_path: Path) -> None:
    path = _write_workflow(tmp_path, _simple_agent_pair())
    estimate = estimate_workflow_file(path)
    absurd = estimate.ceiling_tokens + 10_000
    with pytest.raises(AssertionError, match="falls outside estimate range"):
        _assert_recorded_inside_estimate(absurd, estimate.floor_tokens, estimate.ceiling_tokens)
