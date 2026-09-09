from __future__ import annotations

import socket
from pathlib import Path

import pytest

from readyagents.errors import CassetteMiss, LLMError
from readyagents.llm.registry import get_provider
from readyagents.replay.cassette import Cassette
from readyagents.testing.helpers import ScriptedLLM
from readyagents.workflow.runner import replay_run, run_workflow_file


def _calc_workflow(tmp_path: Path) -> Path:
    path = tmp_path / "calc.yaml"
    path.write_text(
        "name: rec-calc\n"
        "nodes:\n"
        "  - id: math\n"
        "    type: tool\n"
        "    tool: calc\n"
        "    arguments: {expression: '1+1'}\n"
        "    output_key: total\n",
        encoding="utf-8",
    )
    return path


def test_record_and_offline_replay_keyless(tmp_path: Path, tmp_settings) -> None:
    path = _calc_workflow(tmp_path)
    recorded = run_workflow_file(path, settings=tmp_settings, persist=True, record=True)
    assert recorded.status == "succeeded"
    assert recorded.output_keys["total"] == 2
    cassette = Path(recorded.metadata["cassette"])
    assert cassette.is_file()

    def _blocked(*args, **kwargs):
        raise AssertionError("socket opened during offline replay")

    monkey_socket = socket.socket
    socket.socket = _blocked  # type: ignore[method-assign]
    try:
        replayed = replay_run(recorded.run_id, settings=tmp_settings, persist=True, offline=True)
    finally:
        socket.socket = monkey_socket  # type: ignore[method-assign]
    assert replayed.status == "succeeded"
    assert replayed.output_keys["total"] == 2
    assert replayed.metadata.get("replayed_from") == recorded.run_id
    report = replayed.metadata.get("determinism") or {}
    assert "math" in (report.get("recomputed") or report.get("sealed") or [])


def test_offline_get_provider_does_not_read_key() -> None:
    with pytest.raises(LLMError, match="Offline replay cannot construct"):
        get_provider("openai:gpt-4o-mini", offline=True)


def test_cassette_miss_never_calls_live(tmp_path: Path, tmp_settings) -> None:
    path = tmp_path / "agent.yaml"
    path.write_text(
        "name: agent-one\n"
        "nodes:\n"
        "  - id: a\n"
        "    type: agent\n"
        "    prompt: hi\n"
        "    model: mock:m\n"
        "    output_key: t\n",
        encoding="utf-8",
    )
    inner = ScriptedLLM()
    inner.enqueue("hello", model="m")
    recorded = run_workflow_file(path, settings=tmp_settings, persist=True, record=True, llm=inner)
    cassette = Cassette.load(recorded.metadata["cassette"])
    cassette.entries.clear()
    cassette.save(Path(recorded.metadata["cassette"]))
    with pytest.raises(CassetteMiss):
        replay_run(recorded.run_id, settings=tmp_settings, persist=False, offline=True)


def test_unclassified_pack_tool_is_unsealable(tmp_path: Path, tmp_settings) -> None:
    from readyagents.tools import FunctionTool, ToolRegistry

    tools = ToolRegistry()
    tools.register(FunctionTool(name="mystery", description="x", handler=lambda: "y"))
    path = tmp_path / "packish.yaml"
    path.write_text(
        "name: mystery-flow\n"
        "nodes:\n"
        "  - id: m\n"
        "    type: tool\n"
        "    tool: mystery\n"
        "    output_key: v\n",
        encoding="utf-8",
    )
    state = run_workflow_file(
        path,
        settings=tmp_settings,
        persist=True,
        record=True,
        extra_tools=tools,
    )
    report = Cassette.load(state.metadata["cassette"]).report.as_dict()
    assert "m" in report["unsealable"]
