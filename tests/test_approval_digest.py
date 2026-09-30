"""Approval prompt digest: approve-then-edit refusal (reapprove_required)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.errors import REAPPROVE_EXIT_CODE, ApprovalRequired, ReapprovalRequired
from readyagents.workflow.nodes import approval_prompt_digest
from readyagents.workflow.runner import resume_run, run_workflow_file

runner = CliRunner()

WF = """
name: dg
start: g
nodes:
  - id: g
    type: approval
    prompt: "{prompt}"
    then: ok
    else: denied
  - id: ok
    type: transform
    template: "ok"
    output_key: summary
  - id: denied
    type: transform
    template: "denied"
    output_key: summary
"""


def _write(path: Path, prompt: str) -> Path:
    path.write_text(WF.format(prompt=prompt), encoding="utf-8")
    return path


def _run_file(home: Path, run_id: str) -> Path:
    matches = list(home.rglob(f"runs/{run_id}.json"))
    assert len(matches) == 1, matches
    return matches[0]


def _audit_events(home: Path, run_id: str) -> list[dict]:
    lines = []
    for path in home.rglob(f"audit/{run_id}.jsonl"):
        lines += [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
    return lines


def test_pause_stores_digest_in_run_file_and_audit(tmp_settings) -> None:
    path = _write(tmp_settings.workspace_path() / "dg.yaml", "Pay 42?")
    with pytest.raises(ApprovalRequired) as paused:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    run_id = paused.value.run_id
    want = approval_prompt_digest("Pay 42?")
    record = json.loads(_run_file(tmp_settings.home, run_id).read_text(encoding="utf-8"))
    assert record["pending"]["prompt_sha256"] == want
    events = [e for e in _audit_events(tmp_settings.home, run_id) if e["event"] == "paused"]
    assert events and events[-1]["prompt_sha256"] == want


def test_matching_digest_resumes(tmp_settings) -> None:
    path = _write(tmp_settings.workspace_path() / "dg.yaml", "Pay 42?")
    with pytest.raises(ApprovalRequired) as paused:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    state = resume_run(paused.value.run_id, settings=tmp_settings, decisions={"g": "approve"})
    assert state.status == "succeeded"
    assert state.output_keys["summary"] == "ok"


def test_changed_prompt_refuses_then_reapproves(tmp_settings) -> None:
    path = _write(tmp_settings.workspace_path() / "dg.yaml", "Pay 42?")
    with pytest.raises(ApprovalRequired) as paused:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    run_id = paused.value.run_id
    _write(path, "Pay 4200?")
    with pytest.raises(ReapprovalRequired) as refused:
        resume_run(run_id, settings=tmp_settings, decisions={"g": "approve"})
    exc = refused.value
    assert exc.reason == "reapprove_required"
    assert exc.expected_sha256 == approval_prompt_digest("Pay 42?")
    assert exc.actual_sha256 == approval_prompt_digest("Pay 4200?")
    assert exc.state.status == "paused"
    assert exc.state.pending["prompt_sha256"] == exc.actual_sha256
    refusals = [
        e for e in _audit_events(tmp_settings.home, run_id) if e["event"] == "decision_refused"
    ]
    assert refusals and refusals[-1]["reason"] == "reapprove_required"
    # Approving the prompt now on screen goes through.
    state = resume_run(run_id, settings=tmp_settings, decisions={"g": "approve"})
    assert state.status == "succeeded"


def test_reject_is_never_blocked_by_digest(tmp_settings) -> None:
    path = _write(tmp_settings.workspace_path() / "dg.yaml", "Pay 42?")
    with pytest.raises(ApprovalRequired) as paused:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    _write(path, "Pay 4200?")
    state = resume_run(paused.value.run_id, settings=tmp_settings, decisions={"g": "reject"})
    assert state.status == "succeeded"
    assert state.output_keys["summary"] == "denied"


def test_legacy_run_without_digest_resumes_with_warning(
    tmp_settings, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(tmp_settings.workspace_path() / "dg.yaml", "Pay 42?")
    with pytest.raises(ApprovalRequired) as paused:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    run_id = paused.value.run_id
    run_file = _run_file(tmp_settings.home, run_id)
    record = json.loads(run_file.read_text(encoding="utf-8"))
    del record["pending"]["prompt_sha256"]
    run_file.write_text(json.dumps(record), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        state = resume_run(run_id, settings=tmp_settings, decisions={"g": "approve"})
    assert state.status == "succeeded"
    assert "paused before 2.0.11" in caplog.text


def test_cli_mismatch_exit_code_and_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / ".readyagents"
    monkeypatch.setenv("READYAGENTS_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    path = _write(tmp_path / "dg.yaml", "Pay 42?")
    paused = runner.invoke(app, ["run", str(path), "--json"])
    assert paused.exit_code == 2, paused.stdout + paused.stderr
    run_id = json.loads(paused.stdout)["run_id"]
    _write(path, "Pay 4200?")
    refused = runner.invoke(app, ["resume", run_id, "--approve", "g", "--json"])
    assert REAPPROVE_EXIT_CODE == 3
    assert refused.exit_code == REAPPROVE_EXIT_CODE, refused.stdout + refused.stderr
    payload = json.loads(refused.stdout)
    assert payload["ok"] is False
    assert payload["error"] == "ReapprovalRequired"
    assert payload["reason"] == "reapprove_required"
    assert payload["status"] == "paused"
    assert payload["prompt"] == "Pay 4200?"
    assert payload["expected_sha256"] == approval_prompt_digest("Pay 42?")
    # Text mode: reason on stderr, same exit code. Re-edit so it mismatches again.
    _write(path, "Pay 9?")
    text = runner.invoke(app, ["resume", run_id, "--approve", "g"])
    assert text.exit_code == REAPPROVE_EXIT_CODE
    assert "reapprove_required" in text.stderr
    ok = runner.invoke(app, ["resume", run_id, "--approve", "g"])
    assert ok.exit_code == 0, ok.stdout + ok.stderr
    clear_settings_cache()
