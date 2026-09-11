from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from readyagents.errors import NodeError, ToolError
from readyagents.mcp.builtin import tool_calc
from readyagents.testing import run_workflow_spec
from readyagents.tools import FunctionTool, default_registry
from readyagents.workflow.engine import run_workflow
from readyagents.workflow.nodes import ExecutionContext
from readyagents.workflow.runner import run_workflow_file
from readyagents.workflow.schema import WorkflowSpec
from readyagents.workflow.state import persist_run


def _tools(tmp_path: Path):
    return default_registry(allow_http=False, workspace=tmp_path)


def _foreach_spec(items: list[str] | str = "expressions", max_items: int | None = 32) -> dict:
    node: dict = {
        "id": "each",
        "type": "foreach",
        "items": items if isinstance(items, str) else "expressions",
        "output_key": "results",
        "body": {
            "id": "math",
            "type": "tool",
            "tool": "calc",
            "arguments": {"expression": "{{item}}"},
        },
    }
    if max_items is not None:
        node["max_items"] = max_items
    return {
        "name": "foreach-calc",
        "inputs": {"expressions": items if not isinstance(items, str) else ["1+1", "2+2"]},
        "nodes": [node],
    }


def test_foreach_schema_requires_items_and_body() -> None:
    with pytest.raises(ValidationError, match="items"):
        WorkflowSpec.model_validate(
            {
                "name": "x",
                "nodes": [
                    {
                        "id": "f",
                        "type": "foreach",
                        "body": {"id": "t", "type": "transform", "template": "a"},
                    }
                ],
            }
        )
    with pytest.raises(ValidationError, match="body"):
        WorkflowSpec.model_validate(
            {"name": "x", "nodes": [{"id": "f", "type": "foreach", "items": "xs"}]}
        )


def test_nested_foreach_rejected() -> None:
    with pytest.raises(ValidationError, match="nested foreach"):
        WorkflowSpec.model_validate(
            {
                "name": "x",
                "nodes": [
                    {
                        "id": "f",
                        "type": "foreach",
                        "items": "xs",
                        "body": {
                            "id": "inner",
                            "type": "foreach",
                            "items": "xs",
                            "body": {"id": "t", "type": "transform", "template": "a"},
                        },
                    }
                ],
            }
        )


def test_foreach_runs_real_calc(tmp_path: Path) -> None:
    state = run_workflow_spec(
        _foreach_spec(["1+1", "2+2"]),
        tools=_tools(tmp_path),
    )
    assert state.status == "succeeded"
    assert state.output_keys["results"] == [2, 4]


def test_foreach_cap_exceeded(tmp_path: Path) -> None:
    started = time.perf_counter()
    with pytest.raises(NodeError, match="max_items=1") as exc:
        run_workflow_spec(
            _foreach_spec(["1+1", "2+2"], max_items=1),
            tools=_tools(tmp_path),
        )
    assert time.perf_counter() - started < 1.0
    assert exc.value.node_id == "each"


def test_foreach_resume_skips_completed_items(tmp_path, tmp_settings) -> None:
    hits = {"n": 0}

    def calc_flaky(expression: str):
        hits["n"] += 1
        if hits["n"] == 2:
            raise ToolError("boom")
        return tool_calc(expression)

    tools = _tools(tmp_path)
    tools._tools["calc"] = FunctionTool(
        name="calc",
        description="flaky calc",
        schema={"type": "object", "properties": {"expression": {"type": "string"}}},
        handler=calc_flaky,
    )
    spec = WorkflowSpec.model_validate(_foreach_spec(["1+1", "2+2"]))
    saved: list = []

    def on_persist(state) -> None:
        persist_run(state, tmp_settings.runs_dir())
        saved.append(state.status)

    ctx = ExecutionContext(spec, tools, on_persist=on_persist, default_model="mock:test")
    with pytest.raises(NodeError, match="boom"):
        run_workflow(spec, spec.input_defaults(), ctx)
    assert hits["n"] == 2
    run_id = None
    from readyagents.workflow.state import list_runs

    found = list_runs(tmp_settings.runs_dir(), status="failed")
    assert found
    run_id = found[0].run_id
    meta = found[0].metadata.get("_foreach") or {}
    assert meta.get("each")
    assert meta["each"][0]["output"] == 2

    ctx2 = ExecutionContext(spec, tools, on_persist=on_persist, default_model="mock:test")
    from readyagents.workflow.state import load_run

    loaded = load_run(tmp_settings.runs_dir(), run_id)
    state = run_workflow(spec, spec.input_defaults(), ctx2, state=loaded)
    assert state.status == "succeeded"
    assert state.output_keys["results"] == [2, 4]
    # item 0 not re-run: first resume call is item 1 only (hit 3)
    assert hits["n"] == 3


def test_concurrent_foreach_failure_keeps_prefix(tmp_path, tmp_settings) -> None:
    hits = {"seen": []}
    ready = threading.Event()

    def calc_flaky(expression: str):
        hits["seen"].append(expression)
        if expression == "1+1":
            out = tool_calc(expression)
            ready.set()
            return out
        if expression == "2+2":
            ready.wait(timeout=2)
            if hits["seen"].count("2+2") == 1:
                raise ToolError("boom")
        return tool_calc(expression)

    tools = _tools(tmp_path)
    tools._tools["calc"] = FunctionTool(
        name="calc",
        description="flaky calc",
        schema={"type": "object", "properties": {"expression": {"type": "string"}}},
        handler=calc_flaky,
    )
    spec = WorkflowSpec.model_validate(
        {
            "name": "foreach-conc",
            "inputs": {"expressions": ["1+1", "2+2", "3+3"]},
            "nodes": [
                {
                    "id": "each",
                    "type": "foreach",
                    "items": "expressions",
                    "concurrency": 2,
                    "output_key": "results",
                    "body": {
                        "id": "math",
                        "type": "tool",
                        "tool": "calc",
                        "arguments": {"expression": "{{item}}"},
                    },
                }
            ],
        }
    )

    def on_persist(state) -> None:
        persist_run(state, tmp_settings.runs_dir())

    ctx = ExecutionContext(spec, tools, on_persist=on_persist, default_model="mock:test")
    with pytest.raises(NodeError, match="boom"):
        run_workflow(spec, spec.input_defaults(), ctx)
    from readyagents.workflow.state import list_runs, load_run

    found = list_runs(tmp_settings.runs_dir(), status="failed")
    assert found
    meta = found[0].metadata.get("_foreach") or {}
    rows = meta.get("each") or []
    assert rows, "contiguous prefix of ok items must be checkpointed"
    assert rows[0]["output"] == 2
    assert all(row.get("status") == "ok" for row in rows)
    first_count = hits["seen"].count("1+1")
    assert first_count == 1

    ctx2 = ExecutionContext(spec, tools, on_persist=on_persist, default_model="mock:test")
    loaded = load_run(tmp_settings.runs_dir(), found[0].run_id)
    state = run_workflow(spec, spec.input_defaults(), ctx2, state=loaded)
    assert state.status == "succeeded"
    assert state.output_keys["results"] == [2, 4, 6]
    assert hits["seen"].count("1+1") == 1


def test_foreach_example_cli(tmp_path, tmp_settings, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    examples = Path(__file__).resolve().parents[1] / "examples" / "foreach_calc.yaml"
    state = run_workflow_file(examples, persist=False, settings=tmp_settings)
    assert state.status == "succeeded"
    assert state.output_keys["results"] == [2, 4]
