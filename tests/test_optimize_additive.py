"""Byte-identical no-optimize guard for V2-04. Written before optimize lands."""

from __future__ import annotations

import json
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


def test_calc_pipeline_without_optimize_json_keys() -> None:
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
    blob = json.dumps(a)
    assert "optimize" not in blob
    assert "prompt_id" not in blob


def test_eval_pass_without_optimize() -> None:
    result = runner.invoke(
        app, ["eval", str(_root() / "examples" / "eval" / "pass.yaml"), "--json"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    data = _payload(result.stdout)
    assert data["ok"] is True
    assert data["command"] == "eval"
    assert data["passed"] >= 1
    assert data["failed"] == 0


def test_calc_and_approval_examples_clean() -> None:
    import subprocess

    names = ("calc_pipeline.yaml", "approval_gate.yaml")
    for name in names:
        path = _root() / "examples" / name
        assert path.is_file()
        text = path.read_text(encoding="utf-8")
        assert "prompt_id:" not in text
        assert "optimize" not in text
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
    assert diff.stdout == ""
