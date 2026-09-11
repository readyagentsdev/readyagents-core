"""Adversarial packaging suite. Drive shipped package APIs; fail closed."""

from __future__ import annotations

import io
import json
import stat
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

from readyagents.errors import PackageNeedsConfirm, PackagePathDenied, PackageRefused
from readyagents.package.archive import build_package
from readyagents.package.catalog import get_record, upgrade_package
from readyagents.package.extract import extract_archive
from readyagents.package.index import verify_index
from readyagents.package.install import install_package
from readyagents.package.layout import (
    KIND_MEMBER,
    LOCK_NAME,
    MANIFEST_NAME,
    MAX_DEPTH,
    MAX_FILES,
)
from readyagents.trust.digest import DIGEST_ALGORITHM, DIGEST_VERSION, digest_bytes, prefixed

PKG_NAME = "adv-pkg"
PKG_DESC = "Adversarial packaging fixture for tests only."
SECRET_TOKEN = "sk-abcdefghijksecret"
SECRET_ASSIGN = "api_key: sk-planted"


def _manifest(**extra) -> str:
    body = {
        "name": extra.pop("name", PKG_NAME),
        "version": extra.pop("version", "1.0.0"),
        "description": extra.pop("description", PKG_DESC),
        "entry": extra.pop("entry", "workflow.yaml"),
        "secrets": extra.pop("secrets", []),
    }
    body.update(extra)
    return yaml.safe_dump(body, sort_keys=False)


def _workflow(*, tool_now: bool = False) -> str:
    add_next = "stamp" if tool_now else "ok"
    stamp = ""
    if tool_now:
        stamp = "  - id: stamp\n    type: tool\n    tool: now\n    output_key: ts\n    next: ok\n"
    return (
        f"name: {PKG_NAME}\n"
        "start: add\n"
        "nodes:\n"
        "  - id: add\n"
        "    type: tool\n"
        "    tool: calc\n"
        "    arguments:\n"
        "      expression: '2+2'\n"
        "    output_key: sum\n"
        f"    next: {add_next}\n"
        f"{stamp}"
        "  - id: ok\n"
        "    type: transform\n"
        f"    template: '{PKG_NAME} ok: {{{{ sum }}}}'\n"
        "    output_key: summary\n"
    )


def _write_pkg(
    tmp: Path,
    *,
    version: str = "1.0.0",
    workflow: str | None = None,
    extra_files: list[tuple[str, str]] | None = None,
    **manifest_extra,
) -> Path:
    root = tmp / f"{PKG_NAME}-{version}"
    root.mkdir(parents=True)
    (root / "workflow.yaml").write_text(workflow or _workflow(), encoding="utf-8", newline="\n")
    listed = list(manifest_extra.get("files") or [])
    for rel, text in extra_files or []:
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8", newline="\n")
        if rel not in listed:
            listed.append(rel)
    if listed:
        manifest_extra["files"] = listed
    (root / MANIFEST_NAME).write_text(
        _manifest(version=version, **manifest_extra), encoding="utf-8", newline="\n"
    )
    return root


def _pack_zip(
    members: dict[str, bytes],
    *,
    info_for: dict[str, zipfile.ZipInfo] | None = None,
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_STORED) as zf:
        for name, data in members.items():
            extra = (info_for or {}).get(name)
            if extra is not None:
                zf.writestr(extra, data)
            else:
                zf.writestr(name, data)
    return buf.getvalue()


def _lock_for(members: dict[str, bytes]) -> bytes:
    artifacts = [
        {"kind": KIND_MEMBER, "path": name, "digest": prefixed(digest_bytes(data))}
        for name, data in sorted(members.items())
    ]
    body = {
        "version": 1,
        "digest_algorithm": DIGEST_ALGORITHM,
        "digest_version": DIGEST_VERSION,
        "artifacts": artifacts,
    }
    return yaml.safe_dump(body, sort_keys=False, allow_unicode=True).encode("utf-8")


def _assert_path_denied(caught: pytest.ExceptionInfo[BaseException]) -> None:
    err = caught.value
    assert isinstance(err, PackageRefused)
    assert isinstance(err, PackagePathDenied) or err.reason == "path"


def _assert_nothing_outside(dest: Path, *outside: Path) -> None:
    for path in outside:
        assert not path.exists(), f"refused member wrote outside dest: {path}"
    if not dest.exists():
        return
    root = dest.resolve()
    for path in dest.rglob("*"):
        resolved = path.resolve()
        assert resolved == root or root in resolved.parents, resolved


def test_zip_slip_traversal_extract_refused(tmp_path: Path, tmp_settings) -> None:
    dest = tmp_path / "dest"
    escape = tmp_path / "escape" / MANIFEST_NAME
    blob = _pack_zip({f"../escape/{MANIFEST_NAME}": _manifest().encode("utf-8")})
    with pytest.raises(PackageRefused) as caught:
        extract_archive(blob, dest)
    _assert_path_denied(caught)
    _assert_nothing_outside(dest, escape, tmp_path / "escape")

    archive = tmp_path / "slip.rapkg"
    archive.write_bytes(blob)
    with pytest.raises(PackageRefused) as install_caught:
        install_package(archive, home=tmp_settings.home_path(), confirm=True)
    _assert_path_denied(install_caught)
    _assert_nothing_outside(dest, escape, tmp_path / "escape")


@pytest.mark.parametrize("member", ["/tmp/x", "C:/Windows/x"])
def test_absolute_path_member_refused(tmp_path: Path, member: str) -> None:
    dest = tmp_path / "dest"
    blob = _pack_zip({member: b"absolute-payload\n"})
    outside = Path(member) if member.startswith("/") else dest / member
    existed = outside.exists()
    try:
        with pytest.raises(PackageRefused) as caught:
            extract_archive(blob, dest)
        _assert_path_denied(caught)
        _assert_nothing_outside(dest, dest / member)
        if not existed:
            assert not outside.exists()
    finally:
        if not existed and outside.exists() and outside.is_file():
            outside.unlink()


def test_symlink_member_refused(tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    name = "payload.link"
    info = zipfile.ZipInfo(filename=name)
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    blob = _pack_zip({name: b"../escape/secret"}, info_for={name: info})
    with pytest.raises(PackageRefused) as caught:
        extract_archive(blob, dest)
    _assert_path_denied(caught)
    _assert_nothing_outside(dest, tmp_path / "escape" / "secret")
    planted = dest / name
    assert not planted.exists() or not planted.is_symlink()


def test_too_deep_path_refused(tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    rel = "/".join(["d"] * MAX_DEPTH + ["file.yaml"])
    blob = _pack_zip({rel: b"too-deep\n"})
    with pytest.raises(PackageRefused) as caught:
        extract_archive(blob, dest)
    assert caught.value.reason == "too_deep"


def test_too_many_files_refused(tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    members = {f"f{i:03d}.txt": b"x\n" for i in range(MAX_FILES + 1)}
    blob = _pack_zip(members)
    with pytest.raises(PackageRefused) as caught:
        extract_archive(blob, dest)
    assert caught.value.reason == "too_many"


def test_install_does_not_execute_declared_bomb(tmp_path: Path, tmp_settings) -> None:
    marker = tmp_path / "install-bomb-side-effect"
    bomb_src = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported during install\\n', encoding='utf-8')\n"
        "raise RuntimeError('imported during install')\n"
    )
    src = _write_pkg(tmp_path / "src", extra_files=[("bomb.py", bomb_src)])
    archive = build_package(src, out=tmp_path / "adv-pkg.rapkg")
    for key in [k for k in sys.modules if k == "bomb" or k.endswith(".bomb")]:
        sys.modules.pop(key, None)

    home = tmp_settings.home_path()
    row = install_package(archive, home=home, confirm=True)
    assert row["name"] == PKG_NAME
    installed = Path(row["path"]) / "bomb.py"
    assert installed.is_file()
    assert "imported during install" in installed.read_text(encoding="utf-8")
    assert not marker.exists()
    assert "bomb" not in sys.modules
    assert not any(
        Path(getattr(mod, "__file__", "") or "").name == "bomb.py"
        for mod in sys.modules.values()
        if mod is not None
    )


def test_upgrade_permission_widening_requires_confirm(tmp_path: Path, tmp_settings) -> None:
    home = tmp_settings.home_path()
    v1 = _write_pkg(tmp_path / "v1", version="1.0.0")
    install_package(build_package(v1, out=tmp_path / "v1.rapkg"), home=home, confirm=True)
    assert get_record(home, PKG_NAME)["version"] == "1.0.0"

    v2 = _write_pkg(
        tmp_path / "v2",
        version="1.1.0",
        workflow=_workflow(tool_now=True),
    )
    a2 = build_package(v2, out=tmp_path / "v2.rapkg")
    with pytest.raises(PackageNeedsConfirm) as caught:
        upgrade_package(PKG_NAME, a2, home=home, confirm=False)
    review = caught.value.review or {}
    upgrade = review.get("upgrade") or {}
    assert upgrade.get("widened") is True
    assert "now" in (upgrade.get("new_tools") or [])
    assert get_record(home, PKG_NAME)["version"] == "1.0.0"
    assert (home / "packages" / PKG_NAME / "1.0.0").is_dir()
    assert not (home / "packages" / PKG_NAME / "1.1.0").exists()


@pytest.mark.parametrize(
    "plant",
    [
        ("workflow.yaml", f"# planted {SECRET_TOKEN}\n"),
        ("notes.txt", f"{SECRET_ASSIGN}\n"),
        ("evals/suite.yaml", f"cases: []\n# {SECRET_TOKEN}\n"),
    ],
    ids=["workflow", "listed_file", "fixture_yaml"],
)
def test_build_refuses_secret_smuggling(tmp_path: Path, plant: tuple[str, str]) -> None:
    rel, payload = plant
    if rel == "workflow.yaml":
        workflow = _workflow() + payload
        extra_files = None
        extra: dict = {}
    elif rel.endswith((".yaml", ".yml")):
        workflow = None
        extra_files = [(rel, payload)]
        extra = {"fixtures": rel}
    else:
        workflow = None
        extra_files = [(rel, payload)]
        extra = {}
    src = _write_pkg(tmp_path / "src", workflow=workflow, extra_files=extra_files, **extra)
    with pytest.raises(PackageRefused) as caught:
        build_package(src, out=tmp_path / "secret.rapkg")
    assert caught.value.reason == "secret"
    assert not (tmp_path / "secret.rapkg").exists()


def test_install_handcrafted_zip_with_secret_refused(tmp_path: Path, tmp_settings) -> None:
    workflow = (_workflow() + f"\n# planted {SECRET_TOKEN}\n").encode("utf-8")
    manifest = _manifest().encode("utf-8")
    members = {MANIFEST_NAME: manifest, "workflow.yaml": workflow}
    blob = _pack_zip({**members, LOCK_NAME: _lock_for(members)})
    archive = tmp_path / "secret.rapkg"
    archive.write_bytes(blob)
    home = tmp_settings.home_path()
    with pytest.raises(PackageRefused) as caught:
        install_package(archive, home=home, confirm=True)
    assert caught.value.reason == "secret"
    assert not (home / "packages" / PKG_NAME).exists()


def test_unsigned_index_refused(tmp_path: Path, tmp_settings) -> None:
    _ = tmp_settings.home_path()
    index_path = tmp_path / "readyagents.index.json"
    index_path.write_text(
        json.dumps(
            {
                "version": 1,
                "packages": [
                    {
                        "name": PKG_NAME,
                        "version": "1.0.0",
                        "url": "https://example.invalid/adv-pkg-1.0.0.rapkg",
                        "digest": prefixed(digest_bytes(b"not-a-real-archive")),
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert not (tmp_path / "readyagents.index.json.sig").exists()
    with pytest.raises(PackageRefused) as caught:
        verify_index(index_path)
    assert caught.value.reason == "unsigned"
    with pytest.raises(PackageRefused) as bypass:
        verify_index(index_path, require_signature=False)
    assert bypass.value.reason == "unsigned"
