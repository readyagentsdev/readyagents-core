"""Byte-identical no-trigger guard for V2-18. Written before triggers: lands."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache

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


def test_calc_pipeline_without_triggers_json_keys() -> None:
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
    blob = json.dumps(a)
    assert "triggers:" not in blob
    assert a["status"] != "waiting"


def test_approval_gate_without_triggers_still_pauses(tmp_path: Path, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    example = _root() / "examples" / "approval_gate.yaml"
    paused = runner.invoke(app, ["run", str(example), "--json"])
    assert paused.exit_code == 2, paused.stdout + paused.stderr
    data = _payload(paused.stdout)
    assert data["ok"] is False
    assert data["status"] == "paused"
    approved = runner.invoke(app, ["run", str(example), "--approve", "gate", "--json"])
    assert approved.exit_code == 0, approved.stdout + approved.stderr
    done = _payload(approved.stdout)
    assert done["status"] == "succeeded"
    assert "triggers:" not in json.dumps(done)


def test_examples_match_v1_9_0_or_clean() -> None:
    root = _root()
    tagged = subprocess.run(
        [
            "git",
            "diff",
            "v1.9.0",
            "--",
            "examples/calc_pipeline.yaml",
            "examples/approval_gate.yaml",
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if tagged.returncode == 0:
        assert tagged.stdout == "", tagged.stdout
    else:
        status = subprocess.run(
            [
                "git",
                "status",
                "--porcelain",
                "examples/calc_pipeline.yaml",
                "examples/approval_gate.yaml",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        assert status.stdout.strip() == "", status.stdout
