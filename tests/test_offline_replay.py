from __future__ import annotations

import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.errors import CassetteMiss, LLMError
from readyagents.llm.registry import get_provider
from readyagents.replay.cassette import Cassette
from readyagents.testing.helpers import ScriptedLLM
from readyagents.workflow.runner import replay_run, run_workflow_file

_runner = CliRunner()


def _agent_workflow(tmp_path: Path) -> Path:
    path = tmp_path / "agent.yaml"
    path.write_text(
        "name: rec-agent\n"
        "nodes:\n"
        "  - id: a\n"
        "    type: agent\n"
        "    prompt: hi\n"
        "    model: mock:m\n"
        "    output_key: t\n",
        encoding="utf-8",
    )
    return path


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


def test_record_and_offline_replay_agent(tmp_path: Path, tmp_settings, monkeypatch) -> None:
    """Record an agent, then CLI --offline with keys unset and a socket guard."""
    path = _agent_workflow(tmp_path)
    inner = ScriptedLLM()
    inner.enqueue("offline-hello", model="m")
    recorded = run_workflow_file(path, settings=tmp_settings, persist=True, record=True, llm=inner)
    assert recorded.output_keys["t"] == "offline-hello"
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    monkeypatch.setenv("READYAGENTS_WORKSPACE", str(tmp_path))
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_COMPAT_API_KEY",
        "READYAGENTS_OPENAI_API_KEY",
        "READYAGENTS_ANTHROPIC_API_KEY",
        "READYAGENTS_OPENAI_COMPAT_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    clear_settings_cache()

    constructed: list[str] = []

    def _no_client(*args, **kwargs):
        constructed.append("get_provider")
        raise AssertionError("offline replay constructed a live provider")

    monkeypatch.setattr("readyagents.llm.registry.get_provider", _no_client)
    monkeypatch.setattr("readyagents.workflow.nodes.get_provider", _no_client)

    def _blocked(*args, **kwargs):
        raise AssertionError("socket opened during offline replay")

    monkeypatch.setattr(socket, "socket", _blocked)
    replayed = _runner.invoke(
        app, ["runs", "replay", recorded.run_id, "--offline", "--json", "--no-persist"]
    )
    assert replayed.exit_code == 0, replayed.stdout + replayed.stderr
    assert constructed == []
    assert "offline-hello" in replayed.stdout
    assert '"replay"' in replayed.stdout or "replayed_from" in replayed.stdout

    cassette = Cassette.load(recorded.metadata["cassette"])
    cassette.entries.clear()
    cassette.save(Path(recorded.metadata["cassette"]))
    missed = _runner.invoke(app, ["runs", "replay", recorded.run_id, "--offline", "--json"])
    assert missed.exit_code != 0
    assert constructed == []
    assert (
        "CassetteMiss" in (missed.stdout + missed.stderr)
        or "miss" in (missed.stdout + missed.stderr).lower()
    )


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


def test_cassette_miss_never_calls_live(tmp_path: Path, tmp_settings, monkeypatch) -> None:
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
    constructed: list[str] = []

    def _no_client(*args, **kwargs):
        constructed.append("get_provider")
        raise AssertionError("miss constructed a live provider")

    monkeypatch.setattr("readyagents.workflow.nodes.get_provider", _no_client)
    with pytest.raises(CassetteMiss):
        replay_run(recorded.run_id, settings=tmp_settings, persist=False, offline=True)
    assert constructed == []


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
