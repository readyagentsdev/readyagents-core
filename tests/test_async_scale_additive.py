"""Byte-identical single-run guard for V2-07. Written before the async/batch path."""

from __future__ import annotations

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache

runner = CliRunner()

# Keys the 1.9.0 `readyagents run --json` envelope already emitted. Additive
# keys may appear; these must remain.
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

_PAUSE_JSON_KEYS = frozenset(
    {
        "ok",
        "command",
        "error",
        "message",
        "run_id",
        "node_id",
        "prompt",
        "status",
    }
)


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _payload(text: str) -> dict:
    start = text.find("{")
    assert start >= 0, text
    return json.loads(text[start:])


def test_calc_pipeline_json_keys_unchanged() -> None:
    result = runner.invoke(
        app, ["run", str(_root() / "examples" / "calc_pipeline.yaml"), "--json", "--no-persist"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    data = _payload(result.stdout)
    missing = _RUN_JSON_KEYS - set(data)
    assert not missing, missing
    assert data["ok"] is True
    assert data["command"] == "run"
    assert data["status"] == "succeeded"
    assert data["workflow"] == "calc_pipeline"
    assert "calc_pipeline ok" in str(data.get("outputs") or data.get("output_keys") or "")


def test_approval_gate_pauses_then_resume(tmp_path: Path, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    example = _root() / "examples" / "approval_gate.yaml"
    paused = runner.invoke(app, ["run", str(example), "--json"])
    assert paused.exit_code == 2, paused.stdout + paused.stderr
    data = _payload(paused.stdout)
    missing = _PAUSE_JSON_KEYS - set(data)
    assert not missing, missing
    assert data["ok"] is False
    assert data["command"] == "run"
    assert data["status"] == "paused"
    assert data["error"] == "ApprovalRequired"
    run_id = data["run_id"]
    resumed = runner.invoke(app, ["resume", run_id, "--approve", "gate", "--json"])
    assert resumed.exit_code == 0, resumed.stdout + resumed.stderr
    done = _payload(resumed.stdout)
    assert _RUN_JSON_KEYS <= set(done)
    assert done["status"] == "succeeded"
    assert done["ok"] is True
    assert "approval_gate ok" in str(done.get("outputs") or done.get("output_keys") or "")
    clear_settings_cache()


def test_help_lists_batch() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "batch" in result.stdout
    assert "run" in result.stdout
    assert re.search(r"\brun\b", result.stdout)
