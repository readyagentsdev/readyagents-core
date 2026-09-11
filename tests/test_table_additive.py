"""Byte-identical no-table guard for V2-19. Written before type: table lands."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.errors import NodeError
from readyagents.mcp.builtin import tool_json_get, tool_json_set
from readyagents.testing.helpers import run_workflow_spec
from readyagents.tools import default_registry

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


def test_calc_pipeline_without_table_json_keys() -> None:
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
    assert "type: table" not in blob
    assert '"_table"' not in blob
    assert "classify" not in blob


def test_approval_gate_without_table_still_pauses(tmp_path: Path, monkeypatch) -> None:
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


def test_json_get_json_set_builtins_unchanged() -> None:
    data = {"user": {"name": "Ada", "n": 2}}
    assert tool_json_get(data, "user.name") == "Ada"
    out = tool_json_set(data, "user.n", 3)
    assert out["user"]["n"] == 3
    assert data["user"]["n"] == 2


def test_foreach_default_cap_and_resume_unchanged(tmp_path: Path) -> None:
    tools = default_registry(allow_http=False, workspace=tmp_path)
    thirty_two = [f"{i}+0" for i in range(32)]
    spec = {
        "name": "foreach-default",
        "inputs": {"expressions": thirty_two},
        "nodes": [
            {
                "id": "each",
                "type": "foreach",
                "items": "expressions",
                "output_key": "results",
                "body": {
                    "id": "math",
                    "type": "tool",
                    "tool": "calc",
                    "arguments": {"expression": "{{item}}"},
                },
            }
        ],
    }
    state = run_workflow_spec(spec, tools=tools)
    assert state.status == "succeeded"
    assert len(state.output_keys["results"]) == 32
    too_many = {
        "name": "foreach-overflow",
        "inputs": {"expressions": [f"{i}+0" for i in range(33)]},
        "nodes": spec["nodes"],
    }
    with pytest.raises(NodeError, match="max_items=32"):
        run_workflow_spec(too_many, tools=tools)


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
