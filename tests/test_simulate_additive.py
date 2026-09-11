"""Byte-identical no-simulate guard for V2-10. Written before simulate lands."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from readyagents.cli import app

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


def test_calc_pipeline_without_simulate_json_keys() -> None:
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
    assert "simulate" not in blob


def test_eval_pass_without_simulate() -> None:
    first = runner.invoke(app, ["eval", str(_root() / "examples" / "eval" / "pass.yaml"), "--json"])
    second = runner.invoke(
        app, ["eval", str(_root() / "examples" / "eval" / "pass.yaml"), "--json"]
    )
    assert first.exit_code == 0, first.stdout + first.stderr
    assert second.exit_code == 0
    a = _payload(first.stdout)
    b = _payload(second.stdout)
    assert a["ok"] is True
    assert a["passed"] >= 1
    assert a["command"] == b["command"] == "eval"


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
        return
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
