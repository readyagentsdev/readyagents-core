"""Frozen async wrapper: to_thread over the sync engine; nested wrap is safe."""

from __future__ import annotations

import asyncio
from pathlib import Path

from readyagents.testing.helpers import run_workflow_spec
from readyagents.tools import ToolRegistry
from readyagents.workflow.async_engine import run_workflow_async, run_workflow_file_async
from readyagents.workflow.engine import run_workflow
from readyagents.workflow.nodes import ExecutionContext
from readyagents.workflow.schema import WorkflowSpec


def _spec() -> dict:
    return {
        "name": "async_echo",
        "inputs": {"n": 1},
        "nodes": [
            {
                "id": "t",
                "type": "transform",
                "template": "n={{n}}",
                "output_key": "msg",
            }
        ],
    }


def test_sync_and_async_match(tmp_settings) -> None:
    workflow = WorkflowSpec.model_validate(_spec())
    tools = ToolRegistry()
    ctx_sync = ExecutionContext(workflow, tools, default_model="mock:test")
    sync_state = run_workflow(workflow, {"n": 4}, ctx_sync)
    ctx_async = ExecutionContext(workflow, tools, default_model="mock:test")
    async_state = asyncio.run(run_workflow_async(workflow, {"n": 4}, ctx_async))
    assert sync_state.status == async_state.status == "succeeded"
    assert sync_state.output_keys == async_state.output_keys == {"msg": "n=4"}
    assert [row.node_id for row in sync_state.results] == [
        row.node_id for row in async_state.results
    ]
    assert [row.status for row in sync_state.results] == [row.status for row in async_state.results]


def test_nested_async_wrap(tmp_settings) -> None:
    workflow = WorkflowSpec.model_validate(_spec())

    async def outer() -> tuple[str, str]:
        tools = ToolRegistry()
        first = await run_workflow_async(
            workflow,
            {"n": 1},
            ExecutionContext(workflow, tools, default_model="mock:test"),
        )
        second = await run_workflow_async(
            workflow,
            {"n": 2},
            ExecutionContext(workflow, tools, default_model="mock:test"),
        )
        return first.output_keys["msg"], second.output_keys["msg"]

    a, b = asyncio.run(outer())
    assert a == "n=1"
    assert b == "n=2"


def test_run_workflow_file_async(tmp_path: Path, tmp_settings) -> None:
    path = tmp_path / "echo.yaml"
    path.write_text(
        "name: file_async\nnodes:\n"
        "  - id: t\n    type: transform\n    template: 'ok'\n    output_key: msg\n",
        encoding="utf-8",
    )
    state = asyncio.run(run_workflow_file_async(path, persist=False, settings=tmp_settings))
    assert state.status == "succeeded"
    assert state.output_keys["msg"] == "ok"


def test_openai_complete_async_falls_back_to_thread(monkeypatch) -> None:
    import sys
    from types import ModuleType

    from readyagents.llm.base import CompletionResult, Message
    from readyagents.llm.openai_provider import OpenAIProvider

    provider = OpenAIProvider(api_key="sk-test")

    def fake_complete(messages, *, model, tools=None, **kwargs):
        return CompletionResult(text="via-sync", model=model)

    monkeypatch.setattr(provider, "complete", fake_complete)
    monkeypatch.setitem(sys.modules, "openai", ModuleType("openai"))

    result = asyncio.run(
        provider.complete_async([Message(role="user", content="hi")], model="gpt-4o-mini")
    )
    assert result.text == "via-sync"


def test_helpers_still_drive_sync_engine() -> None:
    state = run_workflow_spec(_spec(), inputs={"n": 9}, tools=ToolRegistry())
    assert state.status == "succeeded"
    assert state.output_keys["msg"] == "n=9"
