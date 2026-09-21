from __future__ import annotations

from pathlib import Path

from readyagents.report import render_html, write_html_report
from readyagents.tools import ToolRegistry
from readyagents.workflow.engine import run_workflow
from readyagents.workflow.nodes import ExecutionContext
from readyagents.workflow.schema import WorkflowSpec
from readyagents.workflow.state import RunState


def test_html_report_contains_timeline_and_usage(tmp_path: Path) -> None:
    spec = WorkflowSpec.model_validate(
        {
            "name": "rep",
            "nodes": [
                {
                    "id": "t",
                    "type": "transform",
                    "template": "hello-report",
                    "output_key": "summary",
                }
            ],
        }
    )
    state = run_workflow(spec, {}, ExecutionContext(spec, ToolRegistry()))
    html = render_html(state)
    assert state.run_id in html
    assert "hello-report" in html
    assert "<table" in html
    assert "timeline" in html.lower() or "Node" in html
    dest = tmp_path / "run.html"
    written = write_html_report(state, dest)
    assert written.is_file()
    text = written.read_text(encoding="utf-8")
    assert "ReadyAgents run" in text
    assert state.run_id in text


def test_html_report_renders_answers_and_confidence() -> None:
    """A node output with answers and confidence is a table, not only a dict dump."""
    state = RunState.start("rep", {})
    state.record(
        "triage",
        {
            "decider": "shim",
            "answers": {
                "department": {
                    "type": "choice",
                    "value": "billing",
                    "confidence": 0.596,
                }
            },
            "min_confidence": 0.85,
            "routed": "human_review",
        },
        node_type="decide",
    )
    state.finish("succeeded")
    page = render_html(state)
    assert "billing" in page
    assert "0.596" in page
    assert "<th>confidence</th>" in page
    assert "min_confidence 0.85" in page
    assert "{'decider':" not in page
