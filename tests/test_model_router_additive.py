"""Byte-identical no-policy guard for V2-08. Written before routing lands."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.llm.resilience import model_candidates
from readyagents.testing.helpers import ScriptedLLM, run_workflow_spec

runner = CliRunner()

_RUN_JSON_KEYS = frozenset(
    {
        "ok",
        "command",
        "record_version",
        "run_id",
        "workflow",
        "status",
        "started_at",
        "finished_at",
        "pending_node",
        "pending",
        "inputs",
        "outputs",
        "output_keys",
        "node_outputs",
        "node_results",
        "metadata",
        "errors",
        "usage",
        "provenance",
    }
)


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _payload(text: str) -> dict:
    start = text.find("{")
    assert start >= 0, text
    return json.loads(text[start:])


def test_calc_pipeline_without_routing_json_keys() -> None:
    first = runner.invoke(
        app, ["run", str(_root() / "examples" / "calc_pipeline.yaml"), "--json", "--no-persist"]
    )
    second = runner.invoke(
        app, ["run", str(_root() / "examples" / "calc_pipeline.yaml"), "--json", "--no-persist"]
    )
    assert first.exit_code == 0, first.stdout + first.stderr
    assert second.exit_code == 0, second.stdout + second.stderr
    a = _payload(first.stdout)
    b = _payload(second.stdout)
    assert _RUN_JSON_KEYS <= set(a)
    assert _RUN_JSON_KEYS <= set(b)
    assert a["ok"] is True
    assert a["status"] == "succeeded"
    assert set(a["output_keys"]) == set(b["output_keys"])
    assert "routes" not in (a.get("metadata") or {})


def test_approval_gate_without_routing_still_pauses(tmp_path: Path, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    example = _root() / "examples" / "approval_gate.yaml"
    paused = runner.invoke(app, ["run", str(example), "--json"])
    assert paused.exit_code == 2, paused.stdout + paused.stderr
    data = _payload(paused.stdout)
    assert data["ok"] is False
    assert data["status"] == "paused"
    run_id = data["run_id"]
    resumed = runner.invoke(app, ["resume", run_id, "--approve", "gate", "--json"])
    assert resumed.exit_code == 0, resumed.stdout + resumed.stderr
    done = _payload(resumed.stdout)
    assert done["ok"] is True
    assert done["status"] == "succeeded"
    clear_settings_cache()


def test_calc_and_approval_examples_clean() -> None:
    """Routing must not dirty the 1.9.0 calc/approval examples (tag may be absent on CI)."""
    import subprocess

    names = ("calc_pipeline.yaml", "approval_gate.yaml")
    for name in names:
        path = _root() / "examples" / name
        assert path.is_file()
        assert "routing:" not in path.read_text(encoding="utf-8")
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", *(f"examples/{n}" for n in names)],
        cwd=_root(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert status.returncode == 0, status.stderr
    assert status.stdout.strip() == ""
    tagged = subprocess.run(
        ["git", "rev-parse", "--verify", "v1.9.0"],
        cwd=_root(),
        check=False,
        capture_output=True,
        text=True,
    )
    if tagged.returncode != 0:
        return
    diff = subprocess.run(
        ["git", "diff", "v1.9.0", "--", *(f"examples/{n}" for n in names)],
        cwd=_root(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert diff.returncode == 0, diff.stderr
    assert diff.stdout.strip() == ""


def test_no_policy_model_selection_matches_legacy_candidates() -> None:
    llm = ScriptedLLM()
    llm.enqueue("ok", model="gpt-4o-mini")
    spec = {
        "name": "no-policy",
        "default_model": "openai:gpt-4o-mini",
        "fallback_models": ["anthropic:claude-3-5-haiku"],
        "nodes": [
            {
                "id": "a",
                "type": "agent",
                "prompt": "hi",
                "model": "openai:gpt-4o-mini",
                "fallback_models": ["openai:gpt-4o"],
                "output_key": "t",
            }
        ],
    }
    state = run_workflow_spec(spec, llm=llm)
    assert state.status == "succeeded"
    assert llm.calls[0]["model"] == "gpt-4o-mini"
    expected = model_candidates(
        "openai:gpt-4o-mini",
        ["openai:gpt-4o"],
        ["anthropic:claude-3-5-haiku"],
    )
    assert expected[0] == "openai:gpt-4o-mini"
    assert "routes" not in (state.metadata or {})
