"""runs gc refuses in-window records; override is audited; paused stays kept."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.audit import read_audit_events
from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.errors import ApprovalRequired
from readyagents.workflow.runner import run_workflow_file
from readyagents.workflow.state import gc_runs, list_runs

_runner = CliRunner()


def test_gc_refuses_in_window_records(tmp_path: Path, tmp_settings, monkeypatch) -> None:
    wf = tmp_path / "w.yaml"
    wf.write_text(
        "name: w\nnodes:\n  - id: a\n    type: transform\n    template: 'x'\n",
        encoding="utf-8",
    )
    state = run_workflow_file(wf, settings=tmp_settings, persist=True)
    deleted = gc_runs(
        tmp_settings.runs_dir(),
        min_age_seconds=180 * 86400,
        override_retention=False,
    )
    assert state.run_id not in deleted
    assert any(s.run_id == state.run_id for s in list_runs(tmp_settings.runs_dir()))
    forced = gc_runs(
        tmp_settings.runs_dir(),
        min_age_seconds=180 * 86400,
        override_retention=True,
    )
    assert state.run_id in forced


def test_gc_deletes_old_records(tmp_path: Path, tmp_settings) -> None:
    wf = tmp_path / "w.yaml"
    wf.write_text(
        "name: w\nnodes:\n  - id: a\n    type: transform\n    template: 'x'\n",
        encoding="utf-8",
    )
    state = run_workflow_file(wf, settings=tmp_settings, persist=True)
    old = (datetime.now(UTC) - timedelta(days=200)).isoformat()
    state.finished_at = old
    state.started_at = old
    from readyagents.workflow.state import persist_run

    persist_run(state, tmp_settings.runs_dir())
    deleted = gc_runs(tmp_settings.runs_dir(), min_age_seconds=180 * 86400)
    assert state.run_id in deleted


def test_cli_gc_override_is_audited(tmp_path: Path, tmp_settings, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    monkeypatch.setenv("READYAGENTS_WORKSPACE", str(tmp_path))
    clear_settings_cache()
    wf = tmp_path / "w.yaml"
    wf.write_text(
        "name: w\nnodes:\n  - id: a\n    type: transform\n    template: 'x'\n",
        encoding="utf-8",
    )
    state = run_workflow_file(wf, settings=tmp_settings, persist=True)
    blocked = _runner.invoke(app, ["runs", "gc", "--yes"])
    assert blocked.exit_code == 0, blocked.stdout + blocked.stderr
    assert "Deleted 0" in blocked.stdout
    assert (tmp_settings.runs_dir() / f"{state.run_id}.json").is_file()
    forced = _runner.invoke(app, ["runs", "gc", "--yes", "--override-retention"])
    assert forced.exit_code == 0, forced.stdout + forced.stderr
    assert state.run_id in forced.stdout
    events = read_audit_events(tmp_settings.audit_dir(), "gc")
    assert any(e.get("event") == "gc_override" for e in events)


def test_paused_still_kept(tmp_path: Path, tmp_settings) -> None:
    examples = Path(__file__).resolve().parents[1] / "examples" / "approval_gate.yaml"
    with pytest.raises(ApprovalRequired):
        run_workflow_file(examples, settings=tmp_settings, persist=True)
    deleted = gc_runs(
        tmp_settings.runs_dir(),
        include_paused=False,
        override_retention=True,
    )
    assert not deleted
    still = list_runs(tmp_settings.runs_dir(), status="paused")
    assert still
