"""Byte-identical no-`contract:` guard for V2-09. Written before contracts land."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.errors import StructuredOutputError
from readyagents.testing import ScriptedLLM, run_workflow_spec

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


def test_calc_pipeline_without_contract_json_keys() -> None:
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
    assert a["command"] == "run"
    assert a["ok"] is True
    assert a["status"] == "succeeded"
    assert a["output_keys"] == b["output_keys"] or set(a["output_keys"]) == set(b["output_keys"])


def test_approval_gate_without_contract_still_pauses(tmp_path: Path, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    example = _root() / "examples" / "approval_gate.yaml"
    paused = runner.invoke(app, ["run", str(example), "--json"])
    assert paused.exit_code == 2, paused.stdout + paused.stderr
    data = _payload(paused.stdout)
    assert data["ok"] is False
    assert data["command"] == "run"
    assert data["status"] == "paused"
    run_id = data["run_id"]
    resumed = runner.invoke(app, ["resume", run_id, "--approve", "gate", "--json"])
    assert resumed.exit_code == 0, resumed.stdout + resumed.stderr
    done = _payload(resumed.stdout)
    assert done["ok"] is True
    assert done["status"] == "succeeded"


def test_output_schema_invalid_without_contract_still_typed() -> None:
    llm = ScriptedLLM()
    llm.enqueue('{"label": 9}', model="x")
    with pytest.raises(StructuredOutputError) as exc:
        run_workflow_spec(
            {
                "name": "struct-bad",
                "nodes": [
                    {
                        "id": "a",
                        "type": "agent",
                        "prompt": "classify",
                        "model": "mock:x",
                        "output_schema": {
                            "type": "object",
                            "required": ["label"],
                            "properties": {"label": {"type": "string"}},
                            "additionalProperties": False,
                        },
                    }
                ],
            },
            llm=llm,
        )
    assert exc.value.node_id == "a"
    assert "schema" in str(exc.value).lower() or "validation" in str(exc.value).lower()
    assert len(llm.calls) == 1
