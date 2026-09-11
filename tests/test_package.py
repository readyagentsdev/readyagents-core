"""Shipped package manifest, build, install, catalog, and signed index."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.errors import PackageNeedsConfirm, PackagePathDenied, PackageRefused
from readyagents.firewall.policy_file import load_policy
from readyagents.package.archive import build_package, digest_archive
from readyagents.package.catalog import (
    get_record,
    list_records,
    load_overlay,
    save_overlay,
    upgrade_package,
)
from readyagents.package.install import install_package
from readyagents.package.manifest import load_manifest, parse_manifest_text
from readyagents.testing.eval import load_eval_suite, run_eval

runner = CliRunner()


def _manifest(**extra) -> str:
    body = {
        "name": extra.pop("name", "demo-calc"),
        "version": extra.pop("version", "1.0.0"),
        "description": extra.pop("description", "Packaged calc workflow for packaging tests only."),
        "entry": extra.pop("entry", "workflow.yaml"),
        "secrets": extra.pop("secrets", []),
        "policy": extra.pop("policy", "policy.yaml"),
        "fixtures": extra.pop("fixtures", "evals/suite.yaml"),
        "docs": extra.pop("docs", "README.md"),
        "budget": extra.pop("budget", {"max_cost_usd": 0.5}),
        "requires": extra.pop("requires", {"readyagents": ">=1.9", "connectors": []}),
    }
    body.update(extra)
    return yaml.safe_dump(body, sort_keys=False)


def _workflow() -> str:
    return (
        "name: demo-calc\n"
        "start: add\n"
        "nodes:\n"
        "  - id: add\n"
        "    type: tool\n"
        "    tool: calc\n"
        "    arguments:\n"
        "      expression: '2+2'\n"
        "    output_key: sum\n"
        "    next: ok\n"
        "  - id: ok\n"
        "    type: transform\n"
        "    template: 'demo-calc ok: {{ sum }}'\n"
        "    output_key: summary\n"
    )


def _write_pkg(
    tmp: Path, *, name: str = "demo-calc", version: str = "1.0.0", extra_files=None
) -> Path:
    root = tmp / name
    root.mkdir(parents=True)
    (root / "workflow.yaml").write_text(_workflow(), encoding="utf-8", newline="\n")
    (root / "policy.yaml").write_text(
        "version: 1\ndefault: deny\ntools:\n  calc: {}\n", encoding="utf-8", newline="\n"
    )
    evals = root / "evals"
    evals.mkdir()
    (evals / "suite.yaml").write_text(
        "cases:\n  - name: demo-calc\n    workflow: ../workflow.yaml\n"
        "    expect_status: succeeded\n    expect_contains:\n      summary: demo-calc ok\n",
        encoding="utf-8",
        newline="\n",
    )
    (root / "README.md").write_text("demo calc package\n", encoding="utf-8", newline="\n")
    (root / "readyagents.pkg.yaml").write_text(
        _manifest(name=name, version=version), encoding="utf-8", newline="\n"
    )
    for rel, text in extra_files or []:
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8", newline="\n")
    return root


def test_manifest_refuses_unknown_key_bad_semver_missing_entry(tmp_path: Path) -> None:
    with pytest.raises(PackageRefused, match="Extra") as extra:
        parse_manifest_text(_manifest(marketplace=True))
    assert extra.value.reason in {"unknown_key", "malformed"}
    with pytest.raises(PackageRefused, match="semver"):
        parse_manifest_text(_manifest(version="v1"))
    with pytest.raises(PackageRefused):
        parse_manifest_text(_manifest(entry=""))
    rec = load_manifest(_write_pkg(tmp_path / "ok"))
    assert rec.name == "demo-calc"
    assert rec.entry == "workflow.yaml"
    assert rec.secrets == []


def test_build_is_byte_identical_and_contains_lock(tmp_path: Path) -> None:
    src = _write_pkg(tmp_path / "src")
    a = build_package(src, out=tmp_path / "a.rapkg")
    b = build_package(src, out=tmp_path / "b.rapkg")
    assert a.read_bytes() == b.read_bytes()
    assert digest_archive(a) == digest_archive(b)
    assert digest_archive(a).startswith("sha256:")
    with zipfile.ZipFile(a) as zf:
        names = set(zf.namelist())
    assert "readyagents.pkg.yaml" in names
    assert "workflow.yaml" in names
    assert "policy.yaml" in names
    assert "evals/suite.yaml" in names
    assert "README.md" in names
    assert "readyagents.lock" in names


def test_build_refuses_secret_value(tmp_path: Path) -> None:
    src = _write_pkg(tmp_path / "src")
    (src / "workflow.yaml").write_text(
        _workflow() + "\n# api_key: sk-plantedsecretvalue\n", encoding="utf-8"
    )
    with pytest.raises(PackageRefused, match="secret") as caught:
        build_package(src, out=tmp_path / "x.rapkg")
    assert caught.value.reason == "secret"


def test_install_requires_confirm_and_never_imports(tmp_path: Path, tmp_settings) -> None:
    src = _write_pkg(
        tmp_path / "src",
        extra_files=[("bomb.py", "raise RuntimeError('imported during install')\n")],
    )
    # bomb.py is not a declared member unless listed; add to files:
    (src / "readyagents.pkg.yaml").write_text(
        _manifest(files=["bomb.py"]), encoding="utf-8", newline="\n"
    )
    archive = build_package(src, out=tmp_path / "demo.rapkg")
    home = tmp_settings.home_path()
    with pytest.raises(PackageNeedsConfirm) as caught:
        install_package(archive, home=home, confirm=False)
    review = caught.value.review or {}
    assert review["name"] == "demo-calc"
    assert "calc" in review["tools"]
    assert review["signature"] == "unsigned"
    assert "secrets" in review
    assert "budget" in review
    assert "approvals" in review
    assert list_records(home) == []
    assert not (home / "packages" / "demo-calc").exists()

    row = install_package(archive, home=home, confirm=True)
    assert row["name"] == "demo-calc"
    assert "bomb.py" in (Path(row["path"]) / "bomb.py").as_posix()
    # Copying bytes must not import bomb.py.
    import sys

    assert not any(Path(m).name == "bomb.py" for m in sys.modules)


def test_install_policy_narrowing_shown(tmp_path: Path, tmp_settings) -> None:
    src = _write_pkg(tmp_path / "src")
    archive = build_package(src, out=tmp_path / "demo.rapkg")
    policy_path = tmp_path / "local.yaml"
    policy_path.write_text("version: 1\ndefault: deny\ntools:\n  now: {}\n", encoding="utf-8")
    policy = load_policy(policy_path)
    with pytest.raises(PackageNeedsConfirm) as caught:
        install_package(archive, home=tmp_settings.home_path(), confirm=False, policy=policy)
    review = caught.value.review or {}
    assert review["constrained"] is True
    assert "calc" in review["policy_diff"]["extra_tools"]


def test_extract_refuses_zip_slip(tmp_path: Path, tmp_settings) -> None:
    from readyagents.package.extract import extract_archive

    zpath = tmp_path / "slip.rapkg"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("../escape/readyagents.pkg.yaml", _manifest())
    with pytest.raises(PackageRefused) as caught:
        extract_archive(zpath.read_bytes(), tmp_path / "out")
    assert isinstance(caught.value, PackagePathDenied) or caught.value.reason == "path"
    with pytest.raises(PackageRefused):
        install_package(zpath, home=tmp_settings.home_path(), confirm=True)


def test_catalog_overlay_survives_upgrade(tmp_path: Path, tmp_settings) -> None:
    home = tmp_settings.home_path()
    v1 = _write_pkg(tmp_path / "v1", version="1.0.0")
    a1 = build_package(v1, out=tmp_path / "v1.rapkg")
    install_package(a1, home=home, confirm=True)
    save_overlay(home, "demo-calc", {"default_model": "mock:test", "inputs": {"n": "1"}})
    v2 = _write_pkg(tmp_path / "v2", version="1.0.1")
    a2 = build_package(v2, out=tmp_path / "v2.rapkg")
    with pytest.raises(PackageNeedsConfirm):
        upgrade_package("demo-calc", a2, home=home, confirm=False)
    row = upgrade_package("demo-calc", a2, home=home, confirm=True)
    assert row["version"] == "1.0.1"
    overlay = load_overlay(home, "demo-calc")
    assert overlay["default_model"] == "mock:test"
    listed = get_record(home, "demo-calc")
    assert listed["version"] == "1.0.1"


def test_upgrade_widening_requires_confirm(tmp_path: Path, tmp_settings) -> None:
    home = tmp_settings.home_path()
    v1 = _write_pkg(tmp_path / "v1", version="1.0.0")
    install_package(build_package(v1, out=tmp_path / "v1.rapkg"), home=home, confirm=True)
    v2 = _write_pkg(tmp_path / "v2", version="1.1.0")
    (v2 / "workflow.yaml").write_text(
        _workflow().replace("next: ok", "next: stamp")
        + "  - id: stamp\n    type: tool\n    tool: now\n    output_key: ts\n    next: ok\n",
        encoding="utf-8",
        newline="\n",
    )
    a2 = build_package(v2, out=tmp_path / "v2.rapkg")
    with pytest.raises(PackageNeedsConfirm) as caught:
        upgrade_package("demo-calc", a2, home=home, confirm=False)
    review = caught.value.review or {}
    assert (review.get("upgrade") or {}).get("widened") is True
    assert "now" in (review.get("upgrade") or {}).get("new_tools") or "now" in (
        review.get("tools") or []
    )
    assert get_record(home, "demo-calc")["version"] == "1.0.0"


def test_installed_fixtures_run_eval(tmp_path: Path, tmp_settings) -> None:
    home = tmp_settings.home_path()
    src = _write_pkg(tmp_path / "src")
    archive = build_package(src, out=tmp_path / "demo.rapkg")
    row = install_package(archive, home=home, confirm=True)
    suite = Path(row["path"]) / row["fixtures"]
    report = run_eval(load_eval_suite(suite))
    assert report.ok
    assert report.passed >= 1


def test_cli_package_help_twice_and_install_confirm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / ".readyagents"
    monkeypatch.setenv("READYAGENTS_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    first = runner.invoke(app, ["package", "--help"])
    second = runner.invoke(app, ["package", "--help"])
    assert first.exit_code == 0, first.stdout + first.stderr
    assert second.exit_code == 0
    src = _write_pkg(tmp_path / "src")
    built = runner.invoke(
        app, ["package", "build", str(src), "--out", str(tmp_path / "d.rapkg"), "--json"]
    )
    assert built.exit_code == 0, built.stdout + built.stderr
    payload = json.loads(built.stdout[built.stdout.find("{") :])
    assert payload["ok"] is True
    archive = payload["path"]
    refused = runner.invoke(app, ["package", "install", archive, "--json"])
    assert refused.exit_code == 1
    body = json.loads(refused.stdout[refused.stdout.find("{") :])
    assert body["error"] == "PackageNeedsConfirm"
    assert "calc" in (body.get("review") or {}).get("tools")
    ok = runner.invoke(app, ["package", "install", archive, "--confirm", "--json"])
    assert ok.exit_code == 0, ok.stdout + ok.stderr
    listed = runner.invoke(app, ["package", "list", "--json"])
    assert listed.exit_code == 0
    rows = json.loads(listed.stdout[listed.stdout.find("{") :])["packages"]
    assert rows[0]["name"] == "demo-calc"
    clear_settings_cache()


def _ed25519_pem(tmp: Path) -> tuple[Path, Path]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    priv = tmp / "key.pem"
    pub = tmp / "key.pub.pem"
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


def test_signed_package_round_trip_and_tamper(tmp_path: Path, tmp_settings) -> None:
    pytest.importorskip("cryptography")
    from readyagents.trust.keyring import add_key, load_keyring
    from readyagents.trust.sign import sign_artifact

    priv, pub = _ed25519_pem(tmp_path)
    home = tmp_settings.home_path()
    add_key(pub, name="ops", home=home)
    src = _write_pkg(tmp_path / "src")
    archive = build_package(src, out=tmp_path / "demo.rapkg")
    sign_artifact(archive, key=priv)
    ring = load_keyring(home=home)
    row = install_package(archive, home=home, confirm=True, require_signature=True, keyring=ring)
    assert row["signature_status"] == "signed"
    tampered = tmp_path / "tampered.rapkg"
    tampered.write_bytes(archive.read_bytes() + b"x")
    (tmp_path / "tampered.rapkg.sig").write_text(
        (tmp_path / "demo.rapkg.sig").read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(PackageRefused) as caught:
        install_package(tampered, home=home, confirm=True, require_signature=True, keyring=ring)
    assert caught.value.reason in {"forged", "digest_mismatch", "malformed"}


def test_require_signature_unsigned_refused(tmp_path: Path, tmp_settings) -> None:
    src = _write_pkg(tmp_path / "src")
    archive = build_package(src, out=tmp_path / "demo.rapkg")
    with pytest.raises(PackageRefused, match="unsigned") as caught:
        install_package(
            archive, home=tmp_settings.home_path(), confirm=True, require_signature=True
        )
    assert caught.value.reason == "unsigned"


def test_signed_index_ok_unsigned_refused(tmp_path: Path, tmp_settings) -> None:
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from readyagents.package.index import verify_index
    from readyagents.trust.keyring import add_key, load_keyring
    from readyagents.trust.sign import sign_artifact

    private = Ed25519PrivateKey.generate()
    priv = tmp_path / "key.pem"
    pub = tmp_path / "key.pub.pem"
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
    home = tmp_settings.home_path()
    add_key(pub, name="ops", home=home)
    index_path = tmp_path / "readyagents.index.json"
    index_path.write_text(
        json.dumps(
            {
                "version": 1,
                "packages": [
                    {
                        "name": "demo-calc",
                        "version": "1.0.0",
                        "url": "https://example.invalid/demo-calc-1.0.0.rapkg",
                        "digest": "sha256:" + "ab" * 32,
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(PackageRefused, match="unsigned") as unsigned:
        verify_index(index_path, keyring=load_keyring(home=home))
    assert unsigned.value.reason == "unsigned"
    sign_artifact(index_path, key=priv, kind="package_index")
    verified = verify_index(index_path, keyring=load_keyring(home=home))
    assert verified["ok"] is True
    assert verified["kind"] == "package_index"
    assert verified["packages"][0]["name"] == "demo-calc"
