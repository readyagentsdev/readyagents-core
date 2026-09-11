"""Detached Ed25519 sign/verify and local keyring. Drive shipped sign.py / keyring.py."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.errors import TrustError
from readyagents.trust.keyring import add_key, load_keyring, parse_public_key, remove_key
from readyagents.trust.sign import (
    crypto_available,
    infer_kind,
    sign_artifact,
    signature_path,
    verify_artifact,
)

pytest.importorskip("cryptography")
runner = CliRunner()


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


def _flow(tmp: Path) -> Path:
    path = tmp / "flow.yaml"
    path.write_text(
        "name: signed\nstart: t\nnodes:\n  - id: t\n    type: transform\n    template: 'ok'\n",
        encoding="utf-8",
    )
    return path


def test_sign_verify_round_trip(tmp_path: Path) -> None:
    priv, pub = _ed25519_pem(tmp_path)
    flow = _flow(tmp_path)
    add_key(pub, name="ops", home=tmp_path / "home")
    payload = sign_artifact(flow, key=priv)
    assert payload["kind"] == "workflow"
    assert payload["algorithm"] == "ed25519"
    result = verify_artifact(flow, keyring=load_keyring(home=tmp_path / "home"))
    assert result["ok"] is True
    assert result["digest"] == payload["digest"]
    assert result["kind"] == "workflow"


def test_tampered_artifact_fails(tmp_path: Path) -> None:
    priv, pub = _ed25519_pem(tmp_path)
    flow = _flow(tmp_path)
    add_key(pub, name="ops", home=tmp_path / "home")
    sign_artifact(flow, key=priv)
    flow.write_text(flow.read_text(encoding="utf-8").replace("ok", "no"), encoding="utf-8")
    with pytest.raises(TrustError, match="tampered artifact") as caught:
        verify_artifact(flow, keyring=load_keyring(home=tmp_path / "home"))
    assert caught.value.reason == "tampered"
    assert "flow.yaml" in str(caught.value)


def test_tampered_signature_fails(tmp_path: Path) -> None:
    priv, pub = _ed25519_pem(tmp_path)
    flow = _flow(tmp_path)
    add_key(pub, name="ops", home=tmp_path / "home")
    sign_artifact(flow, key=priv)
    sig = signature_path(flow)
    data = json.loads(sig.read_text(encoding="utf-8"))
    data["signature"] = data["signature"][:-4] + "AAAA"
    sig.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(TrustError, match="tampered signature"):
        verify_artifact(flow, keyring=load_keyring(home=tmp_path / "home"))


def test_workflow_signature_does_not_validate_pack(tmp_path: Path) -> None:
    priv, pub = _ed25519_pem(tmp_path)
    flow = _flow(tmp_path)
    add_key(pub, name="ops", home=tmp_path / "home")
    sign_artifact(flow, key=priv)
    pack = tmp_path / "pack.py"
    pack.write_text("x = 1\n", encoding="utf-8")
    # Copy the workflow signature beside the pack.
    signature_path(pack).write_text(
        signature_path(flow).read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(TrustError, match="cannot validate pack") as caught:
        verify_artifact(pack, kind="pack", keyring=load_keyring(home=tmp_path / "home"))
    assert caught.value.reason == "kind_mismatch"


def test_untrusted_key_fails(tmp_path: Path) -> None:
    priv, _pub = _ed25519_pem(tmp_path)
    other_priv, other_pub = _ed25519_pem(tmp_path, "other")
    flow = _flow(tmp_path)
    add_key(other_pub, name="other", home=tmp_path / "home")
    sign_artifact(flow, key=priv)
    with pytest.raises(TrustError, match="untrusted key") as caught:
        verify_artifact(flow, keyring=load_keyring(home=tmp_path / "home"))
    assert caught.value.reason == "untrusted_key"


def test_unsigned_fails(tmp_path: Path) -> None:
    flow = _flow(tmp_path)
    with pytest.raises(TrustError, match="unsigned artifact") as caught:
        verify_artifact(flow, keyring=load_keyring(home=tmp_path / "home"))
    assert caught.value.reason == "unsigned"


def test_keyring_add_list_remove_permissions(tmp_path: Path) -> None:
    _priv, pub = _ed25519_pem(tmp_path)
    home = tmp_path / "home"
    entry = add_key(pub, name="ops", home=home)
    ring = load_keyring(home=home)
    assert ring.find(entry.key_id) is not None
    from readyagents.permissions import permissions_enforceable

    if permissions_enforceable(home):
        mode = stat.S_IMODE((home / "keyring.json").stat().st_mode)
        assert mode & 0o177 == 0
    removed = remove_key(entry.key_id, home=home)
    assert removed.key_id == entry.key_id
    assert load_keyring(home=home).keys == []


def test_malformed_keyring_fails_closed(tmp_path: Path) -> None:
    dest = tmp_path / "home" / "keyring.json"
    dest.parent.mkdir(parents=True)
    dest.write_text("{not json", encoding="utf-8")
    with pytest.raises(TrustError, match="malformed"):
        load_keyring(dest)
    dest.write_text("[]", encoding="utf-8")
    with pytest.raises(TrustError, match="malformed"):
        load_keyring(dest)
    dest.write_text('{"version": 1, "keys": [{"name": "x"}]}', encoding="utf-8")
    with pytest.raises(TrustError, match="malformed"):
        load_keyring(dest)


def test_missing_keyring_is_empty_not_error(tmp_path: Path) -> None:
    ring = load_keyring(home=tmp_path / "missing")
    assert ring.keys == []


def test_parse_public_key_raw_and_pem(tmp_path: Path) -> None:
    _priv, pub = _ed25519_pem(tmp_path)
    pem_bytes = parse_public_key(pub)
    assert len(pem_bytes) == 32
    assert parse_public_key(pem_bytes) == pem_bytes
    assert parse_public_key(pem_bytes.hex()) == pem_bytes


def test_cli_sign_verify_json_stable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert crypto_available()
    priv, pub = _ed25519_pem(tmp_path)
    flow = _flow(tmp_path)
    home = tmp_path / ".readyagents"
    monkeypatch.setenv("READYAGENTS_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    add = runner.invoke(app, ["trust", "add", str(pub), "--name", "ops"])
    assert add.exit_code == 0, add.stdout + add.stderr
    signed = runner.invoke(app, ["sign", str(flow), "--key", str(priv), "--json"])
    assert signed.exit_code == 0, signed.stdout + signed.stderr
    first = runner.invoke(app, ["verify", str(flow), "--json"])
    second = runner.invoke(app, ["verify", str(flow), "--json"])
    assert first.exit_code == 0, first.stdout
    assert second.exit_code == 0, second.stdout
    a = json.loads(first.stdout)
    b = json.loads(second.stdout)
    assert a["ok"] is True and b["ok"] is True
    assert a["digest"] == b["digest"]
    assert a["kind"] == b["kind"] == "workflow"
    assert a["command"] == b["command"] == "verify"
    clear_settings_cache()


def test_infer_kind(tmp_path: Path) -> None:
    assert infer_kind("x.py") == "pack"
    assert infer_kind("flow.yaml") == "workflow"
    assert infer_kind("SKILL.md") == "skill"
    folder = tmp_path / "house-writing-style"
    folder.mkdir()
    (folder / "SKILL.md").write_text("placeholder", encoding="utf-8")
    assert infer_kind(folder) == "skill"
