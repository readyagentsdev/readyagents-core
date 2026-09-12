"""Byte-identical no-browser guard for V2-14. Written before type: browser lands."""

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


def test_calc_pipeline_without_browser_json_keys() -> None:
    first = runner.invoke(
        app, ["run", str(_root() / "examples" / "calc_pipeline.yaml"), "--json", "--no-persist"]
    )
    second = runner.invoke(
        app, ["run", str(_root() / "examples" / "calc_pipeline.yaml"), "--json", "--no-persist"]
    )
    assert first.exit_code == 0, first.stdout + first.stderr
    assert second.exit_code == 0
    a = _payload(first.stdout)
    b = _payload(second.stdout)
    assert _RUN_JSON_KEYS <= set(a)
    assert set(a) == set(b)
    blob = json.dumps(a)
    assert "browser" not in blob
    assert "playwright" not in blob.lower()
    assert "chromium" not in blob.lower()


def test_eval_pass_without_browser() -> None:
    result = runner.invoke(
        app, ["eval", str(_root() / "examples" / "eval" / "pass.yaml"), "--json"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    data = _payload(result.stdout)
    assert data["ok"] is True
    assert data["failed"] == 0


def test_core_dependencies_have_no_browser_engine() -> None:
    text = (_root() / "pyproject.toml").read_text(encoding="utf-8")
    core = text.split("[project.optional-dependencies]", 1)[0].lower()
    for needle in ("playwright", "selenium", "chromium", "patchright"):
        assert needle not in core


def test_calc_and_approval_examples_clean() -> None:
    import subprocess

    names = ("calc_pipeline.yaml", "approval_gate.yaml")
    for name in names:
        path = _root() / "examples" / name
        text = path.read_text(encoding="utf-8")
        assert "type: browser" not in text
        assert "playwright" not in text.lower()
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
