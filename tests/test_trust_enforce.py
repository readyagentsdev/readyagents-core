"""Enforcement, lockfile, SBOM, verify-before-import, TOCTOU. Drive shipped loader/runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.errors import ConfigError, TrustError
from readyagents.packs.loader import load_pack_file
from readyagents.trust.keyring import add_key
from readyagents.trust.lock import build_lockfile, write_lockfile
from readyagents.trust.sbom import build_sbom, dumps_sbom
from readyagents.trust.sign import sign_artifact
from readyagents.workflow.runner import run_workflow_file

pytest.importorskip("cryptography")
runner = CliRunner()

_PACK = """
from readyagents.packs import BasePack
from readyagents.tools import FunctionTool

class P(BasePack):
    name = "tpack"
    version = "0.0.1"
    def register_tools(self):
        return [FunctionTool(name="connector_ping", description="p", handler=lambda message="": {"ok": True, "message": message})]
    def register_nodes(self):
        return {}
    def register_workflows(self):
        return []

def get_pack():
    return P()
"""

_BOMB = "raise RuntimeError('imported')\n"


def _ed25519_pem(tmp: Path, name: str = "key") -> tuple[Path, Path]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    priv = tmp / f"{name}.pem"
    pub = tmp / f"{name}.pub.pem"
    priv.write_bytes(
        private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    pub.write_bytes(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return priv, pub


def _flow(tmp: Path, name: str = "flow.yaml") -> Path:
    path = tmp / name
    path.write_text(
        "name: t\nstart: n\nnodes:\n  - id: n\n    type: transform\n    template: 'hello'\n    output_key: out\n",
        encoding="utf-8",
    )
    return path


def test_require_signed_refuses_unsigned(tmp_settings) -> None:
    flow = _flow(tmp_settings.workspace_path())
    with pytest.raises(TrustError, match="unsigned artifact") as caught:
        run_workflow_file(flow, settings=tmp_settings, persist=False, require_signed=True)
    assert caught.value.reason == "unsigned"
    assert caught.value.artifact
    assert "flow.yaml" in str(caught.value)


def test_require_signed_signed_workflow_runs(tmp_settings) -> None:
    root = tmp_settings.workspace_path()
    flow = _flow(root)
    priv, pub = _ed25519_pem(root)
    add_key(pub, name="ops", home=tmp_settings.home_path())
    sign_artifact(flow, key=priv)
    state = run_workflow_file(flow, settings=tmp_settings, persist=False, require_signed=True)
    assert state.status == "succeeded"
    supply = state.metadata["supply_chain"]
    assert supply["require_signed"] is True
    assert supply["artifacts"][0]["signature"] == "verified"
    assert supply["digest_version"] == 1


def test_verify_before_import_bomb_pack(tmp_settings) -> None:
    root = tmp_settings.workspace_path()
    flow = _flow(root)
    bomb = root / "bomb.py"
    bomb.write_text(_BOMB, encoding="utf-8")
    priv, pub = _ed25519_pem(root)
    add_key(pub, name="ops", home=tmp_settings.home_path())
    sign_artifact(flow, key=priv)
    with pytest.raises(TrustError, match="unsigned artifact") as caught:
        run_workflow_file(
            flow,
            settings=tmp_settings,
            persist=False,
            require_signed=True,
            pack_specs=[str(bomb)],
        )
    assert "imported" not in str(caught.value)
    assert caught.value.reason == "unsigned"
    assert "bomb.py" in str(caught.value)


def test_load_pack_file_never_execs_unsigned_bomb(tmp_settings) -> None:
    root = tmp_settings.workspace_path()
    bomb = root / "bomb.py"
    bomb.write_text(_BOMB, encoding="utf-8")
    with pytest.raises(TrustError, match="unsigned"):
        load_pack_file(bomb, root=root, require_signed=True)
    # Direct exec still runs the module — proving import is execute.
    with pytest.raises(ConfigError, match="imported"):
        load_pack_file(bomb, root=root, require_signed=False)


def test_toctou_executes_digested_buffer(tmp_settings) -> None:
    root = tmp_settings.workspace_path()
    pack = root / "p.py"
    pack.write_text(_PACK, encoding="utf-8")
    data = pack.read_bytes()
    pack.write_text(_BOMB, encoding="utf-8")
    loaded = load_pack_file(pack, root=root, source=data, require_signed=False)
    assert loaded.name == "tpack"


def test_toctou_require_signed_uses_buffer_not_swapped_path(tmp_settings) -> None:
    root = tmp_settings.workspace_path()
    pack = root / "p.py"
    pack.write_text(_PACK, encoding="utf-8")
    priv, pub = _ed25519_pem(root)
    add_key(pub, name="ops", home=tmp_settings.home_path())
    sign_artifact(pack, key=priv)
    data = pack.read_bytes()
    pack.write_text(_BOMB, encoding="utf-8")
    loaded = load_pack_file(
        pack,
        root=root,
        source=data,
        require_signed=True,
        keyring=__import__("readyagents.trust.keyring", fromlist=["load_keyring"]).load_keyring(
            home=tmp_settings.home_path()
        ),
    )
    assert loaded.name == "tpack"


def test_lock_frozen_mismatch(tmp_settings) -> None:
    root = tmp_settings.workspace_path()
    flow = _flow(root)
    lock = build_lockfile(flow)
    write_lockfile(lock, root / "readyagents.lock")
    flow.write_text(
        "name: t\nstart: n\nnodes:\n  - id: n\n    type: transform\n    template: 'changed'\n    output_key: out\n",
        encoding="utf-8",
    )
    with pytest.raises(TrustError, match="lockfile mismatch") as caught:
        run_workflow_file(flow, settings=tmp_settings, persist=False, frozen=True)
    assert caught.value.reason == "digest_mismatch"


def test_lock_frozen_match_runs(tmp_settings) -> None:
    root = tmp_settings.workspace_path()
    flow = _flow(root)
    lock = build_lockfile(flow)
    write_lockfile(lock, root / "readyagents.lock")
    state = run_workflow_file(flow, settings=tmp_settings, persist=False, frozen=True)
    assert state.status == "succeeded"


def test_missing_lockfile_frozen_fails(tmp_settings) -> None:
    flow = _flow(tmp_settings.workspace_path())
    with pytest.raises(TrustError, match="missing lockfile") as caught:
        run_workflow_file(flow, settings=tmp_settings, persist=False, frozen=True)
    assert caught.value.reason == "missing_lockfile"


def test_policy_require_signed(tmp_settings) -> None:
    root = tmp_settings.workspace_path()
    flow = _flow(root)
    policy = root / "readyagents.policy.yaml"
    policy.write_text("version: 1\nrequire_signed: true\n", encoding="utf-8")
    with pytest.raises(TrustError, match="unsigned"):
        run_workflow_file(flow, settings=tmp_settings, persist=False, policy=policy)


def test_sbom_deterministic_and_secret_free(tmp_settings) -> None:
    root = tmp_settings.workspace_path()
    flow = root / "flow.yaml"
    flow.write_text(
        "name: inv\nstart: n\ndefault_model: openai:gpt-4o-mini\n"
        "mcp_servers:\n  files:\n    command: echo\n    args: ['hi']\n"
        "    env:\n      TOKEN: super-secret-value\n"
        "nodes:\n  - id: n\n    type: tool\n    tool: calc\n    arguments:\n      expression: '1+1'\n",
        encoding="utf-8",
    )
    first = dumps_sbom(build_sbom(flow, workspace=root))
    second = dumps_sbom(build_sbom(flow, workspace=root))
    assert first == second
    payload = json.loads(first)
    assert payload["bomFormat"] == "CycloneDX"
    assert "super-secret-value" not in first
    assert "TOKEN" not in first
    names = {row["name"] for row in payload["components"]}
    assert "tool:calc" in names
    assert "model:openai:gpt-4o-mini" in names
    assert "mcp:files" in names


def test_cli_lock_and_frozen_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / ".readyagents"
    monkeypatch.setenv("READYAGENTS_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    flow = _flow(tmp_path)
    locked = runner.invoke(app, ["lock", str(flow), "--json"])
    assert locked.exit_code == 0, locked.stdout + locked.stderr
    first = json.loads(locked.stdout)
    assert first["ok"] is True
    assert first["digest_version"] == 1
    ok = runner.invoke(app, ["run", str(flow), "--frozen", "--no-persist"])
    assert ok.exit_code == 0, ok.stdout + ok.stderr
    flow.write_text(flow.read_text(encoding="utf-8").replace("hello", "bye"), encoding="utf-8")
    bad = runner.invoke(app, ["run", str(flow), "--frozen", "--no-persist"])
    assert bad.exit_code == 1
    assert "lockfile mismatch" in bad.stdout + bad.stderr
    clear_settings_cache()


def test_cli_sbom_json_stable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    flow = _flow(tmp_path)
    a = runner.invoke(app, ["sbom", str(flow), "--json"])
    b = runner.invoke(app, ["sbom", str(flow), "--json"])
    assert a.exit_code == 0 and b.exit_code == 0, a.stdout + b.stdout
    left = json.loads(a.stdout)
    right = json.loads(b.stdout)
    assert left["ok"] is True and right["ok"] is True
    assert left["bomFormat"] == right["bomFormat"] == "CycloneDX"
    assert left["components"] == right["components"]
    clear_settings_cache()


def test_cli_require_signed_signed_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / ".readyagents"
    monkeypatch.setenv("READYAGENTS_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    flow = tmp_path / "connector.yaml"
    flow.write_text(
        yaml.safe_dump(
            {
                "name": "c",
                "start": "p",
                "nodes": [
                    {
                        "id": "p",
                        "type": "tool",
                        "tool": "connector_ping",
                        "arguments": {"message": "hello"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    pack = tmp_path / "pack.py"
    pack.write_text(_PACK, encoding="utf-8")
    priv, pub = _ed25519_pem(tmp_path)
    add = runner.invoke(app, ["trust", "add", str(pub), "--name", "ops"])
    assert add.exit_code == 0, add.stdout + add.stderr
    assert runner.invoke(app, ["sign", str(flow), "--key", str(priv)]).exit_code == 0
    assert runner.invoke(app, ["sign", str(pack), "--key", str(priv)]).exit_code == 0
    result = runner.invoke(
        app, ["run", str(flow), "--pack", str(pack), "--require-signed", "--no-persist"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "succeeded" in result.stdout
    clear_settings_cache()


def test_unsigned_default_path_still_runs(tmp_settings) -> None:
    flow = _flow(tmp_settings.workspace_path())
    state = run_workflow_file(flow, settings=tmp_settings, persist=False)
    assert state.status == "succeeded"
    assert state.metadata["supply_chain"]["require_signed"] is False
    assert state.metadata["supply_chain"]["artifacts"][0]["signature"] == "unsigned"
