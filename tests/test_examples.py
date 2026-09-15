"""Shipped examples stay in the wheel and reachable via `new` (H-06)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.errors import ConfigError
from readyagents.examples import list_examples, materialize_example, resolve_example

runner = CliRunner()

ROOT = Path(__file__).resolve().parents[1]
SKIP_NAMES = {"__pycache__", ".DS_Store"}


def _repo_example_files() -> set[str]:
    found: set[str] = set()
    for path in (ROOT / "examples").rglob("*"):
        if path.is_dir() or path.name in SKIP_NAMES or path.suffix in {".pyc", ".pyo"}:
            continue
        found.add(path.relative_to(ROOT).as_posix())
    return found


def test_force_include_matches_repo_examples() -> None:
    """Every repo example must be force-included into the wheel, and no stale entries."""
    with open(ROOT / "pyproject.toml", "rb") as fh:
        pyproject = tomllib.load(fh)
    force_include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    manifest = {k for k in force_include if k.startswith("examples/")}
    targets = {v for k, v in force_include.items() if k.startswith("examples/")}
    repo_files = _repo_example_files()
    assert manifest == repo_files, (
        f"missing from wheel manifest: {sorted(repo_files - manifest)}; "
        f"stale manifest entries: {sorted(manifest - repo_files)}"
    )
    assert targets == {f"readyagents/{f}" for f in repo_files}


def test_list_examples_matches_repo() -> None:
    assert [f"examples/{p}" for p in list_examples()] == sorted(_repo_example_files())


def test_resolve_prefers_yaml_for_shared_stem() -> None:
    relpath, content = resolve_example("calc_pipeline")
    assert relpath == "calc_pipeline.yaml"
    assert content == (ROOT / "examples" / "calc_pipeline.yaml").read_bytes()


def test_resolve_accepts_full_relative_path() -> None:
    relpath, _ = resolve_example("bench/suite.yaml")
    assert relpath == "bench/suite.yaml"


def test_resolve_unknown_example_errors() -> None:
    with pytest.raises(ConfigError, match="Unknown example"):
        resolve_example("does-not-exist")


def test_materialize_writes_workflow_and_schema(tmp_path: Path) -> None:
    written = materialize_example("calc_pipeline", tmp_path / "f")
    names = sorted(p.name for p in written)
    assert names == ["workflow.schema.json", "workflow.yaml"]
    assert (tmp_path / "f" / "workflow.yaml").read_bytes() == (
        ROOT / "examples" / "calc_pipeline.yaml"
    ).read_bytes()


def test_materialize_refuses_overwrite(tmp_path: Path) -> None:
    materialize_example("calc_pipeline", tmp_path / "f")
    with pytest.raises(ConfigError, match="Refusing to overwrite"):
        materialize_example("calc_pipeline", tmp_path / "f")


def test_new_list_examples(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["new", "--list-examples"])
    assert result.exit_code == 0, result.output
    assert "calc_pipeline.yaml" in result.output
    assert "bench/suite.yaml" in result.output


def test_new_from_example_end_to_end(tmp_path: Path, monkeypatch) -> None:
    """Acceptance flow: materialize calc_pipeline and run it keyless."""
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    created = runner.invoke(app, ["new", "f", "--from-example", "calc_pipeline"])
    assert created.exit_code == 0, created.output
    assert (tmp_path / "f" / "workflow.yaml").exists()
    run = runner.invoke(app, ["run", "f/workflow.yaml"])
    assert run.exit_code == 0, run.output
    assert "succeeded" in run.output


def test_new_from_example_rejects_template(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["new", "f", "--from-example", "calc_pipeline", "--template", "basic"]
    )
    assert result.exit_code != 0
    assert "Cannot combine" in result.output


def test_new_from_example_unknown_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["new", "f", "--from-example", "does-not-exist"])
    assert result.exit_code != 0
    assert "Unknown example" in result.output
