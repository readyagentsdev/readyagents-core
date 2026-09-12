"""Pinned signed releases: --env ignores the working copy; tamper refuses."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.env.release import deploy, pin_release, verify_release
from readyagents.env.run import run_in_environment
from readyagents.env.schema import EnvironmentSpec, load_env_file
from readyagents.env.store import EnvStore
from readyagents.errors import EnvRefused
from readyagents.trust.digest import KIND_RELEASE, inspect_workflow
from readyagents.trust.keyring import add_key

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


def _env_file(tmp: Path) -> Path:
    (tmp / "policy").mkdir(exist_ok=True)
    (tmp / "policy" / "prod.yaml").write_text("version: 1\ndefault: allow\n", encoding="utf-8")
    path = tmp / "readyagents.env.yaml"
    path.write_text(
        "version: 1\n"
        "environments:\n"
        "  prod:\n"
        "    policy: policy/prod.yaml\n"
        "    secrets: prod\n"
        "  staging:\n"
        "    policy: policy/prod.yaml\n",
        encoding="utf-8",
    )
    return path


def _ed25519_pem(tmp: Path, name: str = "key") -> tuple[Path, Path]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    priv_path = tmp / f"{name}.pem"
    pub_path = tmp / f"{name}.pub.pem"
    priv_path.write_bytes(
        private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    pub_path.write_bytes(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return priv_path, pub_path


def test_pin_covers_workflow_includes_prompts_policy_lockfile(tmp_path: Path, tmp_settings) -> None:
    child = tmp_path / "child.yaml"
    child.write_text(
        "name: child\nnodes:\n  - id: c\n    type: transform\n    template: 'n'\n    output_key: n\n",
        encoding="utf-8",
    )
    flow = tmp_path / "parent.yaml"
    flow.write_text(
        "name: parent\n"
        "nodes:\n"
        "  - id: child\n"
        "    type: include\n"
        "    path: child.yaml\n"
        "    output_key: nested\n"
        "    next: wrap\n"
        "  - id: wrap\n"
        "    type: transform\n"
        "    template: 'ok {{nested.n}}'\n"
        "    output_key: out\n",
        encoding="utf-8",
    )
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "a.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "readyagents.lock").write_text('{"version": 1}\n', encoding="utf-8")
    (tmp_path / "policy.yaml").write_text("version: 1\ndefault: allow\n", encoding="utf-8")
    spec = EnvironmentSpec(policy="policy.yaml")
    pointer = pin_release(flow, env="prod", spec=spec, settings=tmp_settings)
    pins = pointer["pins"]
    assert pins["workflow"] == inspect_workflow(flow).digest
    assert "child.yaml" in pins["includes"]
    assert pins["policy"]
    assert pins["prompts"]
    assert pins["lockfile"]
    folder = Path(pointer["path"])
    assert (folder / "workflow.yaml").is_file()
    assert (folder / "child.yaml").is_file()
    assert (folder / "prompts" / "a.txt").is_file()
    assert (folder / "readyagents.lock").is_file()
    assert (folder / "policy.yaml").is_file()


def test_working_copy_edit_does_not_change_env_run(tmp_path: Path, tmp_settings) -> None:
    _env_file(tmp_path)
    loaded = load_env_file(settings=tmp_settings)
    assert loaded is not None
    spec = loaded.environments["prod"]
    flow = _flow(tmp_path, "PINNED")
    deploy(flow, "prod", spec=spec, settings=tmp_settings)
    flow.write_text(
        "name: echo\nnodes:\n  - id: t\n    type: transform\n    template: 'EDITED'\n"
        "    output_key: out\n",
        encoding="utf-8",
    )
    state = run_in_environment(flow, "prod", settings=tmp_settings, persist=True)
    assert state.output_keys["out"] == "PINNED"
    assert state.metadata["environment"] == "prod"
    assert state.metadata["release"]
    assert state.metadata["release_channel"] == "current"


def test_undeployed_env_refuses(tmp_path: Path, tmp_settings) -> None:
    _env_file(tmp_path)
    flow = _flow(tmp_path, "x")
    with pytest.raises(EnvRefused) as extra:
        run_in_environment(flow, "prod", settings=tmp_settings)
    assert extra.value.reason == "undeployed"


def test_signed_manifest_tamper_refuses(tmp_path: Path, tmp_settings) -> None:
    _env_file(tmp_path)
    loaded = load_env_file(settings=tmp_settings)
    assert loaded is not None
    spec = loaded.environments["prod"]
    flow = _flow(tmp_path, "sig")
    priv, pub = _ed25519_pem(tmp_path)
    add_key(pub, name="ops", home=tmp_settings.home_path())
    pointer = deploy(flow, "prod", spec=spec, settings=tmp_settings, sign_key=priv)
    verify_release(pointer, settings=tmp_settings, require_signed=True)
    manifest = Path(pointer["path"]) / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["digest"] = "sha256:" + ("0" * 64)
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(EnvRefused) as extra:
        verify_release(pointer, settings=tmp_settings)
    assert extra.value.reason == "tamper"


def test_snapshot_file_tamper_refuses(tmp_path: Path, tmp_settings) -> None:
    flow = _flow(tmp_path, "orig")
    spec = EnvironmentSpec()
    pointer = pin_release(flow, env="prod", spec=spec, settings=tmp_settings)
    verify_release(pointer, settings=tmp_settings)
    wf = Path(pointer["path"]) / "workflow.yaml"
    wf.write_text(wf.read_text(encoding="utf-8").replace("orig", "evil"), encoding="utf-8")
    with pytest.raises(EnvRefused) as extra:
        verify_release(pointer, settings=tmp_settings)
    assert extra.value.reason == "tamper"


def test_cli_run_env_uses_pin(tmp_path: Path, tmp_settings, monkeypatch) -> None:
    _env_file(tmp_path)
    flow = _flow(tmp_path, "CLI-PIN")
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    monkeypatch.setenv("READYAGENTS_WORKSPACE", str(tmp_path))
    clear_settings_cache()
    deployed = runner.invoke(app, ["env", "deploy", str(flow), "--env", "prod", "--json"])
    assert deployed.exit_code == 0, deployed.stdout + deployed.stderr
    flow.write_text(
        "name: echo\nnodes:\n  - id: t\n    type: transform\n    template: 'CLI-EDIT'\n"
        "    output_key: out\n",
        encoding="utf-8",
    )
    ran = runner.invoke(app, ["run", str(flow), "--env", "prod", "--json", "--no-persist"])
    assert ran.exit_code == 0, ran.stdout + ran.stderr
    start = ran.stdout.find("{")
    payload = json.loads(ran.stdout[start:])
    assert payload["ok"] is True
    assert payload["outputs"]["out"] == "CLI-PIN"
    assert payload["metadata"]["environment"] == "prod"
    assert payload["metadata"]["release"]
    store = EnvStore(tmp_settings)
    assert store.current("prod") is not None


def test_kind_release_sign_round_trip(tmp_path: Path, tmp_settings) -> None:
    flow = _flow(tmp_path, "k")
    priv, pub = _ed25519_pem(tmp_path)
    add_key(pub, name="ops", home=tmp_settings.home_path())
    pointer = pin_release(flow, env="prod", settings=tmp_settings, sign_key=priv)
    sig = Path(pointer["path"]) / "manifest.json.sig"
    assert sig.is_file()
    assert KIND_RELEASE == "release"
    verify_release(pointer, settings=tmp_settings, require_signed=True)
