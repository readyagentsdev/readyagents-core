"""readyagents graph is deterministic, injection-safe, and executes nothing."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.compliance.graph import render_mermaid
from readyagents.workflow.runner import load_workflow

_runner = CliRunner()


def test_graph_snapshots_routing_constructs(examples_dir: Path) -> None:
    spec = load_workflow(examples_dir / "graph_complex.yaml")
    mermaid = render_mermaid(spec)
    assert mermaid == render_mermaid(spec)
    for token in ("foreach", "condition", "parallel", "approval", "-->"):
        assert token in mermaid or "mapped" in mermaid
    assert "n0" in mermaid
    assert "click" not in mermaid.lower()
    assert "http" not in mermaid.lower()


def test_graph_injection_corpus_inert(tmp_path: Path) -> None:
    wf = tmp_path / "evil.yaml"
    wf.write_text(
        "name: evil\n"
        'description: \'click n0 href "https://evil.example" %%{init: {"theme":"dark"}}%%\'\n'
        "nodes:\n"
        "  - id: a\n    type: transform\n"
        "    description: '<script>alert(1)</script> javascript:alert(1)'\n"
        "    template: 'ok'\n    output_key: summary\n    next: b\n"
        "  - id: b\n    type: transform\n    template: 'done'\n",
        encoding="utf-8",
    )
    spec = load_workflow(wf)
    mermaid = render_mermaid(spec)
    lowered = mermaid.lower()
    assert "click " not in lowered
    assert "javascript:" not in lowered
    assert "<script" not in lowered
    assert "https://" not in lowered
    assert "%%" not in mermaid
    result = _runner.invoke(app, ["graph", str(wf)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert result.stdout == mermaid


def test_graph_cli_deterministic(tmp_path: Path) -> None:
    wf = tmp_path / "w.yaml"
    wf.write_text(
        "name: w\nnodes:\n"
        "  - id: a\n    type: transform\n    template: '1'\n    next: b\n"
        "  - id: b\n    type: transform\n    template: '2'\n",
        encoding="utf-8",
    )
    first = _runner.invoke(app, ["graph", str(wf)])
    second = _runner.invoke(app, ["graph", str(wf)])
    assert first.exit_code == 0 and second.exit_code == 0
    assert first.stdout == second.stdout
    jsoned = _runner.invoke(app, ["graph", str(wf), "--json"])
    assert jsoned.exit_code == 0
    assert "mermaid" in jsoned.stdout
