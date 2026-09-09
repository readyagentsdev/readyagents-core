"""Policy load, precedence, fail-closed. Drive shipped load_policy / CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.errors import PolicyError
from readyagents.firewall.policy_file import load_policy, load_resolved, resolve_policy

_runner = CliRunner()


def test_load_valid_policy(tmp_path: Path) -> None:
    path = tmp_path / "readyagents.policy.yaml"
    path.write_text("version: 1\ndefault: deny\n", encoding="utf-8")
    policy = load_policy(path)
    assert policy.default == "deny"


def test_unknown_key_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "p.yaml"
    path.write_text("version: 1\ndefault: allow\nnope: 1\n", encoding="utf-8")
    with pytest.raises(PolicyError):
        load_policy(path)


def test_malformed_yaml_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "p.yaml"
    path.write_text(":\n  - [\n", encoding="utf-8")
    with pytest.raises(PolicyError):
        load_policy(path)


def test_missing_explicit_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="not found"):
        resolve_policy(explicit=tmp_path / "missing.yaml")


def test_env_missing_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="READYAGENTS_POLICY"):
        resolve_policy(env={"READYAGENTS_POLICY": str(tmp_path / "nope.yaml")})


def test_beside_workflow_optional(tmp_path: Path) -> None:
    assert resolve_policy(workflow_dir=tmp_path) is None
    path = tmp_path / "readyagents.policy.yaml"
    path.write_text("version: 1\n", encoding="utf-8")
    assert resolve_policy(workflow_dir=tmp_path) == path


def test_explicit_wins_over_env(tmp_path: Path) -> None:
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    a.write_text("version: 1\ndefault: allow\n", encoding="utf-8")
    b.write_text("version: 1\ndefault: deny\n", encoding="utf-8")
    loaded = load_resolved(explicit=a, env={"READYAGENTS_POLICY": str(b)})
    assert loaded is not None
    assert loaded.default == "allow"


def test_policy_check_cli(tmp_path: Path) -> None:
    path = tmp_path / "p.yaml"
    path.write_text("version: 1\ndefault: allow\n", encoding="utf-8")
    ok = _runner.invoke(app, ["policy", "check", str(path), "--json"])
    assert ok.exit_code == 0, ok.stdout
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\nunknown: true\n", encoding="utf-8")
    fail = _runner.invoke(app, ["policy", "check", str(bad)])
    assert fail.exit_code == 1
