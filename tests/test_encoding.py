from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.scaffold import create_project

runner = CliRunner()


def test_scaffold_unix_newlines(tmp_path: Path) -> None:
    dest = tmp_path / "proj"
    create_project(dest, name="proj", template="pipeline")
    raw = (dest / "workflow.yaml").read_bytes()
    assert b"\r\n" not in raw
    assert b"\n" in raw


def test_cli_unicode_json() -> None:
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code in {0, 1}
    result.stdout.encode("utf-8")
    assert "\x1b" not in result.stdout
