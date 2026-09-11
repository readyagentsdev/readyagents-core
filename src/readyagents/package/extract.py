"""Confined archive extraction. Copies bytes. Never imports or executes."""

from __future__ import annotations

import io
import stat
import zipfile
from pathlib import Path

from readyagents.errors import PackagePathDenied, PackageRefused
from readyagents.package.layout import (
    MANIFEST_NAME,
    MAX_ARCHIVE_BYTES,
    MAX_DEPTH,
    MAX_FILES,
    MAX_MEMBER_BYTES,
)


def open_archive_bytes(path: Path | str, *, data: bytes | None = None) -> bytes:
    if data is not None:
        blob = data
    else:
        file = Path(path)
        if not file.is_file():
            raise PackageRefused(f"package archive not found: {file}", reason="missing")
        size = file.stat().st_size
        if size > MAX_ARCHIVE_BYTES:
            raise PackageRefused("package archive exceeds size cap", reason="too_large")
        blob = file.read_bytes()
    if len(blob) > MAX_ARCHIVE_BYTES:
        raise PackageRefused("package archive exceeds size cap", reason="too_large")
    return blob


def list_members(blob: bytes) -> list[zipfile.ZipInfo]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as extra:
        raise PackageRefused("package archive is not a zip", reason="malformed") from extra
    with zf:
        infos = list(zf.infolist())
    if len(infos) > MAX_FILES:
        raise PackageRefused("package exceeds file count cap", reason="too_many")
    return infos


def extract_archive(blob: bytes, dest: Path) -> Path:
    """Write archive members under dest. Never import, chmod +x, or run."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    infos = list_members(blob)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        files = 0
        for info in infos:
            name = info.filename.replace("\\", "/")
            _reject_member(name, info)
            if name.endswith("/"):
                (dest / name).mkdir(parents=True, exist_ok=True)
                continue
            files += 1
            if files > MAX_FILES:
                raise PackageRefused("package exceeds file count cap", reason="too_many")
            if info.file_size > MAX_MEMBER_BYTES:
                raise PackageRefused(f"package member exceeds size cap: {name}", reason="too_large")
            target = (dest / name).resolve()
            if dest.resolve() not in target.parents and target != dest.resolve():
                raise PackagePathDenied(f"zip slip refused: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            data = zf.read(info)
            if len(data) > MAX_MEMBER_BYTES:
                raise PackageRefused(f"package member exceeds size cap: {name}", reason="too_large")
            target.write_bytes(data)
    if not (dest / MANIFEST_NAME).is_file():
        nested = _find_manifest_root(dest)
        return nested
    return dest


def _find_manifest_root(dest: Path) -> Path:
    matches = list(dest.rglob(MANIFEST_NAME))
    if len(matches) == 1:
        return matches[0].parent
    raise PackageRefused("archive does not contain readyagents.pkg.yaml", reason="missing")


def _reject_member(name: str, info: zipfile.ZipInfo) -> None:
    if name.startswith("/") or name.startswith("\\") or re_abs(name):
        raise PackagePathDenied(f"absolute archive member refused: {name}")
    parts = Path(name).parts
    if ".." in parts:
        raise PackagePathDenied(f"archive traversal refused: {name}")
    depth = len([p for p in parts if p not in {".", ""}])
    if name.endswith("/"):
        depth = max(0, depth)
    if depth > MAX_DEPTH:
        raise PackageRefused(f"package member exceeds depth cap: {name}", reason="too_deep")
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode) or (info.header_offset >= 0 and _is_symlink_attr(info)):
        raise PackagePathDenied(f"archive member is a symlink: {name}")
    if info.create_system == 3 and stat.S_ISLNK(mode):
        raise PackagePathDenied(f"archive member is a symlink: {name}")


def _is_symlink_attr(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return stat.S_ISLNK(mode)


def re_abs(name: str) -> bool:
    return len(name) >= 3 and name[1] == ":" and name[2] in {"/", "\\"}
