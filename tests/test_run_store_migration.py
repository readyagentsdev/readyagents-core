from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import Settings, clear_settings_cache
from readyagents.errors import ApprovalRequired, ConfigError
from readyagents.run_store.migrate import migrate_json_to_sqlite
from readyagents.run_store.sqlite_store import SQLiteRunStore
from readyagents.workflow.runner import run_workflow_file
from readyagents.workflow.state import RunState, persist_run

runner = CliRunner()
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _plain(text: str) -> str:
    """Strip ANSI and whitespace so Rich wrapping cannot hide tokens."""
    return re.sub(r"\s+", "", re.sub(r"\x1b\[[0-9;]*m", "", text))


def _settings(tmp_path: Path) -> Settings:
    clear_settings_cache()
    return Settings(
        home=tmp_path / ".readyagents",
        workspace=tmp_path,
        allow_http=False,
        default_model="openai:gpt-4o-mini",
        openai_api_key=None,
        anthropic_api_key=None,
        _env_file=(),  # type: ignore[call-arg]
    )


def _write_run(
    runs: Path, *, status: str = "succeeded", run_id: str | None = None, **inputs
) -> RunState:
    state = RunState.start("wf", inputs or {"n": 1}, run_id=run_id)
    state.status = status
    persist_run(state, runs)
    return state


def _snapshots(runs: Path) -> dict[str, bytes]:
    return {
        p.name: p.read_bytes() for p in sorted(runs.glob("*.json")) if not p.name.startswith(".")
    }


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runs = settings.runs_dir()
    _write_run(runs)
    dest = settings.run_db_path()
    before = _snapshots(runs)
    report = migrate_json_to_sqlite(settings=settings, dry_run=True)
    assert report.ok is True
    assert report.scanned == 1
    assert report.imported == 0
    assert report.dry_run is True
    assert not dest.exists()
    assert _snapshots(runs) == before


def test_clean_import_verifies(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runs = settings.runs_dir()
    state = _write_run(runs, hello="世界", blob="x" * 8000)
    before = _snapshots(runs)
    report = migrate_json_to_sqlite(settings=settings)
    assert report.ok is True
    assert report.imported == 1
    assert report.verified is True
    loaded = SQLiteRunStore(settings.run_db_path()).get(state.run_id, allow_prefix=False)
    assert loaded.state.inputs["hello"] == "世界"
    assert loaded.state.inputs["blob"] == "x" * 8000
    assert _snapshots(runs) == before


def test_invalid_source_aborts_before_writes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runs = settings.runs_dir()
    runs.mkdir(parents=True)
    good = _write_run(runs)
    bad = runs / "nope.json"
    bad.write_text("{not json", encoding="utf-8")
    dest = settings.run_db_path()
    before = _snapshots(runs)
    report = migrate_json_to_sqlite(settings=settings)
    assert report.ok is False
    assert report.invalid == 1
    assert report.imported == 0
    assert not dest.exists()
    assert _snapshots(runs) == before
    _ = good


def test_skip_invalid_reports_counts(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runs = settings.runs_dir()
    _write_run(runs)
    (runs / "bad.json").write_text("{", encoding="utf-8")
    before = _snapshots(runs)
    report = migrate_json_to_sqlite(settings=settings, skip_invalid=True)
    assert report.ok is True
    assert report.scanned == 2
    assert report.imported == 1
    assert report.invalid == 1
    assert _snapshots(runs) == before


def test_conflict_aborts_and_preserves_both_sides(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runs = settings.runs_dir()
    state = _write_run(runs, n=1)
    dest = settings.run_db_path()
    first = migrate_json_to_sqlite(settings=settings)
    assert first.ok is True
    other = RunState.start("wf", {"n": 99}, run_id=state.run_id)
    other.status = "failed"
    store = SQLiteRunStore(dest)
    store.save(other)
    store.close()
    before_src = _snapshots(runs)
    before_dest = dest.read_bytes()
    report = migrate_json_to_sqlite(settings=settings, on_conflict="error")
    assert report.ok is False
    assert report.conflicts == 1
    assert report.imported == 0
    assert _snapshots(runs) == before_src
    assert dest.read_bytes() == before_dest
    loaded = SQLiteRunStore(dest).get(state.run_id)
    assert loaded.state.inputs["n"] == 99


def test_skip_identical_idempotent_rejects_nonidentical(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runs = settings.runs_dir()
    _write_run(runs, n=1)
    first = migrate_json_to_sqlite(settings=settings)
    assert first.ok is True
    before = _snapshots(runs)
    again = migrate_json_to_sqlite(settings=settings, on_conflict="skip-identical")
    assert again.ok is True
    assert again.imported == 0
    assert again.skipped == 1
    dest = settings.run_db_path()
    colliding = RunState.start("wf", {"n": 2})
    persist_run(colliding, runs)
    store = SQLiteRunStore(dest)
    store.save(RunState.start("wf", {"n": 3}, run_id=colliding.run_id))
    store.close()
    report = migrate_json_to_sqlite(settings=settings, on_conflict="skip-identical")
    assert report.ok is False
    assert report.conflicts >= 1
    assert _snapshots(runs) == {
        **before,
        f"{colliding.run_id}.json": (runs / f"{colliding.run_id}.json").read_bytes(),
    }


def test_mid_batch_failure_rolls_back_and_resumes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runs = settings.runs_dir()
    ids = [_write_run(runs, run_id=f"{i:032x}").run_id for i in range(3)]
    dest = settings.run_db_path()
    before = _snapshots(runs)
    failed = migrate_json_to_sqlite(settings=settings, fail_after=0)
    assert failed.ok is False
    store = SQLiteRunStore(dest)
    listed = store.list()
    store.close()
    assert listed == []
    resumed = migrate_json_to_sqlite(settings=settings, on_conflict="skip-identical")
    assert resumed.ok is True
    assert resumed.imported == 3
    store = SQLiteRunStore(dest)
    got = {item.state.run_id for item in store.list()}
    store.close()
    assert got == set(ids)
    assert _snapshots(runs) == before


def test_unicode_pending_failed_cancelled_lossless(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runs = settings.runs_dir()
    paused = RunState.start("gate", {"note": "café 🎵"})
    paused.status = "paused"
    paused.pending_node = "gate"
    paused.pending = {"type": "approval", "prompt": "ok?"}
    persist_run(paused, runs)
    failed = _write_run(runs, status="failed", reason="boom")
    cancelled = _write_run(runs, status="cancelled")
    before = _snapshots(runs)
    report = migrate_json_to_sqlite(settings=settings)
    assert report.ok is True
    assert report.imported == 3
    store = SQLiteRunStore(settings.run_db_path())
    p = store.get(paused.run_id)
    assert p.state.status == "paused"
    assert p.state.inputs["note"] == "café 🎵"
    assert p.state.pending_node == "gate"
    assert store.get(failed.run_id).state.status == "failed"
    assert store.get(cancelled.run_id).state.status == "cancelled"
    store.close()
    assert _snapshots(runs) == before


def test_cli_dry_run_and_apply_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(settings.home))
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    with pytest.raises(ApprovalRequired):
        run_workflow_file(EXAMPLES / "approval_gate.yaml", settings=settings, persist=True)
    dest = settings.run_db_path()
    help_result = runner.invoke(app, ["runs", "migrate", "--help"])
    assert help_result.exit_code == 0, help_result.stdout + help_result.stderr
    text = _plain(help_result.stdout)
    for flag in (
        "--from",
        "--to",
        "--source",
        "--database",
        "--on-conflict",
        "--skip-invalid",
        "--verify",
        "--dry-run",
        "--json",
    ):
        assert flag in text
    dry = runner.invoke(
        app,
        ["runs", "migrate", "--from", "json", "--to", "sqlite", "--dry-run", "--json"],
    )
    assert dry.exit_code == 0, dry.stdout + dry.stderr
    payload = json.loads(dry.stdout[dry.stdout.find("{") :])
    assert payload["ok"] is True
    assert payload["command"] == "runs migrate"
    assert payload["scanned"] >= 1
    assert payload["imported"] == 0
    assert not dest.exists()
    applied = runner.invoke(
        app,
        ["runs", "migrate", "--from", "json", "--to", "sqlite", "--json"],
    )
    assert applied.exit_code == 0, applied.stdout + applied.stderr
    body = json.loads(applied.stdout[applied.stdout.find("{") :])
    assert body["ok"] is True
    assert body["imported"] >= 1
    assert body["verified"] is True
    run_id = json.loads((settings.runs_dir() / os.listdir(settings.runs_dir())[0]).read_text())[
        "run_id"
    ]
    monkeypatch.setenv("READYAGENTS_RUN_STORE", "sqlite")
    clear_settings_cache()
    shown = runner.invoke(app, ["runs", "show", run_id, "--json"])
    assert shown.exit_code == 0, shown.stdout + shown.stderr
    shown_body = json.loads(shown.stdout[shown.stdout.find("{") :])
    assert shown_body.get("status") == "paused" or shown_body.get("ok") is True
    loaded = SQLiteRunStore(dest).get(run_id)
    assert loaded.state.status == "paused"


def test_cli_conflict_exit_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(settings.home))
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    _write_run(settings.runs_dir())
    runner.invoke(app, ["runs", "migrate", "--from", "json", "--to", "sqlite"])
    result = runner.invoke(app, ["runs", "migrate", "--from", "json", "--to", "sqlite", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout[result.stdout.find("{") :])
    assert payload["ok"] is False
    assert payload["command"] == "runs migrate"


def test_rejects_directory_database(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_run(settings.runs_dir())
    dest = tmp_path / "not-a-db"
    dest.mkdir()
    with pytest.raises(ConfigError, match="directory"):
        migrate_json_to_sqlite(settings=settings, database=dest)


def test_relative_database_under_home(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_run(settings.runs_dir())
    report = migrate_json_to_sqlite(settings=settings, database=Path("alt.sqlite3"))
    assert report.ok is True
    assert (settings.home_path() / "alt.sqlite3").is_file()


def test_rejects_symlink_outside_home(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_run(settings.runs_dir())
    settings.home_path().mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.sqlite3"
    outside.write_bytes(b"")
    link = settings.home_path() / "sneaky.sqlite3"
    link.symlink_to(outside)
    with pytest.raises(ConfigError, match="symlink"):
        migrate_json_to_sqlite(settings=settings, database=Path("sneaky.sqlite3"))
    assert outside.read_bytes() == b""
