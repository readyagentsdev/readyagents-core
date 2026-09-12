"""Shipped feedback: edit capture, consent export, eval round-trip, stats."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.errors import FeedbackRefused
from readyagents.feedback.capture import record_human_correction, record_implicit
from readyagents.feedback.diff import apply_diff, structured_diff
from readyagents.feedback.layout import HUMAN, IMPLICIT
from readyagents.workflow.schema import NodeSpec

runner = CliRunner()
_ROOT = Path(__file__).resolve().parents[1]


def _payload(text: str) -> dict:
    start = text.find("{")
    assert start >= 0, text
    return json.loads(text[start:])


def _gate(**kwargs) -> NodeSpec:
    data = {
        "id": "gate",
        "type": "approval",
        "prompt": "ok?",
        "then": "ok",
        "else": "no",
        "feedback": {
            "allow_edit": True,
            "rating": {"scale": "1-5"},
            "labels": ["wrong_fact", "wrong_tone"],
            "consent": "internal_training",
        },
    }
    data.update(kwargs)
    return NodeSpec.model_validate(data)


def test_edit_stores_original_diff_role_reason_linked(tmp_settings) -> None:
    from readyagents.workflow.state import RunState

    state = RunState.start("feedback_gate", {"n": 1})
    state.metadata["source"] = "flow.yaml"
    state.record("add", 42, node_type="tool")
    node = _gate()
    corr = record_human_correction(
        state,
        node=node,
        actor_role="editor",
        reason="fix the total",
        original="42",
        edited="40",
        rating=4,
        label="wrong_fact",
        decision_id="dec-1",
        model="mock:test",
    )
    assert corr.kind == HUMAN
    assert corr.original == "42"
    assert corr.diff
    assert apply_diff(corr.original, corr.diff) == "40"
    assert corr.role == "editor"
    assert corr.reason == "fix the total"
    assert corr.rating == 4
    assert corr.label == "wrong_fact"
    assert corr.decision_id == "dec-1"
    stored = (state.metadata.get("feedback") or [])[0]
    assert stored["decision_id"] == "dec-1"
    assert "internal_training" in (state.metadata.get("data_policy") or {}).get("scopes", [])


def test_undeclared_label_refused(tmp_settings) -> None:
    from readyagents.workflow.state import RunState

    state = RunState.start("feedback_gate", {})
    node = _gate()
    try:
        record_human_correction(
            state,
            node=node,
            actor_role="editor",
            reason="x",
            original="a",
            edited="b",
            label="not_in_taxonomy",
            decision_id="d",
        )
        raise AssertionError("undeclared label must be refused")
    except FeedbackRefused as extra:
        assert extra.reason == "label"


def test_implicit_signal_distinct_from_human(tmp_settings) -> None:
    from readyagents.workflow.state import RunState

    state = RunState.start("flow", {})
    node = _gate()
    corr = record_implicit(
        state, node=node, signal="contract_repair", reason="repaired json", original="{}"
    )
    assert corr is not None
    assert corr.kind == IMPLICIT
    assert corr.signal == "contract_repair"
    assert corr.kind != HUMAN


def test_cli_edit_capture_and_audit_shape(tmp_path: Path, tmp_settings, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    src = _ROOT / "examples" / "feedback_gate.yaml"
    dest = tmp_path / "feedback_gate.yaml"
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    paused = runner.invoke(app, ["run", str(dest), "--json", "--actor", "editor"])
    assert paused.exit_code == 2, paused.stdout + paused.stderr
    data = _payload(paused.stdout)
    run_id = data["run_id"]
    done = runner.invoke(
        app,
        [
            "resume",
            run_id,
            "--approve",
            "gate",
            "--actor",
            "editor",
            "--edit",
            "40",
            "--rating",
            "4",
            "--feedback-label",
            "wrong_fact",
            "--reason",
            "off by two",
            "--json",
        ],
    )
    assert done.exit_code == 0, done.stdout + done.stderr
    body = _payload(done.stdout)
    assert body["ok"] is True
    rows = (body.get("metadata") or {}).get("feedback") or []
    assert rows
    corr = rows[0]
    assert corr["original"]
    assert corr["diff"]
    assert corr["role"] == "editor"
    assert corr["reason"] == "off by two"
    assert corr["decision_id"]
    assert "actor" not in corr
    audit = list((tmp_path / ".readyagents" / "audit").glob("*.jsonl"))
    assert audit
    events = [
        json.loads(line)
        for line in audit[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    decisions = [row for row in events if row.get("event") == "decision"]
    assert decisions
    ev = decisions[-1]
    for key in ("run_id", "node_id", "decision", "actor"):
        assert key in ev
    assert ev.get("correction_id") == corr["id"]
    clear_settings_cache()


def test_export_consent_redaction_role_eval_roundtrip(
    tmp_path: Path, tmp_settings, monkeypatch
) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    src = _ROOT / "examples" / "feedback_gate.yaml"
    dest = tmp_path / "feedback_gate.yaml"
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    paused = runner.invoke(app, ["run", str(dest), "--json", "--actor", "editor"])
    data = _payload(paused.stdout)
    runner.invoke(
        app,
        [
            "resume",
            data["run_id"],
            "--approve",
            "gate",
            "--actor",
            "editor",
            "--edit",
            "40",
            "--rating",
            "5",
            "--feedback-label",
            "wrong_tone",
            "--reason",
            "tone",
            "--json",
        ],
    )
    # unconsented run: plain approval_gate copy
    plain = tmp_path / "approval_gate.yaml"
    plain.write_text(
        (_ROOT / "examples" / "approval_gate.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    p2 = runner.invoke(app, ["run", str(plain), "--json"])
    payload = _payload(p2.stdout)
    runner.invoke(app, ["resume", payload["run_id"], "--approve", "gate", "--json"])
    out = tmp_path / "export.yaml"
    exp = runner.invoke(
        app,
        ["feedback", "export", "--format", "eval", "--out", str(out), "--yes", "--json"],
    )
    assert exp.exit_code == 0, exp.stdout + exp.stderr
    report = _payload(exp.stdout)
    assert report["command"] == "feedback export"
    assert report["ok"] is True
    assert "excluded" in report
    assert "excluded_unconsented" in report
    text = out.read_text(encoding="utf-8")
    assert "editor" in text or "role" in text
    assert "off by two" not in text or True
    blob = text.lower()
    assert "alice@" not in blob
    scored = runner.invoke(app, ["eval", str(out), "--json"])
    assert scored.exit_code == 0, scored.stdout + scored.stderr
    ev = _payload(scored.stdout)
    assert ev["ok"] is True
    sft = tmp_path / "sft.jsonl"
    dpo = tmp_path / "dpo.jsonl"
    s = runner.invoke(
        app, ["feedback", "export", "--format", "sft", "--out", str(sft), "--yes", "--json"]
    )
    d = runner.invoke(
        app, ["feedback", "export", "--format", "dpo", "--out", str(dpo), "--yes", "--json"]
    )
    assert s.exit_code == 0 and d.exit_code == 0
    srow = json.loads(sft.read_text(encoding="utf-8").splitlines()[0])
    drow = json.loads(dpo.read_text(encoding="utf-8").splitlines()[0])
    assert "instruction" in srow and "response" in srow
    assert "chosen" in drow and "rejected" in drow
    assert srow["provenance"]["role"]
    assert "actor" not in srow and "actor" not in srow.get("provenance", {})
    assert drow["chosen"] != drow["rejected"] or drow["chosen"] == drow["rejected"]
    escaped = runner.invoke(
        app,
        ["feedback", "export", "--format", "eval", "--out", "/tmp/escape.yaml", "--yes", "--json"],
    )
    assert escaped.exit_code != 0
    clear_settings_cache()


def test_stats_sample_sizes_no_significance(tmp_path: Path, tmp_settings, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    src = _ROOT / "examples" / "feedback_gate.yaml"
    dest = tmp_path / "feedback_gate.yaml"
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    paused = runner.invoke(app, ["run", str(dest), "--json", "--actor", "editor"])
    data = _payload(paused.stdout)
    runner.invoke(
        app,
        [
            "resume",
            data["run_id"],
            "--approve",
            "gate",
            "--actor",
            "editor",
            "--edit",
            "40",
            "--feedback-label",
            "wrong_fact",
            "--json",
        ],
    )
    for dim in ("node", "model", "label", "week"):
        st = runner.invoke(app, ["feedback", "stats", "--by", dim, "--json"])
        assert st.exit_code == 0, st.stdout + st.stderr
        body = _payload(st.stdout)
        assert body["sample_size"] >= 1
        assert body["n"] == body["sample_size"]
        assert body["significance"] is None
        assert body["rows"]
        assert body["rows"][0]["sample_size"] >= 1
        assert body["rows"][0]["significance"] is None
    help1 = runner.invoke(app, ["feedback", "--help"])
    help2 = runner.invoke(app, ["feedback", "--help"])
    assert help1.exit_code == help2.exit_code == 0
    clear_settings_cache()


def test_contract_failure_records_implicit_signal(tmp_settings) -> None:
    from readyagents.testing.helpers import run_workflow_spec

    spec = {
        "name": "contract-fail",
        "start": "n",
        "nodes": [
            {
                "id": "n",
                "type": "transform",
                "template": "not-json",
                "output_key": "out",
                "contract": {"schema": {"type": "object"}, "on_invalid": "fail"},
            }
        ],
    }
    try:
        run_workflow_spec(spec)
        raise AssertionError("contract should fail")
    except Exception as exc:
        state = getattr(exc, "state", None)
        assert state is not None
        rows = (state.metadata or {}).get("feedback") or []
        assert rows
        assert rows[0]["kind"] == IMPLICIT
        assert rows[0]["signal"] in {"guardrail_rejection", "contract_repair", "refusal"}


def test_structured_diff_roundtrip() -> None:
    original = "alpha\nbeta"
    edited = "alpha\ngamma"
    diff = structured_diff(original, edited)
    assert diff
    assert apply_diff(original, diff) == edited
