"""Fidelity categories, stubs, structure, --explain, overwrite."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from import_cases import crewai_export, do_import, langgraph_export, n8n_export, trigger_export
from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.errors import ImportRefused
from readyagents.importers.ir import FIDELITY
from readyagents.importers.service import explain_source, import_workflow
from readyagents.workflow.runner import run_workflow_file

runner = CliRunner()


def test_every_node_has_fidelity_status(tmp_path: Path, tmp_settings) -> None:
    for source, text, name in (
        ("n8n", n8n_export(), "a.json"),
        ("langgraph", langgraph_export(), "b.py"),
        ("crewai", crewai_export(), "c.yaml"),
        ("trigger", trigger_export(), "d.json"),
    ):
        result = do_import(source, text, tmp_path, tmp_settings, name)
        assert result.report.nodes
        for row in result.report.nodes:
            assert row.status in FIDELITY
            assert row.reason
        counts = result.report.as_dict()["counts"]
        assert sum(counts.values()) == len(result.report.nodes)
        assert result.report.coverage == round(
            100.0 * (counts["translated"] + counts["approximated"]) / len(result.report.nodes),
            1,
        )
        assert "must be tested" in result.report.statement.lower()
        assert result.report.as_dict()["equivalence"] is False


def test_structural_cases_named(tmp_path: Path, tmp_settings) -> None:
    n8n = do_import("n8n", n8n_export(), tmp_path, tmp_settings, "n.json")
    kinds = {row.structural for row in n8n.report.nodes if row.structural}
    assert {"branch", "loop", "parallel", "subworkflow", "error"} <= kinds
    lg = do_import("langgraph", langgraph_export(), tmp_path, tmp_settings, "g.py")
    lg_struct = {row.structural for row in lg.report.nodes if row.structural}
    assert "branch" in lg_struct
    crew = crewai_export() + "  ask:\n    description: confirm\n    human_input: true\n"
    human = do_import("crewai", crew, tmp_path, tmp_settings, "h.yaml")
    assert any(row.structural == "human" for row in human.report.nodes)
    trig = do_import("trigger", trigger_export(), tmp_path, tmp_settings, "z.json")
    tstruct = {row.structural for row in trig.report.nodes if row.structural}
    assert {"branch", "loop", "parallel", "subworkflow", "error"} <= tstruct
    for result in (n8n, lg, trig):
        for row in result.report.nodes:
            if row.structural:
                assert row.status == "approximated"


def test_unsupported_stub_is_not_skipped(tmp_path: Path, tmp_settings) -> None:
    result = do_import("n8n", n8n_export(), tmp_path, tmp_settings, "s.json")
    stubs = [n for n in result.report.nodes if n.status == "unsupported"]
    assert stubs
    slack = next(n for n in stubs if "slack" in n.kind.lower())
    text = result.workflow_path.read_text(encoding="utf-8")
    assert slack.id in text
    assert "UNSUPPORTED" in text
    assert "Slack" in text
    # Run the stub itself: emit a one-node workflow by running full graph;
    # IF when:true skips Slack, so run a source that only has Slack.
    only = {
        "name": "only-slack",
        "nodes": [{"id": "1", "name": "Slack", "type": "n8n-nodes-base.slack", "parameters": {}}],
        "connections": {},
    }
    src = tmp_path / "only.json"
    src.write_text(json.dumps(only), encoding="utf-8")
    stubbed = import_workflow("n8n", src, out=tmp_path / "only-out", settings=tmp_settings)
    state = run_workflow_file(
        stubbed.workflow_path, dry_run=False, persist=False, settings=tmp_settings
    )
    assert state.status == "succeeded"
    blob = json.dumps(state.output_keys)
    assert "UNSUPPORTED" in blob
    assert "Slack" in blob
    assert any(
        "slack" in (r.node_id or "") or "UNSUPPORTED" in str(r.output) for r in state.results
    )


def test_explain_writes_nothing(tmp_path: Path, tmp_settings) -> None:
    table = explain_source("n8n")
    assert table.entries
    assert (tmp_path / "workflow.yaml").exists() is False
    first = runner.invoke(app, ["import", "--explain", "n8n"])
    second = runner.invoke(app, ["import", "--explain", "n8n"])
    assert first.exit_code == 0, first.stdout
    assert second.exit_code == 0
    assert "n8n-nodes-base.set" in first.stdout


def test_overwrite_refused_without_force(tmp_path: Path, tmp_settings) -> None:
    src = tmp_path / "n.json"
    src.write_text(n8n_export(), encoding="utf-8")
    dest = tmp_path / "out"
    import_workflow("n8n", src, out=dest, settings=tmp_settings)
    with pytest.raises(ImportRefused) as extra:
        import_workflow("n8n", src, out=dest, settings=tmp_settings)
    assert extra.value.reason == "exists"
    again = import_workflow("n8n", src, out=dest, settings=tmp_settings, force=True)
    assert again.workflow_path.is_file()


def test_cli_import_and_explain(tmp_path: Path, tmp_settings, monkeypatch) -> None:
    src = tmp_path / "n.json"
    src.write_text(n8n_export(), encoding="utf-8")
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    monkeypatch.setenv("READYAGENTS_WORKSPACE", str(tmp_path))
    clear_settings_cache()
    dest = tmp_path / "imported"
    first = runner.invoke(app, ["import", "n8n", str(src), "--out", str(dest), "--json"])
    assert first.exit_code == 0, first.stdout + first.stderr
    payload = json.loads(first.stdout[first.stdout.find("{") :])
    assert payload["ok"] is True
    assert payload["command"] == "import"
    assert payload["equivalence"] is False
    explained = runner.invoke(app, ["import", "--explain", "langgraph", "--json"])
    assert explained.exit_code == 0
    body = json.loads(explained.stdout[explained.stdout.find("{") :])
    assert body["ok"] is True
    assert body["command"] == "import explain"
