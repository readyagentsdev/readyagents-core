"""Byte-identical no-studio guard for V2-17. Written before the command lands."""

from __future__ import annotations

import json
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


def test_calc_pipeline_without_studio_json_keys() -> None:
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


def test_approval_gate_without_studio_still_pauses(tmp_path: Path, monkeypatch) -> None:
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


def test_examples_match_v1_9_0_tag() -> None:
    import subprocess

    def _diff(base: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "git",
                "diff",
                base,
                "--",
                "examples/calc_pipeline.yaml",
                "examples/approval_gate.yaml",
            ],
            cwd=_root(),
            check=False,
            capture_output=True,
            text=True,
        )

    tagged = subprocess.run(
        ["git", "rev-parse", "--verify", "v1.9.0"],
        cwd=_root(),
        check=False,
        capture_output=True,
        text=True,
    )
    if tagged.returncode == 0:
        result = _diff("v1.9.0")
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        return
    # GitHub Actions checkout is often tagless; studio must not touch these files.
    result = _diff("HEAD^")
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
