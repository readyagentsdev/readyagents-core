"""Reproducible ``.rapkg`` archives: sorted stored zip + member digest lockfile."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from readyagents.errors import PackageRefused
from readyagents.package.layout import (
    ARCHIVE_SUFFIX,
    KIND_MEMBER,
    LOCK_NAME,
    MANIFEST_NAME,
    MAX_ARCHIVE_BYTES,
    MAX_FILES,
    MAX_MEMBER_BYTES,
    ZIP_EPOCH,
)
from readyagents.package.manifest import PackageManifest, load_manifest
from readyagents.package.secrets import scan_tree_for_secret_values
from readyagents.trust.digest import DIGEST_ALGORITHM, DIGEST_VERSION, digest_bytes, prefixed


def build_package(source: Path | str, *, out: Path | str | None = None) -> Path:
    root = Path(source).expanduser().resolve()
    if not root.is_dir():
        raise PackageRefused(f"package source is not a directory: {root}", reason="missing")
    manifest = load_manifest(root)
    scan_tree_for_secret_values(root)
    members = _collect_members(root, manifest)
    if len(members) > MAX_FILES:
        raise PackageRefused("package exceeds file count cap", reason="too_many")
    lock_body = _lock_yaml(members)
    payload = dict(members)
    payload[LOCK_NAME] = lock_body.encode("utf-8")
    if out is not None:
        dest = Path(out)
    else:
        dest = root / f"{manifest.name}-{manifest.version}{ARCHIVE_SUFFIX}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}.{uuid4().hex}.tmp"
    try:
        _write_zip(tmp, payload)
        size = tmp.stat().st_size
        if size > MAX_ARCHIVE_BYTES:
            raise PackageRefused("package archive exceeds size cap", reason="too_large")
        with zipfile.ZipFile(tmp, mode="r") as zf:
            bad = zf.testzip()
        if bad:
            raise PackageRefused(f"package archive member corrupt: {bad}", reason="malformed")
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return dest


def digest_archive(path: Path | str, *, data: bytes | None = None) -> str:
    payload = data if data is not None else Path(path).read_bytes()
    return digest_bytes(payload)


def _collect_members(root: Path, manifest: PackageManifest) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    for rel in manifest.member_paths():
        path = root / rel
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_symlink():
                    raise PackageRefused(f"package contains a symlink: {child}", reason="symlink")
                if not child.is_file():
                    continue
                inner = child.relative_to(root).as_posix()
                members[inner] = _read_member(child, inner)
            continue
        if path.is_symlink():
            raise PackageRefused(f"package contains a symlink: {path}", reason="symlink")
        if not path.is_file():
            raise PackageRefused(f"package member not found: {rel}", reason="missing")
        members[rel] = _read_member(path, rel)
    if MANIFEST_NAME not in members:
        raise PackageRefused("package manifest missing from archive", reason="missing")
    return dict(sorted(members.items()))


def _read_member(path: Path, rel: str) -> bytes:
    data = path.read_bytes()
    if len(data) > MAX_MEMBER_BYTES:
        raise PackageRefused(f"package member exceeds size cap: {rel}", reason="too_large")
    return data


def _lock_yaml(members: dict[str, bytes]) -> str:
    artifacts = [
        {"kind": KIND_MEMBER, "path": name, "digest": prefixed(digest_bytes(data))}
        for name, data in sorted(members.items())
    ]
    body: dict[str, Any] = {
        "version": 1,
        "digest_algorithm": DIGEST_ALGORITHM,
        "digest_version": DIGEST_VERSION,
        "artifacts": artifacts,
    }
    return yaml.safe_dump(body, sort_keys=False, allow_unicode=True)


def _write_zip(path: Path, members: dict[str, bytes]) -> None:
    """Stored zip of files only. Directory entries are omitted (Windows zipfile)."""
    with zipfile.ZipFile(path, mode="w", compression=zipfile.ZIP_STORED, allowZip64=False) as zf:
        for name, data in sorted(members.items()):
            rel = name.replace("\\", "/")
            info = zipfile.ZipInfo(filename=rel, date_time=ZIP_EPOCH)
            zf.writestr(info, data)
