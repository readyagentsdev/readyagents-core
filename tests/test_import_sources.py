"""Four sources import to schema-valid workflows that dry-run."""

from __future__ import annotations

from pathlib import Path

from import_cases import crewai_export, do_import, langgraph_export, n8n_export, trigger_export
from readyagents.workflow.runner import load_workflow, run_workflow_file


def test_n8n_imports_schema_valid_dry_run(tmp_path: Path, tmp_settings) -> None:
    result = do_import("n8n", n8n_export(), tmp_path, tmp_settings, "n8n.json")
    spec = load_workflow(result.workflow_path)
    assert spec.nodes
    state = run_workflow_file(
        result.workflow_path, dry_run=True, persist=False, settings=tmp_settings
    )
    assert state.status == "succeeded"
    assert result.dry_run_ok is True
    assert result.graph_path.is_file()
    assert "flowchart" in result.mermaid


def test_langgraph_imports_schema_valid_dry_run(tmp_path: Path, tmp_settings) -> None:
    result = do_import("langgraph", langgraph_export(), tmp_path, tmp_settings, "g.py")
    spec = load_workflow(result.workflow_path)
    assert spec.nodes
    state = run_workflow_file(
        result.workflow_path, dry_run=True, persist=False, settings=tmp_settings
    )
    assert state.status == "succeeded"
    assert result.dry_run_ok is True


def test_crewai_imports_schema_valid_dry_run(tmp_path: Path, tmp_settings) -> None:
    result = do_import("crewai", crewai_export(), tmp_path, tmp_settings, "crew.yaml")
    spec = load_workflow(result.workflow_path)
    assert spec.nodes
    state = run_workflow_file(
        result.workflow_path, dry_run=True, persist=False, settings=tmp_settings
    )
    assert state.status == "succeeded"
    assert result.dry_run_ok is True


def test_trigger_imports_schema_valid_dry_run(tmp_path: Path, tmp_settings) -> None:
    result = do_import("trigger", trigger_export(), tmp_path, tmp_settings, "zap.json")
    spec = load_workflow(result.workflow_path)
    assert spec.nodes
    state = run_workflow_file(
        result.workflow_path, dry_run=True, persist=False, settings=tmp_settings
    )
    assert state.status == "succeeded"
    assert result.dry_run_ok is True
