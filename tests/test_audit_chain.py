"""Hash-chained audit. Drive shipped append_audit_event / make_auditor / CLI."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from typer.testing import CliRunner

from readyagents.audit import (
    append_audit_event,
    make_auditor,
    read_audit_events,
    verify_audit_file,
)
from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.workflow.runner import run_workflow_file

_runner = CliRunner()


def test_chain_links_on_real_append(tmp_path: Path) -> None:
    audit = tmp_path / "audit"
    append_audit_event(audit, {"run_id": "r1", "event": "run_started"})
    append_audit_event(audit, {"run_id": "r1", "event": "node_ok", "node_id": "a"})
    events = read_audit_events(audit, "r1")
    assert events[0]["seq"] == 1
    assert events[1]["seq"] == 2
    assert events[1]["prev_hash"] == events[0]["entry_hash"]
    report = verify_audit_file(audit / "r1.jsonl")
    assert report.ok
    assert report.chained == 2
    assert report.unchained == 0


def test_mutated_middle_detected_at_seq(tmp_path: Path) -> None:
    audit = tmp_path / "audit"
    path = append_audit_event(audit, {"run_id": "r1", "event": "a"})
    append_audit_event(audit, {"run_id": "r1", "event": "b"})
    append_audit_event(audit, {"run_id": "r1", "event": "c"})
    lines = path.read_text(encoding="utf-8").splitlines()
    middle = json.loads(lines[1])
    middle["event"] = "tampered"
    lines[1] = json.dumps(middle)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = verify_audit_file(path)
    assert report.ok is False
    assert report.first_break == 2
    assert "mismatch" in (report.first_break_reason or "")


def test_truncated_file_detected(tmp_path: Path) -> None:
    audit = tmp_path / "audit"
    path = append_audit_event(audit, {"run_id": "r1", "event": "a"})
    append_audit_event(audit, {"run_id": "r1", "event": "b"})
    text = path.read_text(encoding="utf-8")
    path.write_text(text[:-8], encoding="utf-8")
    report = verify_audit_file(path)
    assert report.ok is False
    assert report.first_break is not None
    assert "invalid JSON" in (report.first_break_reason or "") or "truncated" in (
        report.first_break_reason or ""
    )


def test_pre_chain_file_reported_unchained_not_failed(tmp_path: Path) -> None:
    path = tmp_path / "old.jsonl"
    path.write_text(
        json.dumps({"event": "run_started", "run_id": "r1"})
        + "\n"
        + json.dumps({"event": "node_ok", "run_id": "r1", "node_id": "a"})
        + "\n",
        encoding="utf-8",
    )
    report = verify_audit_file(path)
    assert report.ok is True
    assert report.unchained == 2
    assert report.chained == 0
    assert report.unchained_ranges


def test_rotation_anchor_verifies(tmp_path: Path) -> None:
    audit = tmp_path / "audit"
    append_audit_event(audit, {"run_id": "r1", "event": "a"}, rotate_bytes=80)
    append_audit_event(audit, {"run_id": "r1", "event": "b" * 40}, rotate_bytes=80)
    append_audit_event(audit, {"run_id": "r1", "event": "c" * 40}, rotate_bytes=80)
    files = sorted(audit.glob("r1*.jsonl"))
    assert len(files) >= 2
    for path in files:
        report = verify_audit_file(path)
        assert report.ok, (path, report.first_break_reason)
    rotated = {p.name for p in files if p.name != "r1.jsonl"}
    current = audit / "r1.jsonl"
    events = [
        json.loads(line)
        for line in current.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(e.get("event") == "chain_anchor" for e in events)
    assert any(e.get("prev_file") in rotated for e in events)


def test_parallel_appends_stay_a_valid_chain(tmp_path: Path) -> None:
    audit = tmp_path / "audit"

    def _one(i: int) -> None:
        append_audit_event(audit, {"run_id": "r1", "event": "node_ok", "node_id": str(i)})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_one, range(24)))
    report = verify_audit_file(audit / "r1.jsonl")
    assert report.ok, report.first_break_reason
    assert report.chained == 24
    events = read_audit_events(audit, "r1")
    hashes = [e["entry_hash"] for e in events]
    assert len(hashes) == len(set(hashes))


def test_make_auditor_and_runner_chain(tmp_path: Path, tmp_settings) -> None:
    auditor = make_auditor(tmp_path / "audit")
    auditor("run_started", run_id="x")
    auditor("node_ok", run_id="x", node_id="n")
    report = verify_audit_file(tmp_path / "audit" / "x.jsonl")
    assert report.ok
    wf = tmp_path / "w.yaml"
    wf.write_text(
        "name: w\nnodes:\n  - id: a\n    type: transform\n    template: 'one'\n",
        encoding="utf-8",
    )
    state = run_workflow_file(wf, settings=tmp_settings, persist=True)
    chained = verify_audit_file(tmp_settings.home_path() / "audit" / f"{state.run_id}.jsonl")
    assert chained.ok
    assert chained.chained >= 2


def test_audit_verify_cli(tmp_path: Path, tmp_settings, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    audit = tmp_settings.home_path() / "audit"
    append_audit_event(audit, {"run_id": "r1", "event": "ok"})
    ok = _runner.invoke(app, ["audit", "verify", "--file", str(audit / "r1.jsonl"), "--json"])
    assert ok.exit_code == 0, ok.stdout + ok.stderr
    payload = json.loads(ok.stdout[ok.stdout.find("{") :])
    assert payload["ok"] is True
    path = audit / "r1.jsonl"
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    row["event"] = "nope"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    bad = _runner.invoke(app, ["audit", "verify", "--file", str(path), "--json"])
    assert bad.exit_code == 1
    broken = json.loads(bad.stdout[bad.stdout.find("{") :])
    assert broken["ok"] is False
    assert broken.get("first_break") is not None
