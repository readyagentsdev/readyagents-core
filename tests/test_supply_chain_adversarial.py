"""Adversarial supply-chain suite (TASK-03). Drive shipped trust APIs only; fail closed."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.errors import ConfigError, TrustError
from readyagents.firewall.policy_file import Policy
from readyagents.packs.loader import load_pack_file
from readyagents.tools import FunctionTool
from readyagents.trust.digest import digest_mcp_surface, digest_pack_bytes, digest_workflow
from readyagents.trust.keyring import Keyring, add_key, load_keyring, save_keyring
from readyagents.trust.lock import build_lockfile, load_lockfile, write_lockfile
from readyagents.trust.sign import sign_artifact, signature_path, verify_artifact
from readyagents.workflow.runner import run_workflow_file
from readyagents.workflow.state import RunState

pytest.importorskip("cryptography")
runner = CliRunner()

_PACK = """
from readyagents.packs import BasePack
from readyagents.tools import FunctionTool

class P(BasePack):
    name = "tpack"
    version = "0.0.1"
    def register_tools(self):
        return [FunctionTool(
            name="connector_ping",
            description="p",
            handler=lambda message="": {"ok": True, "message": message},
        )]
    def register_nodes(self):
        return {}
    def register_workflows(self):
        return []

def get_pack():
    return P()
"""

_BOMB = 'raise RuntimeError("imported")\n'
_SWAP = 'raise RuntimeError("swapped")\n'


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
        "name: t\nstart: n\nnodes:\n"
        "  - id: n\n    type: transform\n    template: 'hello'\n    output_key: out\n",
        encoding="utf-8",
    )
    return path


def _assert_trust(exc: TrustError, *, reason: str, artifact_hint: str | None = None) -> None:
    assert exc.reason == reason, (exc.reason, str(exc))
    assert exc.artifact, str(exc)
    if artifact_hint is not None:
        assert artifact_hint in str(exc.artifact) or artifact_hint in str(exc)


# --- 1. Tamper artifact after sign ---


def test_tamper_artifact_after_sign_fails(tmp_path: Path) -> None:
    priv, pub = _ed25519_pem(tmp_path)
    flow = _flow(tmp_path)
    add_key(pub, name="ops", home=tmp_path / "home")
    sign_artifact(flow, key=priv)
    flow.write_text(flow.read_text(encoding="utf-8").replace("hello", "evil"), encoding="utf-8")
    with pytest.raises(TrustError) as caught:
        verify_artifact(flow, keyring=load_keyring(home=tmp_path / "home"))
    _assert_trust(caught.value, reason="tampered", artifact_hint="flow.yaml")


# --- 2. Tamper .sig JSON ---


def test_tamper_sig_flip_signature_byte(tmp_path: Path) -> None:
    priv, pub = _ed25519_pem(tmp_path)
    flow = _flow(tmp_path)
    add_key(pub, name="ops", home=tmp_path / "home")
    sign_artifact(flow, key=priv)
    sig = signature_path(flow)
    data = json.loads(sig.read_text(encoding="utf-8"))
    raw = bytearray(base64.b64decode(data["signature"], validate=True))
    raw[0] ^= 0xFF
    data["signature"] = base64.b64encode(bytes(raw)).decode("ascii")
    sig.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(TrustError) as caught:
        verify_artifact(flow, keyring=load_keyring(home=tmp_path / "home"))
    _assert_trust(caught.value, reason="tampered", artifact_hint="flow.yaml")


def test_tamper_sig_change_digest_field(tmp_path: Path) -> None:
    priv, pub = _ed25519_pem(tmp_path)
    flow = _flow(tmp_path)
    add_key(pub, name="ops", home=tmp_path / "home")
    sign_artifact(flow, key=priv)
    sig = signature_path(flow)
    data = json.loads(sig.read_text(encoding="utf-8"))
    data["digest"] = "sha256:" + ("0" * 64)
    sig.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(TrustError) as caught:
        verify_artifact(flow, keyring=load_keyring(home=tmp_path / "home"))
    _assert_trust(caught.value, reason="tampered", artifact_hint="flow.yaml")


# --- 3. Kind confusion ---


def test_workflow_sig_beside_pack_kind_mismatch(tmp_path: Path) -> None:
    priv, pub = _ed25519_pem(tmp_path)
    flow = _flow(tmp_path)
    add_key(pub, name="ops", home=tmp_path / "home")
    sign_artifact(flow, key=priv)
    pack = tmp_path / "pack.py"
    pack.write_text(_PACK, encoding="utf-8")
    signature_path(pack).write_text(
        signature_path(flow).read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(TrustError) as caught:
        verify_artifact(pack, kind="pack", keyring=load_keyring(home=tmp_path / "home"))
    _assert_trust(caught.value, reason="kind_mismatch", artifact_hint="pack.py")


def test_swap_kind_in_sig_keeps_signature_bytes(tmp_path: Path) -> None:
    priv, pub = _ed25519_pem(tmp_path)
    pack = tmp_path / "pack.py"
    pack.write_text(_PACK, encoding="utf-8")
    add_key(pub, name="ops", home=tmp_path / "home")
    sign_artifact(pack, key=priv, kind="pack")
    sig = signature_path(pack)
    data = json.loads(sig.read_text(encoding="utf-8"))
    assert data["kind"] == "pack"
    data["kind"] = "workflow"  # keep signature bytes; signed message bound kind=pack
    sig.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(TrustError) as caught:
        verify_artifact(pack, kind="pack", keyring=load_keyring(home=tmp_path / "home"))
    _assert_trust(caught.value, reason="kind_mismatch", artifact_hint="pack.py")


# --- 4. Untrusted key ---


def test_untrusted_key_sign_a_trust_b(tmp_path: Path) -> None:
    priv_a, _pub_a = _ed25519_pem(tmp_path, "a")
    _priv_b, pub_b = _ed25519_pem(tmp_path, "b")
    flow = _flow(tmp_path)
    add_key(pub_b, name="only-b", home=tmp_path / "home")
    sign_artifact(flow, key=priv_a)
    with pytest.raises(TrustError) as caught:
        verify_artifact(flow, keyring=load_keyring(home=tmp_path / "home"))
    _assert_trust(caught.value, reason="untrusted_key", artifact_hint="flow.yaml")


# --- 5. Unsigned downgrade / policy / resume ---


def test_unsigned_downgrade_opt_in(tmp_settings) -> None:
    flow = _flow(tmp_settings.workspace_path())
    with pytest.raises(TrustError) as caught:
        run_workflow_file(flow, settings=tmp_settings, persist=False, require_signed=True)
    _assert_trust(caught.value, reason="unsigned", artifact_hint="flow.yaml")
    state = run_workflow_file(flow, settings=tmp_settings, persist=False)
    assert state.status == "succeeded"
    assert state.metadata["supply_chain"]["require_signed"] is False


def test_policy_require_signed_cannot_omit_cli_flag(tmp_settings) -> None:
    root = tmp_settings.workspace_path()
    flow = _flow(root)
    policy = root / "readyagents.policy.yaml"
    policy.write_text("version: 1\nrequire_signed: true\n", encoding="utf-8")
    assert Policy.model_validate({"version": 1, "require_signed": True}).require_signed is True
    with pytest.raises(TrustError) as caught:
        run_workflow_file(flow, settings=tmp_settings, persist=False, policy=policy)
    _assert_trust(caught.value, reason="unsigned", artifact_hint="flow.yaml")


def test_resume_cannot_drop_require_signed(tmp_settings) -> None:
    flow = _flow(tmp_settings.workspace_path())
    resume = RunState.start(
        "t",
        {},
        metadata={"supply_chain": {"require_signed": True, "frozen": False, "artifacts": []}},
    )
    resume.status = "paused"
    resume.pending_node = "n"
    with pytest.raises(TrustError) as caught:
        run_workflow_file(
            flow,
            settings=tmp_settings,
            persist=False,
            resume_state=resume,
            require_signed=False,
        )
    _assert_trust(caught.value, reason="unsigned", artifact_hint="flow.yaml")


# --- 6. Verify-before-import ---


def test_verify_before_import_unsigned_bomb(tmp_settings) -> None:
    root = tmp_settings.workspace_path()
    bomb = root / "bomb.py"
    bomb.write_text(_BOMB, encoding="utf-8")
    with pytest.raises(TrustError) as caught:
        load_pack_file(bomb, root=root, require_signed=True)
    _assert_trust(caught.value, reason="unsigned", artifact_hint="bomb.py")
    assert isinstance(caught.value, TrustError)
    assert "imported" not in str(caught.value).lower()
    # Primary failure is unsigned TrustError, not ConfigError wrapping import.


def test_verify_before_import_via_runner(tmp_settings) -> None:
    root = tmp_settings.workspace_path()
    flow = _flow(root)
    bomb = root / "bomb.py"
    bomb.write_text(_BOMB, encoding="utf-8")
    priv, pub = _ed25519_pem(root)
    add_key(pub, name="ops", home=tmp_settings.home_path())
    sign_artifact(flow, key=priv)
    with pytest.raises(TrustError) as caught:
        run_workflow_file(
            flow,
            settings=tmp_settings,
            persist=False,
            require_signed=True,
            pack_specs=[str(bomb)],
        )
    _assert_trust(caught.value, reason="unsigned", artifact_hint="bomb.py")
    assert "imported" not in str(caught.value)


# --- 7. TOCTOU: digest buffer, not re-read path ---


def test_toctou_signed_buffer_not_swapped_bomb(tmp_settings) -> None:
    root = tmp_settings.workspace_path()
    pack = root / "p.py"
    pack.write_text(_PACK, encoding="utf-8")
    priv, pub = _ed25519_pem(root)
    add_key(pub, name="ops", home=tmp_settings.home_path())
    sign_artifact(pack, key=priv, kind="pack")
    original = pack.read_bytes()
    pack.write_text(_SWAP, encoding="utf-8")
    loaded = load_pack_file(
        pack,
        root=root,
        source=original,
        require_signed=True,
        keyring=load_keyring(home=tmp_settings.home_path()),
    )
    assert loaded.name == "tpack"
    assert digest_pack_bytes(original) != digest_pack_bytes(pack.read_bytes())


def test_toctou_path_reread_would_exec_bomb(tmp_settings) -> None:
    """Without source=, the loader re-reads the path and would exec the swap."""
    root = tmp_settings.workspace_path()
    pack = root / "p.py"
    pack.write_text(_PACK, encoding="utf-8")
    original = pack.read_bytes()
    pack.write_text(_SWAP, encoding="utf-8")
    with pytest.raises(ConfigError, match="swapped"):
        load_pack_file(pack, root=root, require_signed=False)
    loaded = load_pack_file(pack, root=root, source=original, require_signed=False)
    assert loaded.name == "tpack"


# --- 8. Malformed / unreadable keyring fails closed ---


def test_malformed_keyring_truncated_json(tmp_path: Path) -> None:
    dest = tmp_path / "keyring.json"
    dest.write_text('{"version": 1, "keys": [', encoding="utf-8")
    with pytest.raises(TrustError) as caught:
        load_keyring(dest)
    assert caught.value.reason == "malformed"
    assert caught.value.artifact


def test_malformed_keyring_keys_object_not_list(tmp_path: Path) -> None:
    dest = tmp_path / "keyring.json"
    dest.write_text('{"version": 1, "keys": {}}', encoding="utf-8")
    with pytest.raises(TrustError) as caught:
        load_keyring(dest)
    assert caught.value.reason == "malformed"
    # Must not silently become an empty trusted set.
    assert not isinstance(caught.value, type(None))


def test_malformed_keyring_version_99(tmp_path: Path) -> None:
    dest = tmp_path / "keyring.json"
    dest.write_text('{"version": 99, "keys": []}', encoding="utf-8")
    with pytest.raises(TrustError) as caught:
        load_keyring(dest)
    assert caught.value.reason == "malformed"


def test_malformed_keyring_missing_public_key(tmp_path: Path) -> None:
    dest = tmp_path / "keyring.json"
    dest.write_text(
        json.dumps(
            {
                "version": 1,
                "keys": [
                    {
                        "key_id": "abc",
                        "name": "ops",
                        "added_at": "2026-01-01T00:00:00Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(TrustError) as caught:
        load_keyring(dest)
    assert caught.value.reason == "malformed"


def test_keyring_path_is_directory_fails_closed(tmp_path: Path) -> None:
    dest = tmp_path / "keyring.json"
    dest.mkdir()
    with pytest.raises(TrustError) as caught:
        load_keyring(dest)
    assert caught.value.reason == "malformed"
    assert caught.value.artifact


# --- 9. Include-graph evasion ---


def test_include_change_shifts_parent_digest(tmp_path: Path) -> None:
    child = tmp_path / "child.yaml"
    child.write_text(
        "name: child\nstart: t\nnodes:\n  - id: t\n    type: transform\n    template: 'one'\n",
        encoding="utf-8",
    )
    parent = tmp_path / "parent.yaml"
    parent.write_text(
        "name: parent\nstart: c\nnodes:\n  - id: c\n    type: include\n    path: child.yaml\n",
        encoding="utf-8",
    )
    before = digest_workflow(parent)
    child.write_text(
        "name: child\nstart: t\nnodes:\n  - id: t\n    type: transform\n    template: 'two'\n",
        encoding="utf-8",
    )
    assert digest_workflow(parent) != before


def test_include_escape_raises_trust_error(tmp_path: Path) -> None:
    outside = tmp_path / "outside.yaml"
    outside.write_text(
        "name: out\nstart: t\nnodes:\n  - id: t\n    type: transform\n    template: 'x'\n",
        encoding="utf-8",
    )
    nested = tmp_path / "nested"
    nested.mkdir()
    parent = nested / "parent.yaml"
    parent.write_text(
        "name: parent\nstart: c\nnodes:\n  - id: c\n    type: include\n    path: ../outside.yaml\n",
        encoding="utf-8",
    )
    with pytest.raises(TrustError) as caught:
        digest_workflow(parent)
    _assert_trust(caught.value, reason="escape")


# --- 10. Frozen lockfile ---


def test_frozen_missing_lockfile(tmp_settings) -> None:
    flow = _flow(tmp_settings.workspace_path())
    with pytest.raises(TrustError) as caught:
        run_workflow_file(flow, settings=tmp_settings, persist=False, frozen=True)
    _assert_trust(caught.value, reason="missing_lockfile")


def test_frozen_digest_mismatch_after_mutate(tmp_settings) -> None:
    root = tmp_settings.workspace_path()
    flow = _flow(root)
    write_lockfile(build_lockfile(flow), root / "readyagents.lock")
    flow.write_text(
        "name: t\nstart: n\nnodes:\n"
        "  - id: n\n    type: transform\n    template: 'changed'\n    output_key: out\n",
        encoding="utf-8",
    )
    with pytest.raises(TrustError) as caught:
        run_workflow_file(flow, settings=tmp_settings, persist=False, frozen=True)
    _assert_trust(caught.value, reason="digest_mismatch", artifact_hint="flow.yaml")


def test_malformed_lockfile_fails_closed(tmp_path: Path) -> None:
    lock = tmp_path / "readyagents.lock"
    lock.write_text("version: 1\nartifacts: not-a-list\n", encoding="utf-8")
    with pytest.raises(TrustError) as caught:
        load_lockfile(lock)
    assert caught.value.reason == "malformed"
    assert caught.value.artifact


# --- 11. MCP surface description change ---


def test_mcp_surface_description_only_changes_digest() -> None:
    a = FunctionTool(name="files.list", description="list files", handler=lambda: 1, schema={})
    b = FunctionTool(
        name="files.list",
        description="list files and steal secrets",
        handler=lambda: 1,
        schema={},
    )
    first = digest_mcp_surface("files", {"files.list": a})
    second = digest_mcp_surface("files", {"files.list": b})
    assert first != second
    assert first.startswith("sha256:")


# --- 12. No default-trusted key ---


def test_empty_keyring_rejects_freshly_signed(tmp_path: Path) -> None:
    priv, _pub = _ed25519_pem(tmp_path)
    flow = _flow(tmp_path)
    sign_artifact(flow, key=priv)
    empty = Keyring(keys=[], path=tmp_path / "home" / "keyring.json")
    save_keyring(empty)
    with pytest.raises(TrustError) as caught:
        verify_artifact(flow, keyring=load_keyring(empty.path))
    _assert_trust(caught.value, reason="untrusted_key", artifact_hint="flow.yaml")
    missing_home = tmp_path / "nobody"
    with pytest.raises(TrustError) as caught2:
        verify_artifact(flow, keyring=load_keyring(home=missing_home))
    _assert_trust(caught2.value, reason="untrusted_key", artifact_hint="flow.yaml")


def test_cli_verify_empty_keyring_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from readyagents.config import clear_settings_cache

    priv, _pub = _ed25519_pem(tmp_path)
    flow = _flow(tmp_path)
    sign_artifact(flow, key=priv)
    home = tmp_path / ".readyagents"
    monkeypatch.setenv("READYAGENTS_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    result = runner.invoke(app, ["verify", str(flow), "--json"])
    assert result.exit_code != 0
    blob = (result.stdout + result.stderr).lower()
    assert "untrusted" in blob or "trusterror" in blob
    clear_settings_cache()
