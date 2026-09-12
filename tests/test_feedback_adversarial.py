"""Adversarial feedback: consent bypass, residual secrets, identity leak, stats honesty."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.feedback.capture import record_human_correction
from readyagents.feedback.consent import permits
from readyagents.feedback.export import export_feedback
from readyagents.feedback.stats import feedback_stats
from readyagents.workflow.schema import NodeSpec
from readyagents.workflow.state import RunState

runner = CliRunner()


def _node() -> NodeSpec:
    return NodeSpec.model_validate(
        {
            "id": "gate",
            "type": "approval",
            "prompt": "ok?",
            "then": "ok",
            "else": "no",
            "feedback": {
                "allow_edit": True,
                "labels": ["wrong_fact"],
                "consent": "internal_training",
            },
        }
    )


def test_unconsented_run_excluded_from_every_format(tmp_settings, tmp_path: Path) -> None:
    state = RunState.start("plain", {})
    state.metadata["source"] = "x.yaml"
    # correction without consent scope / data_policy
    node = NodeSpec.model_validate(
        {
            "id": "gate",
            "type": "approval",
            "prompt": "ok?",
            "then": "ok",
            "else": "no",
            "feedback": {"allow_edit": True, "labels": ["wrong_fact"]},
        }
    )
    record_human_correction(
        state,
        node=node,
        actor_role="editor",
        reason="x",
        original="a",
        edited="b",
        label="wrong_fact",
        decision_id="d",
    )
    assert not permits(state, None)
    from readyagents.run_store import open_run_store

    store = open_run_store(tmp_settings)
    try:
        store.save(state)
    finally:
        store.close()
    for fmt in ("eval", "sft", "dpo"):
        dest = tmp_path / f"out.{fmt}"
        report = export_feedback(settings=tmp_settings, dest=dest, fmt=fmt, yes=True)
        assert report.written == 0
        assert report.excluded_unconsented >= 1
        assert report.excluded >= 1


def test_no_consent_cli_flag(tmp_path: Path) -> None:
    help_text = runner.invoke(app, ["feedback", "export", "--help"])
    assert help_text.exit_code == 0
    assert "--consent" not in help_text.stdout
    assert "--allow-unconsented" not in help_text.stdout


def test_residual_secret_excludes_whole_record(tmp_settings, tmp_path: Path) -> None:
    state = RunState.start("flow", {})
    state.metadata["source"] = "x.yaml"
    node = _node()
    record_human_correction(
        state,
        node=node,
        actor_role="editor",
        reason="keep sk-abcdefghijksecret in the edit",
        original="safe",
        edited="sk-abcdefghijksecret leaked",
        label="wrong_fact",
        decision_id="d",
    )
    from readyagents.run_store import open_run_store

    store = open_run_store(tmp_settings)
    try:
        store.save(state)
    finally:
        store.close()
    dest = tmp_path / "secret.yaml"

    class _Noop:
        def redact(self, value):
            return value

        def redact_text(self, text):
            return text

    report = export_feedback(
        settings=tmp_settings,
        dest=dest,
        fmt="eval",
        yes=True,
        secrets=["sk-abcdefghijksecret"],
        redactor=_Noop(),
    )
    assert report.written == 0
    assert report.excluded_secret >= 1
    if dest.is_file():
        assert "sk-abcdefghijksecret" not in dest.read_text(encoding="utf-8")


def test_reviewer_identity_not_in_export(tmp_settings, tmp_path: Path) -> None:
    state = RunState.start("flow", {})
    state.metadata["source"] = "x.yaml"
    node = _node()
    record_human_correction(
        state,
        node=node,
        actor_role="editor",
        reason="because Alice said so",
        original="one",
        edited="two",
        label="wrong_fact",
        decision_id="d",
    )
    # role only on the correction object
    from readyagents.run_store import open_run_store

    store = open_run_store(tmp_settings)
    try:
        store.save(state)
    finally:
        store.close()
    dest = tmp_path / "id.jsonl"
    report = export_feedback(settings=tmp_settings, dest=dest, fmt="sft", yes=True)
    assert report.written == 1
    text = dest.read_text(encoding="utf-8")
    assert "editor" in text
    assert '"actor"' not in text
    assert "alice@" not in text.lower()


def test_export_path_escape_refused(tmp_settings) -> None:
    from readyagents.errors import ConfigError, FeedbackRefused, PathError

    try:
        export_feedback(settings=tmp_settings, dest="/tmp/escape.yaml", fmt="eval", yes=True)
        raise AssertionError("escape must be refused")
    except (FeedbackRefused, ConfigError, PathError):
        pass


def test_stats_n1_no_significance(tmp_settings) -> None:
    state = RunState.start("flow", {})
    node = _node()
    record_human_correction(
        state,
        node=node,
        actor_role="editor",
        reason="x",
        original="a",
        edited="b",
        label="wrong_fact",
        decision_id="d",
    )
    from readyagents.run_store import open_run_store

    store = open_run_store(tmp_settings)
    try:
        store.save(state)
    finally:
        store.close()
    report = feedback_stats(settings=tmp_settings, by="node")
    assert report.sample_size >= 1
    assert report.as_dict()["significance"] is None
    assert report.rows[0]["n"] >= 1
    assert report.rows[0]["significance"] is None
