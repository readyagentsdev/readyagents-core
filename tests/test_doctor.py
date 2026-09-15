from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.doctor import format_doctor, run_doctor

runner = CliRunner()


def test_doctor_json_envelope_and_socket_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_k):  # noqa: ANN002
        raise AssertionError("network")

    monkeypatch.setattr(socket, "create_connection", boom)
    report = run_doctor()
    assert report["command"] == "doctor"
    assert "ok" in report
    assert "platform" in report
    assert "python" in report
    assert "readyagents" in report
    assert "extras" in report
    assert "home" in report
    assert "filesystem" in report
    assert "loopback" in report
    assert "run_store" in report
    assert "findings" in report
    assert "permissions_enforceable" in report["home"]
    assert "case_sensitive" in report["filesystem"]
    assert "sqlite_wal" in report["run_store"]


def test_doctor_cli_json() -> None:
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code in {0, 1}
    assert "\x1b" not in result.stdout
    data = json.loads(result.stdout[result.stdout.find("{") :])
    assert data["command"] == "doctor"
    assert "platform" in data


def test_help_lists_doctor() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "doctor" in result.stdout


def test_doctor_names_runs_dir_and_default_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("READYAGENTS_HOME", raising=False)
    report = run_doctor()
    assert report["home"]["runs_dir"] == str((tmp_path / ".readyagents").resolve() / "runs")
    assert report["home"]["source"] == "default"
    assert "Runs " in format_doctor(report)
    assert "source=default" in format_doctor(report)
    clear_settings_cache()


def test_doctor_source_prefers_env_over_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("READYAGENTS_HOME=.from-file\n", encoding="utf-8")
    assert run_doctor()["home"]["source"] == "config file"
    clear_settings_cache()
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".from-env"))
    report = run_doctor()
    assert report["home"]["source"] == "READYAGENTS_HOME"
    assert report["home"]["runs_dir"] == str((tmp_path / ".from-env").resolve() / "runs")
    clear_settings_cache()
