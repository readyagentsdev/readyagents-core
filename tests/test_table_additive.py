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
    assert set(a) >= _RUN_JSON_KEYS
    assert set(b) >= _RUN_JSON_KEYS
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


def test_classify_without_decider_still_calls_llm_not_decider(
    tmp_path: Path, tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from readyagents.table.store import TableStore
    from readyagents.testing.helpers import ScriptedLLM

    def boom(*_a, **_k):
        raise AssertionError("decider must not run when model_for_remainder has no decider")

    monkeypatch.setattr("readyagents.decide.registry.get_decider", boom)
    path = tmp_path / "exports.csv"
    path.write_text("id,email,amount\n1,ada@x.test,5\n2,bob@x.test,20\n", encoding="utf-8")
    llm = ScriptedLLM().enqueue(
        '[{"index": 0, "label": "review"}, {"index": 1, "label": "reject"}]',
        model="mock:test",
    )
    spec = {
        "name": "pipe",
        "nodes": [
            {
                "id": "load",
                "type": "table",
                "op": "read",
                "source": {"kind": "csv", "path": "exports.csv"},
                "schema": {"id": "int", "email": "str", "amount": "float"},
                "output_key": "rows",
                "next": "triage",
            },
            {
                "id": "triage",
                "type": "classify",
                "source": "{{ rows }}",
                "rules": [],
                "model_for_remainder": {
                    "model": "mock:test",
                    "batch": 25,
                    "labels": ["review", "reject"],
                },
                "output_key": "labelled",
            },
        ],
    }
    state = run_workflow_spec(
        spec, pin_home=tmp_settings.home_path(), workflow_dir=tmp_path, llm=llm
    )
    assert state.status == "succeeded"
    assert len(llm.calls) == 1
    store = TableStore(tmp_settings.home_path() / "tables")
    rows = list(store.iter_rows(state.output_keys["labelled"]["sha256"]))
    assert {r["decision"] for r in rows} == {"model"}
