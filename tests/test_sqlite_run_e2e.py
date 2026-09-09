"""SQLite-only run/resume/decide/replay with no JSON run files."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import Settings, clear_settings_cache
from readyagents.errors import ApprovalRequired
from readyagents.workflow.runner import replay_run, resume_run, run_workflow_file

runner = CliRunner()
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _sqlite_settings(tmp_path: Path) -> Settings:
    clear_settings_cache()
    return Settings(
        home=tmp_path / ".readyagents",
        workspace=tmp_path,
        run_store="sqlite",
        allow_http=False,
        default_model="openai:gpt-4o-mini",
        openai_api_key=None,
        anthropic_api_key=None,
        _env_file=(),  # type: ignore[call-arg]
    )


def _json_files(settings: Settings) -> list[Path]:
    runs = settings.runs_dir()
    if not runs.is_dir():
        return []
    return [p for p in runs.glob("*.json") if not p.name.startswith(".")]


def test_sqlite_only_run_resume_no_json(tmp_path: Path) -> None:
    settings = _sqlite_settings(tmp_path)
    with pytest.raises(ApprovalRequired) as exc:
        run_workflow_file(EXAMPLES / "approval_gate.yaml", settings=settings, persist=True)
    run_id = exc.value.run_id
    assert _json_files(settings) == []
    assert settings.run_db_path().is_file()
    state = resume_run(run_id, settings=settings, decisions={"gate": "approve"})
    assert state.status == "succeeded"
    assert "approval_gate ok" in str(state.output_keys.get("summary"))
    assert _json_files(settings) == []


def test_sqlite_only_decide_and_replay(tmp_path: Path) -> None:
    settings = _sqlite_settings(tmp_path)
    with pytest.raises(ApprovalRequired) as exc:
        run_workflow_file(EXAMPLES / "approval_gate.yaml", settings=settings, persist=True)
    run_id = exc.value.run_id
    assert _json_files(settings) == []
    decided = resume_run(run_id, settings=settings, decisions={"gate": "reject"})
    assert decided.status == "succeeded"
    assert "denied" in str(decided.output_keys.get("summary"))
    replayed = replay_run(run_id, settings=settings, persist=True, decisions={"gate": "approve"})
    assert replayed.status == "succeeded"
    assert replayed.run_id != run_id
    assert _json_files(settings) == []


def test_cli_sqlite_run_resume_decide(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / ".readyagents"
    monkeypatch.setenv("READYAGENTS_HOME", str(home))
    monkeypatch.setenv("READYAGENTS_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("READYAGENTS_RUN_STORE", "sqlite")
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    paused = runner.invoke(app, ["run", str(EXAMPLES / "approval_gate.yaml")])
    assert paused.exit_code == 2, paused.stdout + paused.stderr
    text = paused.stdout + paused.stderr
    import re

    match = re.search(r"resume ([0-9a-f]{16,})", text)
    assert match, text
    run_id = match.group(1)
    json_runs = list((home / "runs").glob("*.json")) if (home / "runs").is_dir() else []
    assert json_runs == []
    decided = runner.invoke(app, ["decide", run_id, "--node", "gate", "--decision", "approve"])
    assert decided.exit_code == 0, decided.stdout + decided.stderr
    assert "approval_gate ok" in decided.stdout
    replayed = runner.invoke(app, ["runs", "replay", run_id, "--approve", "gate"])
    assert replayed.exit_code == 0, replayed.stdout + replayed.stderr
