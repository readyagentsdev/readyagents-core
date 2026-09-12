"""Per-environment isolation and secret-value refuse. Drive shipped APIs."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.env.release import deploy
from readyagents.env.run import run_in_environment, runs_dir_for
from readyagents.env.schema import EnvironmentSpec, load_env_file, refuse_secrets
from readyagents.env.store import EnvStore
from readyagents.errors import EnvRefused
from readyagents.workflow.schema import BudgetSpec

runner = CliRunner()


def _flow(tmp: Path, token: str, name: str = "echo.yaml") -> Path:
    path = tmp / name
    path.write_text(
        "name: echo\n"
        "nodes:\n"
        "  - id: t\n"
        "    type: transform\n"
        f"    template: '{token}'\n"
        "    output_key: out\n",
        encoding="utf-8",
    )
    return path


def _write_env(tmp: Path) -> Path:
    (tmp / "policy").mkdir(exist_ok=True)
    (tmp / "routing").mkdir(exist_ok=True)
    (tmp / "policy" / "dev.yaml").write_text("version: 1\ndefault: allow\n", encoding="utf-8")
    (tmp / "policy" / "prod.yaml").write_text("version: 1\ndefault: deny\n", encoding="utf-8")
    routing = "version: 1\npool: ['openai:gpt-4o-mini']\n"
    (tmp / "routing" / "dev.yaml").write_text(routing, encoding="utf-8")
    (tmp / "routing" / "prod.yaml").write_text(routing, encoding="utf-8")
    path = tmp / "readyagents.env.yaml"
    path.write_text(
        "version: 1\n"
        "environments:\n"
        "  dev:\n"
        "    policy: policy/dev.yaml\n"
        "    routing: routing/dev.yaml\n"
        "    budget: {max_cost_usd: 1.00}\n"
        "    secrets: dev\n"
        "    store: env-runs/dev\n"
        "  prod:\n"
        "    policy: policy/prod.yaml\n"
        "    routing: routing/prod.yaml\n"
        "    budget: {max_cost_usd: 50.00}\n"
        "    secrets: prod\n"
        "    store: env-runs/prod\n",
        encoding="utf-8",
    )
    return path


def test_named_envs_do_not_leak_policy_budget_routing_store(tmp_path: Path, tmp_settings) -> None:
    _write_env(tmp_path)
    loaded = load_env_file(tmp_path / "readyagents.env.yaml", settings=tmp_settings)
    assert loaded is not None
    dev = loaded.environments["dev"]
    prod = loaded.environments["prod"]
    assert dev.policy != prod.policy
    assert dev.routing != prod.routing
    assert dev.secrets != prod.secrets
    assert dev.store != prod.store
    assert dev.budget is not None and prod.budget is not None
    assert dev.budget.max_cost_usd != prod.budget.max_cost_usd
    store = EnvStore(tmp_settings)
    dev_dir = runs_dir_for("dev", dev, store)
    prod_dir = runs_dir_for("prod", prod, store)
    assert dev_dir != prod_dir
    assert tmp_settings.home_path() in dev_dir.parents
    assert tmp_settings.home_path() in prod_dir.parents
    flow = _flow(tmp_path, "hello")
    deploy(flow, "dev", spec=dev, settings=tmp_settings, actor="ops")
    deploy(flow, "prod", spec=prod, settings=tmp_settings, actor="ops")
    a = run_in_environment(flow, "dev", settings=tmp_settings, persist=True)
    b = run_in_environment(flow, "prod", settings=tmp_settings, persist=True)
    assert a.metadata["environment"] == "dev"
    assert b.metadata["environment"] == "prod"
    assert a.metadata["secret_scope"] == "dev"
    assert b.metadata["secret_scope"] == "prod"
    assert a.metadata["env_store"] != b.metadata["env_store"]
    assert Path(a.metadata["env_store"]) == dev_dir
    assert Path(b.metadata["env_store"]) == prod_dir
    assert (dev_dir / f"{a.run_id}.json").is_file()
    assert (prod_dir / f"{b.run_id}.json").is_file()
    assert not (dev_dir / f"{b.run_id}.json").is_file()
    assert not (prod_dir / f"{a.run_id}.json").is_file()
    default_runs = tmp_settings.runs_dir()
    assert not (default_runs / f"{a.run_id}.json").is_file()
    assert not (default_runs / f"{b.run_id}.json").is_file()


def test_secret_value_in_config_is_typed_refuse() -> None:
    with pytest.raises(EnvRefused) as extra:
        refuse_secrets({"environments": {"prod": {"token": "sk-abc12345xxxx"}}})
    assert extra.value.reason == "secret"
    with pytest.raises(EnvRefused) as ghp:
        refuse_secrets({"environments": {"prod": {"notes": "ghp_abcdefgh"}}})
    assert ghp.value.reason == "secret"
    with pytest.raises(EnvRefused) as pem:
        refuse_secrets({"environments": {"prod": {"key": "-----BEGIN PRIVATE KEY-----\nMIIB\n"}}})
    assert pem.value.reason == "secret"
    refuse_secrets({"environments": {"prod": {"secrets": "prod", "policy": "policy/prod.yaml"}}})


def test_load_env_file_refuses_secret_and_missing_is_none(tmp_path: Path, tmp_settings) -> None:
    assert load_env_file(tmp_path / "missing.yaml", settings=tmp_settings) is None
    bad = tmp_path / "readyagents.env.yaml"
    bad.write_text(
        "version: 1\nenvironments:\n  prod:\n    secrets: sk-abcdefghijklmnop\n",
        encoding="utf-8",
    )
    with pytest.raises(EnvRefused) as extra:
        load_env_file(bad, settings=tmp_settings)
    assert extra.value.reason == "secret"


def test_store_path_escape_refused(tmp_settings) -> None:
    store = EnvStore(tmp_settings)
    spec = EnvironmentSpec(store="/tmp/escape")
    with pytest.raises(EnvRefused) as extra:
        runs_dir_for("prod", spec, store)
    assert extra.value.reason == "store"
    spec2 = EnvironmentSpec(store="../escape")
    with pytest.raises(EnvRefused):
        runs_dir_for("prod", spec2, store)


def test_budget_is_per_env_spec() -> None:
    cheap = EnvironmentSpec(budget=BudgetSpec(max_cost_usd=1.0))
    dear = EnvironmentSpec(budget=BudgetSpec(max_cost_usd=50.0))
    assert cheap.budget is not None and dear.budget is not None
    assert cheap.budget.max_cost_usd != dear.budget.max_cost_usd


def test_no_env_flag_does_not_read_env_file(tmp_path: Path, tmp_settings, monkeypatch) -> None:
    _write_env(tmp_path)
    _flow(tmp_path, "hello", "calc.yaml")
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    monkeypatch.setenv("READYAGENTS_WORKSPACE", str(tmp_path))
    clear_settings_cache()
    first = runner.invoke(app, ["run", str(tmp_path / "calc.yaml"), "--json", "--no-persist"])
    assert first.exit_code == 0, first.stdout + first.stderr
    payload = yaml.safe_load(first.stdout[first.stdout.find("{") :])
    assert payload["ok"] is True
    assert "environment" not in (payload.get("metadata") or {})
