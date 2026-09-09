"""Decision records are a projection of a real run, not a hand-built store."""

from __future__ import annotations

from pathlib import Path

import pytest

from readyagents.audit import read_audit_events
from readyagents.compliance.decision import project_decisions
from readyagents.errors import ApprovalRequired
from readyagents.workflow.runner import load_workflow, resume_run, run_workflow_file


def test_every_node_type_gets_a_decision_record(tmp_path: Path, tmp_settings) -> None:
    wf = tmp_path / "w.yaml"
    wf.write_text(
        "name: all\n"
        "nodes:\n"
        "  - id: t\n    type: tool\n    tool: calc\n    arguments: {expression: '1+1'}\n"
        "    output_key: total\n    next: x\n"
        "  - id: x\n    type: transform\n    template: 'n={{total}}'\n    output_key: summary\n"
        "    next: c\n"
        "  - id: c\n    type: condition\n    when: total == 2\n    then: ok\n    else: ok\n"
        "  - id: ok\n    type: transform\n    template: 'ok'\n    output_key: done\n",
        encoding="utf-8",
    )
    state = run_workflow_file(wf, settings=tmp_settings, persist=True)
    spec = load_workflow(wf)
    audit = read_audit_events(tmp_settings.audit_dir(), state.run_id)
    records = project_decisions(state, spec, audit)
    kinds = {row.node_type for row in records}
    assert kinds >= {"tool", "transform", "condition"}
    assert [row.node_id for row in records] == [r.node_id for r in state.results]
    calc = next(row for row in records if row.node_id == "t")
    assert calc.output == 2
    assert calc.attempts >= 1
    assert calc.started_at
    assert calc.finished_at


def test_approval_confirm_and_reject(tmp_path: Path, tmp_settings) -> None:
    wf = tmp_path / "g.yaml"
    wf.write_text(
        "name: g\nnodes:\n"
        "  - id: gate\n    type: approval\n    prompt: go?\n    then: ok\n    else: denied\n"
        "  - id: ok\n    type: transform\n    template: paid\n    output_key: summary\n"
        "  - id: denied\n    type: transform\n    template: denied\n    output_key: summary\n",
        encoding="utf-8",
    )
    with pytest.raises(ApprovalRequired):
        run_workflow_file(wf, settings=tmp_settings, persist=True)
    approved = resume_run(
        next(p.stem for p in tmp_settings.runs_dir().glob("*.json")),
        settings=tmp_settings,
        decisions={"gate": "approve"},
    )
    spec = load_workflow(wf)
    audit = read_audit_events(tmp_settings.audit_dir(), approved.run_id)
    records = project_decisions(approved, spec, audit)
    human = next(row for row in records if row.node_id == "gate").human
    assert human is not None
    assert human.decision == "approve"
    assert human.outcome == "confirm"
    assert human.actor is None or isinstance(human.actor, str)
    assert human.signature_status in {"signed", "unsigned", "n/a"}
    assert human.timestamp

    wf2 = tmp_path / "g2.yaml"
    wf2.write_text(wf.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ApprovalRequired):
        run_workflow_file(wf2, settings=tmp_settings, persist=True)
    run_id = max(p.stat().st_mtime_ns for p in tmp_settings.runs_dir().glob("*.json"))
    newest = max(tmp_settings.runs_dir().glob("*.json"), key=lambda p: p.stat().st_mtime_ns)
    rejected = resume_run(newest.stem, settings=tmp_settings, decisions={"gate": "reject"})
    _ = run_id
    audit2 = read_audit_events(tmp_settings.audit_dir(), rejected.run_id)
    recs = project_decisions(rejected, spec, audit2)
    human2 = next(row for row in recs if row.node_id == "gate").human
    assert human2 is not None
    assert human2.decision == "reject"
    assert human2.outcome == "override"


def test_retries_and_foreach_occurrence(tmp_path: Path, tmp_settings) -> None:
    wf = tmp_path / "f.yaml"
    wf.write_text(
        "name: f\nnodes:\n"
        "  - id: mapped\n    type: foreach\n    items: items\n    output_key: outs\n"
        "    body:\n      id: inner\n      type: transform\n      template: 'v={{item}}'\n",
        encoding="utf-8",
    )
    state = run_workflow_file(wf, settings=tmp_settings, persist=True, inputs={"items": [1, 2]})
    spec = load_workflow(wf)
    records = project_decisions(state, spec, [])
    foreach = next(row for row in records if row.node_id == "mapped")
    assert foreach.node_type == "foreach"
    assert foreach.attempts >= 1
    assert isinstance(foreach.output, list)
    assert len(foreach.output) == 2
