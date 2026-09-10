from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest

from readyagents.errors import BudgetExceeded, CircuitOpen, LLMError, NodeError
from readyagents.llm.resilience import CircuitBreaker
from readyagents.logging import (
    JsonLogFormatter,
    _RunContextFilter,
    _SafeStreamHandler,
    configure_logging,
    log_event,
)
from readyagents.testing import ScriptedLLM, run_workflow_spec
from readyagents.tools import ToolRegistry
from readyagents.workflow.engine import run_workflow
from readyagents.workflow.nodes import ExecutionContext
from readyagents.workflow.runner import run_workflow_file
from readyagents.workflow.schema import WorkflowSpec


def _two_agents() -> dict:
    return {
        "name": "two-agents",
        "start": "a",
        "nodes": [
            {
                "id": "a",
                "type": "agent",
                "prompt": "first",
                "model": "mock:a",
                "output_key": "one",
                "next": "b",
            },
            {
                "id": "b",
                "type": "agent",
                "prompt": "second",
                "model": "mock:b",
                "output_key": "two",
            },
        ],
    }


def test_json_log_records_include_run_and_node_and_parse() -> None:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.addFilter(_RunContextFilter())
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger("readyagents.testjson")
    logger.handlers[:] = []
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    log_event(logger, "node_start", "hello", run_id="abc123", node_id="gate", status="running")
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    assert lines
    payload = json.loads(lines[-1])
    assert payload["run"] == "abc123"
    assert payload["node"] == "gate"
    assert payload["event"] == "node_start"
    assert payload["message"] == "hello"


def test_safe_stream_handler_closed_stream_does_not_raise() -> None:
    buf = io.StringIO()
    handler = _SafeStreamHandler(buf)
    handler.setLevel(logging.INFO)
    buf.close()
    record = logging.LogRecord(
        "readyagents.test",
        logging.INFO,
        __file__,
        0,
        "closed-stream",
        (),
        None,
    )
    handler.emit(record)
    handler.handleError(record)


def test_configure_json_format_emits_parseable_lines() -> None:
    logger = logging.getLogger("readyagents")
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.addFilter(_RunContextFilter())
    handler.setFormatter(JsonLogFormatter())
    logger.addHandler(handler)
    try:
        configure_logging("INFO", fmt="json")
        log_event(logger, "run_start", "go", run_id="r1", node_id="-")
        text = buf.getvalue()
        assert text.strip()
        row = json.loads(text.strip().splitlines()[-1])
        assert row["run"] == "r1"
        assert "node" in row
    finally:
        logger.removeHandler(handler)
        configure_logging("INFO", fmt="text")


def test_two_agent_nodes_accumulate_different_usage() -> None:
    llm = ScriptedLLM()
    llm.enqueue(
        "alpha",
        model="a",
        usage={"prompt_tokens": 10, "completion_tokens": 5, "cost_micros": 100},
    )
    llm.enqueue(
        "beta",
        model="b",
        usage={"prompt_tokens": 3, "completion_tokens": 7, "cost_micros": 40},
    )
    state = run_workflow_spec(_two_agents(), llm=llm)
    assert state.status == "succeeded"
    usage_a = next(r.usage for r in state.results if r.node_id == "a")
    usage_b = next(r.usage for r in state.results if r.node_id == "b")
    assert usage_a["prompt_tokens"] == 10
    assert usage_a["completion_tokens"] == 5
    assert usage_a["cost_micros"] == 100
    assert usage_b["prompt_tokens"] == 3
    assert usage_b["cost_micros"] == 40
    assert usage_a != usage_b
    assert state.usage["prompt_tokens"] == usage_a["prompt_tokens"] + usage_b["prompt_tokens"]
    assert (
        state.usage["completion_tokens"]
        == usage_a["completion_tokens"] + usage_b["completion_tokens"]
    )
    assert state.usage["total_tokens"] == usage_a["total_tokens"] + usage_b["total_tokens"]
    assert state.usage["cost_micros"] == usage_a["cost_micros"] + usage_b["cost_micros"]
    assert len(llm.calls) == 2


def test_budget_stops_further_llm_calls() -> None:
    llm = ScriptedLLM()
    llm.enqueue(
        "alpha",
        model="a",
        usage={"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
    )
    llm.enqueue("should-not-run", model="b", usage={"prompt_tokens": 1, "completion_tokens": 1})
    spec = WorkflowSpec.model_validate(_two_agents())
    ctx = ExecutionContext(
        spec,
        ToolRegistry(),
        llm=llm,
        default_model="mock:a",
        budget_tokens=10,
    )
    with pytest.raises(BudgetExceeded) as exc:
        run_workflow(spec, {}, ctx)
    assert exc.value.kind == "tokens"
    assert exc.value.used >= 10
    assert len(llm.calls) == 1
    assert exc.value.state is not None
    assert exc.value.state.status == "failed"


def test_model_fallback_then_success() -> None:
    llm = ScriptedLLM()
    llm.enqueue(error=LLMError("primary down"), model="primary")
    llm.enqueue(
        "from-backup",
        model="backup",
        usage={"prompt_tokens": 2, "completion_tokens": 2},
    )
    state = run_workflow_spec(
        {
            "name": "fallback",
            "nodes": [
                {
                    "id": "a",
                    "type": "agent",
                    "prompt": "hi",
                    "model": "mock:primary",
                    "fallback_models": ["mock:backup"],
                    "output_key": "text",
                }
            ],
        },
        llm=llm,
    )
    assert state.status == "succeeded"
    assert state.output_keys["text"] == "from-backup"
    assert [c["model"] for c in llm.calls] == ["primary", "backup"]


def test_circuit_breaker_skips_until_cooldown() -> None:
    clock = {"t": 0.0}

    def now() -> float:
        return clock["t"]

    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=10, clock=now)
    llm = ScriptedLLM()
    spec = WorkflowSpec.model_validate(
        {
            "name": "cb",
            "nodes": [
                {
                    "id": "a",
                    "type": "agent",
                    "prompt": "x",
                    "model": "mock:primary",
                    "output_key": "text",
                }
            ],
        }
    )

    llm.enqueue(error=LLMError("fail-1"), model="primary")
    with pytest.raises(NodeError):
        run_workflow(
            spec,
            {},
            ExecutionContext(spec, ToolRegistry(), llm=llm, circuit_breaker=breaker),
        )
    llm.enqueue(error=LLMError("fail-2"), model="primary")
    with pytest.raises(NodeError):
        run_workflow(
            spec,
            {},
            ExecutionContext(spec, ToolRegistry(), llm=llm, circuit_breaker=breaker),
        )
    calls_after_open = len(llm.calls)
    with pytest.raises(CircuitOpen):
        run_workflow(
            spec,
            {},
            ExecutionContext(spec, ToolRegistry(), llm=llm, circuit_breaker=breaker),
        )
    assert len(llm.calls) == calls_after_open
    clock["t"] = 11.0
    llm.enqueue("recovered", model="primary", usage={"prompt_tokens": 1, "completion_tokens": 1})
    state = run_workflow(
        spec,
        {},
        ExecutionContext(spec, ToolRegistry(), llm=llm, circuit_breaker=breaker),
    )
    assert state.status == "succeeded"
    assert state.output_keys["text"] == "recovered"
    assert len(llm.calls) == calls_after_open + 1


def test_engine_run_emits_parseable_json_with_run_and_node() -> None:
    """JSON lines from a real engine run still include `run` and `node`."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.addFilter(_RunContextFilter())
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger("readyagents")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        spec = WorkflowSpec.model_validate(
            {
                "name": "log-run",
                "nodes": [
                    {
                        "id": "t",
                        "type": "transform",
                        "template": "hello",
                        "output_key": "out",
                    }
                ],
            }
        )
        state = run_workflow(spec, {}, ExecutionContext(spec, ToolRegistry()))
        rows = []
        for line in buf.getvalue().splitlines():
            if not line.strip():
                continue
            rows.append(json.loads(line))
        assert rows, "engine emitted no JSON log lines"
        assert all("run" in row and "node" in row for row in rows)
        assert any(row["run"] == state.run_id for row in rows)
        assert any(row["node"] == "t" for row in rows)
        assert any(row.get("event") in {"run_start", "node_start"} for row in rows)
    finally:
        logger.removeHandler(handler)


def test_dry_run_estimated_tokens_still_compatible() -> None:
    spec = WorkflowSpec.model_validate(
        {
            "name": "dry",
            "nodes": [{"id": "a", "type": "agent", "prompt": "hello world", "output_key": "out"}],
        }
    )
    llm = ScriptedLLM()
    ctx = ExecutionContext(spec, ToolRegistry(), dry_run=True, llm=llm)
    state = run_workflow(spec, {}, ctx)
    assert state.usage.get("estimated_tokens", 0) >= 1
    assert "[dry-run]" in state.output_keys["out"]
    assert "estimated_tokens=" in state.output_keys["out"]
    assert llm.calls == []


def test_include_agent_usage_counted_once(tmp_path: Path, tmp_settings) -> None:
    child = tmp_path / "child.yaml"
    child.write_text(
        """
name: child-agent
start: a
nodes:
  - id: a
    type: agent
    prompt: "hi"
    model: mock:x
    output_key: t
""",
        encoding="utf-8",
    )
    parent = tmp_path / "parent.yaml"
    parent.write_text(
        """
name: parent-include
start: child
nodes:
  - id: child
    type: include
    path: child.yaml
    output_key: nested
""",
        encoding="utf-8",
    )
    llm = ScriptedLLM()
    llm.enqueue(
        "ok",
        model="x",
        usage={"prompt_tokens": 10, "completion_tokens": 5, "cost_micros": 100},
    )
    state = run_workflow_file(parent, settings=tmp_settings, llm=llm, persist=False)
    assert state.status == "succeeded"
    include_usage = next(r.usage for r in state.results if r.node_id == "child")
    assert include_usage["prompt_tokens"] == 10
    assert include_usage["completion_tokens"] == 5
    assert include_usage["cost_micros"] == 100
    assert state.usage["prompt_tokens"] == include_usage["prompt_tokens"]
    assert state.usage["completion_tokens"] == include_usage["completion_tokens"]
    assert state.usage["cost_micros"] == include_usage["cost_micros"]
    assert len(llm.calls) == 1


def test_parallel_agent_branches_merge_usage() -> None:
    llm = ScriptedLLM()
    llm.enqueue(
        "left",
        model="l",
        usage={"prompt_tokens": 10, "completion_tokens": 1, "cost_micros": 100},
    )
    llm.enqueue(
        "right",
        model="r",
        usage={"prompt_tokens": 3, "completion_tokens": 7, "cost_micros": 40},
    )
    spec = WorkflowSpec.model_validate(
        {
            "name": "fan-usage",
            "start": "fan",
            "nodes": [
                {
                    "id": "fan",
                    "type": "parallel",
                    "output_key": "parts",
                    "branches": [
                        {
                            "id": "left",
                            "type": "agent",
                            "prompt": "L",
                            "model": "mock:l",
                        },
                        {
                            "id": "right",
                            "type": "agent",
                            "prompt": "R",
                            "model": "mock:r",
                        },
                    ],
                }
            ],
        }
    )
    ctx = ExecutionContext(spec, ToolRegistry(), llm=llm, default_model="mock:l")
    state = run_workflow(spec, {}, ctx)
    assert state.status == "succeeded"
    fan_usage = next(r.usage for r in state.results if r.node_id == "fan")
    assert fan_usage["prompt_tokens"] == 13
    assert fan_usage["completion_tokens"] == 8
    assert fan_usage["cost_micros"] == 140
    assert state.usage["prompt_tokens"] == fan_usage["prompt_tokens"]
    assert state.usage["completion_tokens"] == fan_usage["completion_tokens"]
    assert state.usage["cost_micros"] == fan_usage["cost_micros"]
    assert len(llm.calls) == 2
