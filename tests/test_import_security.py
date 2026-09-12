"""Secrets, AST-only, parser bombs, path confine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from import_cases import langgraph_export, n8n_export
from readyagents.errors import (
    ConfigError,
    ImportBoundDepth,
    ImportBoundNodes,
    ImportBoundOversized,
    ImportRefused,
)
from readyagents.importers.bounds import MAX_BYTES, MAX_DEPTH, MAX_NODES
from readyagents.importers.crewai import parse_crewai
from readyagents.importers.langgraph import parse_langgraph
from readyagents.importers.n8n import parse_n8n
from readyagents.importers.service import import_workflow
from readyagents.importers.trigger import parse_trigger


def test_secret_not_written_and_redacted(tmp_path: Path, tmp_settings) -> None:
    secret = "sk-abcdefghijklmnop"
    data = json.loads(n8n_export())
    data["nodes"][1]["parameters"] = {"apiKey": secret, "note": "ok"}
    src = tmp_path / "secret.json"
    src.write_text(json.dumps(data), encoding="utf-8")
    result = import_workflow("n8n", src, out=tmp_path / "out", settings=tmp_settings)
    yaml_text = result.workflow_path.read_text(encoding="utf-8")
    report = result.report_path.read_text(encoding="utf-8")
    blob = yaml_text + report + json.dumps(result.workflow)
    for token in (secret, secret.replace("-", "_"), secret.replace("_", "-")):
        assert token not in blob
    assert "***REDACTED***" in report or "secret" in " ".join(result.report.warnings).lower()
    assert result.report.warnings
    assert "export file itself is a secret" in " ".join(result.report.warnings)


def test_langgraph_ast_parses_without_importing(tmp_path: Path, tmp_settings) -> None:
    graph = parse_langgraph(langgraph_export())
    assert graph.nodes
    with pytest.raises(ImportError):
        import definitely_missing_langgraph_pkg_xyz  # noqa: F401


def test_non_static_add_node_refused() -> None:
    src = "g = object()\nname = 'x'\ng.add_node(name, lambda s: s)\n"
    with pytest.raises(ImportRefused) as extra:
        parse_langgraph(src)
    assert extra.value.reason == "ast"


def test_exec_call_refused() -> None:
    src = "g = object()\nexec('g.add_node(\"x\", lambda s: s)')\n"
    with pytest.raises(ImportRefused) as extra:
        parse_langgraph(src)
    assert extra.value.reason == "ast"


def test_crewai_python_ast_only() -> None:
    src = "from missing_crewai_pkg_zzz import Agent\nAgent(role='Writer', goal='Draft')\n"
    graph = parse_crewai(src, filename="crew.py")
    assert any(n.kind == "agent" for n in graph.nodes)


def test_oversized_refused() -> None:
    blob = "x" * (MAX_BYTES + 10)
    with pytest.raises(ImportBoundOversized):
        parse_n8n(blob)
    with pytest.raises(ImportBoundOversized):
        parse_trigger(blob)
    with pytest.raises(ImportBoundOversized):
        parse_langgraph(blob)


def test_depth_refused() -> None:
    node: dict = {"v": 1}
    cur = node
    for _ in range(MAX_DEPTH + 5):
        nxt: dict = {}
        cur["c"] = nxt
        cur = nxt
    blob = json.dumps({"name": "d", "nodes": [], "extra": node})
    with pytest.raises(ImportBoundDepth):
        parse_n8n(blob)


def test_node_bomb_refused() -> None:
    nodes = [
        {"id": str(i), "name": f"n{i}", "type": "n8n-nodes-base.noOp", "parameters": {}}
        for i in range(MAX_NODES + 5)
    ]
    blob = json.dumps({"name": "bomb", "nodes": nodes, "connections": {}})
    with pytest.raises(ImportBoundNodes):
        parse_n8n(blob)


def test_unknown_trigger_version_refused() -> None:
    with pytest.raises(ImportRefused) as extra:
        parse_trigger(json.dumps({"version": "99", "trigger": {}, "steps": []}))
    assert extra.value.reason == "version"


def test_out_path_confined(tmp_path: Path, tmp_settings) -> None:
    src = tmp_path / "n.json"
    src.write_text(n8n_export(), encoding="utf-8")
    outside = tmp_path.parent / "escape-import"
    with pytest.raises(ConfigError):
        import_workflow("n8n", src, out=outside, settings=tmp_settings)


def test_crewai_yaml_with_agent_substring_stays_yaml(tmp_path: Path, tmp_settings) -> None:
    text = (
        "name: research\n"
        "version: 1\n"
        "agents:\n"
        "  writer:\n"
        "    role: Writer\n"
        "tasks:\n"
        "  draft:\n"
        "    description: Write Agent(s) summary\n"
        "    agent: writer\n"
    )
    graph = parse_crewai(text, filename="crew.yaml")
    assert any(n.kind == "task" for n in graph.nodes)
    src = tmp_path / "crew.yaml"
    src.write_text(text, encoding="utf-8")
    result = import_workflow("crewai", src, out=tmp_path / "out", settings=tmp_settings)
    assert result.dry_run_ok is True
    assert "Write Agent(s) summary" in result.workflow_path.read_text(encoding="utf-8")


def test_secret_shaped_node_id_is_redacted(tmp_path: Path, tmp_settings) -> None:
    secret = "sk-abcdefghijklmnop"
    src = (
        "from langgraph.graph import StateGraph, START, END\n"
        "g = StateGraph(dict)\n"
        "g.add_node('greet', lambda s: s)\n"
        f"g.add_node('{secret}', lambda s: s)\n"
        "g.add_edge(START, 'greet')\n"
    )
    path = tmp_path / "g.py"
    path.write_text(src, encoding="utf-8")
    result = import_workflow("langgraph", path, out=tmp_path / "out", settings=tmp_settings)
    yaml_text = result.workflow_path.read_text(encoding="utf-8")
    report = result.report_path.read_text(encoding="utf-8")
    blob = yaml_text + report + json.dumps(result.workflow) + json.dumps(result.report.as_dict())
    for token in (secret, secret.replace("-", "_"), secret.replace("_", "-")):
        assert token not in blob
        assert token.lower() not in blob.lower() or token not in blob
