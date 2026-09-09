from __future__ import annotations

import json
import re
import socket
import tarfile
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]


def _plain(text: str) -> str:
    """Strip ANSI and whitespace so Rich wrapping cannot hide tokens."""
    return re.sub(r"\s+", "", re.sub(r"\x1b\[[0-9;]*m", "", text))


def _json_from_cli(text: str) -> dict:
    for i, ch in enumerate(text):
        if ch in "{[":
            data = json.loads(text[i:])
            assert isinstance(data, dict)
            return data
    raise AssertionError(f"no JSON in:\n{text}")


def test_validate_json_adds_problems_keeps_keys(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "name: bad\nnodes:\n  - id: a\n    type: transform\n    template: x\n    next: missing\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["validate", str(path), "--json"])
    assert result.exit_code == 1
    assert "\x1b" not in result.stdout
    data = _json_from_cli(result.stdout)
    assert data["ok"] is False
    assert data["command"] == "validate"
    assert data["error"] == "WorkflowError"
    assert "Invalid workflow" in data["message"]
    assert "problems" in data
    assert isinstance(data["problems"], list)
    assert data["problems"]
    row = data["problems"][0]
    assert "loc" in row and "message" in row and "file" in row
    assert "line" in row and "column" in row


def test_validate_human_shows_caret(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "name: bad\n"
        "nodes:\n"
        "  - id: a\n"
        "    type: transform\n"
        "    template: x\n"
        '    retry: {max_attempts: "three"}\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code == 1
    text = result.stdout + result.stderr
    assert "WorkflowError" in text
    assert "Invalid workflow" in text
    assert "^" in text
    assert "max_attempts" in text


def test_run_invalid_workflow_still_exit_1(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "name: bad\nnodes:\n  - id: a\n    type: transform\n    template: x\n    next: missing\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["run", str(path), "--no-persist"])
    assert result.exit_code == 1
    assert "Invalid workflow" in result.stdout + result.stderr


def test_ansi_and_secret_stripped_from_excerpt(tmp_path: Path) -> None:
    path = tmp_path / "inj.yaml"
    # invalid integer on a line that also contains ANSI and a vendor key
    path.write_text(
        "name: inj\n"
        "nodes:\n"
        "  - id: a\n"
        "    type: transform\n"
        "    template: x\n"
        '    retry: {max_attempts: "\x1b[31msk-abcdefghijklmnop"}\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["validate", str(path)])
    text = result.stdout + result.stderr
    assert result.exit_code == 1
    assert "\x1b[" not in text
    assert "sk-abcdefghijklmnop" not in text


def test_schema_and_validate_do_not_use_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a, **_k):  # noqa: ANN002
        raise AssertionError("network")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    schema = runner.invoke(app, ["schema"])
    assert schema.exit_code == 0, schema.stdout + schema.stderr
    wf = tmp_path / "ok.yaml"
    wf.write_text(
        "name: ok\nnodes:\n  - id: t\n    type: transform\n    template: hi\n    output_key: v\n",
        encoding="utf-8",
    )
    valid = runner.invoke(app, ["validate", str(wf)])
    assert valid.exit_code == 0, valid.stdout + valid.stderr
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: bad\nnodes:\n  - id: a\n    type: agent\n", encoding="utf-8")
    failed = runner.invoke(app, ["validate", str(bad)])
    assert failed.exit_code == 1


def test_pyproject_ships_schema() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "schemas/workflow-v1.json" in text
    assert '"/schemas"' in text or "/schemas" in text
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "schema-check" in makefile
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "schema --check" in ci


def test_wheel_and_sdist_contain_schema(tmp_path: Path) -> None:
    import subprocess
    import sys

    dist = tmp_path / "dist"
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(dist)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    wheels = list(dist.glob("*.whl"))
    sdists = list(dist.glob("*.tar.gz"))
    assert wheels and sdists
    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
        assert any(name.endswith("schemas/workflow-v1.json") for name in names), names
    with tarfile.open(sdists[0], "r:gz") as tf:
        names = tf.getnames()
        assert any(name.endswith("schemas/workflow-v1.json") for name in names), names


def test_help_lists_schema() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "schema" in _plain(result.stdout)
    help_schema = runner.invoke(app, ["schema", "--help"])
    assert help_schema.exit_code == 0, help_schema.stdout + help_schema.stderr
    text = _plain(help_schema.stdout)
    assert "--output" in text
    assert "--check" in text
    assert "--json" in text


def test_success_path_load_does_not_require_compose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import readyagents.workflow.source_map as sm

    def boom(*_a, **_k):  # noqa: ANN002
        raise AssertionError("compose on success path")

    monkeypatch.setattr(sm.yaml, "compose", boom)
    path = tmp_path / "ok.yaml"
    path.write_text(
        "name: ok\nnodes:\n  - id: t\n    type: transform\n    template: hi\n    output_key: v\n",
        encoding="utf-8",
    )
    from readyagents.workflow.runner import load_workflow

    spec = load_workflow(path)
    assert spec.name == "ok"
    clear_settings_cache()
