from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from readyagents.errors import ForkError
from readyagents.replay.fork import reconstruct_after
from readyagents.workflow.runner import load_workflow, run_workflow_file
from readyagents.workflow.state import parse_input_pairs


def _copy_composed(tmp_path: Path, examples_dir: Path) -> Path:
    shutil.copy(examples_dir / "composed_gate.yaml", tmp_path / "composed_gate.yaml")
    shutil.copy(examples_dir / "included_min.yaml", tmp_path / "included_min.yaml")
    return tmp_path / "composed_gate.yaml"


def test_fork_composed_gate_state(tmp_path: Path, tmp_settings, examples_dir: Path) -> None:
    path = _copy_composed(tmp_path, examples_dir)
    parent = run_workflow_file(
        path,
        settings=tmp_settings,
        persist=True,
        inputs={"n": 8},
        decisions={"gate": "approve"},
    )
    assert parent.status == "succeeded"
    spec = load_workflow(path)
    child = reconstruct_after(parent, "nested", workflow=spec)
    assert child.run_id != parent.run_id
    assert child.metadata["forked_from"] == parent.run_id
    assert child.metadata["forked_at_node"] == "nested"
    assert "nested" in child.node_outputs
    assert child.node_outputs["nested"] == parent.node_outputs["nested"]
    parent_path = tmp_settings.runs_dir() / f"{parent.run_id}.json"
    audit_path = tmp_settings.audit_dir() / f"{parent.run_id}.jsonl"
    parent_bytes = parent_path.read_bytes()
    audit_bytes = audit_path.read_bytes() if audit_path.is_file() else b""
    from readyagents.replay.fork import fork_run

    fork_run(
        parent.run_id,
        "nested",
        settings=tmp_settings,
        persist=True,
        decisions={"gate": "approve"},
    )
    assert parent_path.read_bytes() == parent_bytes
    assert audit_path.is_file()
    assert audit_path.read_bytes() == audit_bytes


def test_fork_foreach_occurrence(tmp_path: Path, tmp_settings, examples_dir: Path) -> None:
    shutil.copy(examples_dir / "foreach_calc.yaml", tmp_path / "foreach_calc.yaml")
    path = tmp_path / "foreach_calc.yaml"
    parent = run_workflow_file(path, settings=tmp_settings, persist=True)
    spec = load_workflow(path)
    with pytest.raises(ForkError, match="never ran"):
        reconstruct_after(parent, "missing", workflow=spec)
    child = reconstruct_after(parent, "each", workflow=spec)
    assert child.node_outputs["each"] == parent.node_outputs["each"]


def test_fork_parallel_interior_refused(tmp_path: Path, tmp_settings, examples_dir: Path) -> None:
    path = _copy_composed(tmp_path, examples_dir)
    parent = run_workflow_file(
        path,
        settings=tmp_settings,
        persist=True,
        inputs={"n": 8},
        decisions={"gate": "approve"},
    )
    spec = load_workflow(path)
    with pytest.raises(ForkError, match="parallel-branch interior"):
        reconstruct_after(parent, "extra", workflow=spec)


def test_set_cannot_satisfy_approval(tmp_path: Path, tmp_settings, examples_dir: Path) -> None:
    from readyagents.errors import ApprovalRequired
    from readyagents.replay.fork import fork_run

    path = _copy_composed(tmp_path, examples_dir)
    parent = run_workflow_file(
        path,
        settings=tmp_settings,
        persist=True,
        inputs={"n": 8},
        decisions={"gate": "approve"},
    )
    with pytest.raises(ApprovalRequired):
        fork_run(
            parent.run_id,
            "nested",
            settings=tmp_settings,
            persist=True,
            overrides=parse_input_pairs(["gate=approve", "approved=true"]),
        )
