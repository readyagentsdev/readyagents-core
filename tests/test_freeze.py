from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.errors import CassetteError
from readyagents.replay.cassette import Cassette
from readyagents.replay.freeze import freeze_run
from readyagents.testing.helpers import ScriptedLLM
from readyagents.tools import FunctionTool, ToolRegistry
from readyagents.workflow.runner import run_workflow_file

_runner = CliRunner()


def _cli_env(monkeypatch, tmp_path: Path, tmp_settings) -> None:
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    monkeypatch.setenv("READYAGENTS_WORKSPACE", str(tmp_path))
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_COMPAT_API_KEY",
        "READYAGENTS_OPENAI_API_KEY",
        "READYAGENTS_ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    clear_settings_cache()


def _record_agent(tmp_path: Path, tmp_settings, text: str = "hello-frozen-agent"):
    workflow = tmp_path / "agent.yaml"
    workflow.write_text(
        "name: freeze-agent\n"
        "nodes:\n"
        "  - id: a\n"
        "    type: agent\n"
        "    prompt: hi\n"
        "    model: mock:m\n"
        "    output_key: summary\n",
        encoding="utf-8",
    )
    inner = ScriptedLLM()
    inner.enqueue(text, model="m")
    state = run_workflow_file(workflow, settings=tmp_settings, persist=True, record=True, llm=inner)
    assert state.status == "succeeded"
    return state


def test_freeze_round_trip_eval(tmp_path: Path, tmp_settings, monkeypatch) -> None:
    """Generate a fixture and execute it with shipped `readyagents eval` (no keys)."""
    _cli_env(monkeypatch, tmp_path, tmp_settings)
    state = _record_agent(tmp_path, tmp_settings)
    frozen = _runner.invoke(app, ["runs", "freeze", state.run_id, "--out", "frozen-agent"])
    assert frozen.exit_code == 0, frozen.stdout + frozen.stderr
    err = frozen.stdout + frozen.stderr
    assert "WARNING" in err or "recorded model" in err.lower()
    dest = tmp_path / "frozen-agent"
    assert (dest / "cassette.json").is_file()
    assert (dest / "case.yaml").is_file()
    readme = (dest / "README.md").read_text(encoding="utf-8")
    assert "WARNING" in readme and "recorded model" in readme.lower()
    scored = _runner.invoke(app, ["eval", str(dest / "case.yaml")])
    assert scored.exit_code == 0, scored.stdout + scored.stderr
    assert "PASS" in scored.stdout or "passed=" in scored.stdout
    case_text = (dest / "case.yaml").read_text(encoding="utf-8")
    assert "expect_determinism:" in case_text
    assert "expect_nodes:" in case_text
    assert "pins determinism" in (dest / "README.md").read_text(encoding="utf-8")

    # Wiping cassette entries must fail eval — the case consults cassette.json.
    cassette = Cassette.load(dest / "cassette.json")
    cassette.entries.clear()
    cassette.save(dest / "cassette.json", root=dest)
    wiped = _runner.invoke(app, ["eval", str(dest / "case.yaml")])
    assert wiped.exit_code == 1, wiped.stdout + wiped.stderr


def test_freeze_refuses_unsealable_without_flag(tmp_path: Path, tmp_settings) -> None:
    tools = ToolRegistry()
    tools.register(FunctionTool(name="mystery", description="x", handler=lambda: "y"))
    workflow = tmp_path / "mystery.yaml"
    workflow.write_text(
        "name: mystery-flow\n"
        "nodes:\n"
        "  - id: m\n"
        "    type: tool\n"
        "    tool: mystery\n"
        "    output_key: v\n",
        encoding="utf-8",
    )
    state = run_workflow_file(
        workflow, settings=tmp_settings, persist=True, record=True, extra_tools=tools
    )
    cassette = Cassette.load(state.metadata["cassette"])
    with pytest.raises(CassetteError, match="unsealable"):
        freeze_run(
            state,
            cassette,
            out_dir=tmp_path / "nope",
            workspace=tmp_path,
            allow_unsealed=False,
        )


def test_freeze_exact_pins_outputs(tmp_path: Path, tmp_settings) -> None:
    state = _record_agent(tmp_path, tmp_settings, text="exact-pin")
    cassette = Cassette.load(state.metadata["cassette"])
    dest = tmp_path / "exact-fix"
    freeze_run(
        state,
        cassette,
        out_dir=dest,
        workspace=tmp_path,
        exact=True,
    )
    case = (dest / "case.yaml").read_text(encoding="utf-8")
    assert "expect_outputs" in case
    assert "exact-pin" in case
    assert "expect_determinism:" in case
    assert "expect_nodes:" in case


def test_diff_identical_and_changed(tmp_settings, tmp_path: Path) -> None:
    from readyagents.replay.diff import diff_runs

    workflow = tmp_path / "d.yaml"
    workflow.write_text(
        "name: diff-me\n"
        "inputs: {n: 1}\n"
        "nodes:\n"
        "  - id: t\n"
        "    type: transform\n"
        "    template: 'n={{n}}'\n"
        "    output_key: summary\n",
        encoding="utf-8",
    )
    a = run_workflow_file(workflow, settings=tmp_settings, persist=True, inputs={"n": 1})
    b = run_workflow_file(workflow, settings=tmp_settings, persist=True, inputs={"n": 1})
    same = diff_runs(a, b)
    assert same["identical"] is True
    c = run_workflow_file(workflow, settings=tmp_settings, persist=True, inputs={"n": 2})
    changed = diff_runs(a, c)
    assert changed["identical"] is False
    assert changed["first_divergence"]["node_id"] == "t"
    assert (tmp_settings.runs_dir() / f"{a.run_id}.json").is_file()
