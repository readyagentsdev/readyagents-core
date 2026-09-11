"""Install a skill folder into the confined catalog. No marketplace."""

from __future__ import annotations

import shutil
import stat
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from readyagents.errors import SkillPathDenied, SkillRefused
from readyagents.skills.catalog import catalog_dir, skill_dir, upsert
from readyagents.skills.digest import digest_skill_dir
from readyagents.skills.parse import load_skill_dir
from readyagents.trust.digest import prefixed

MAX_ARCHIVE_BYTES = 1_048_576
MAX_URL_BYTES = 1_048_576


def add_skill(
    source: str | Path,
    *,
    home: Path,
    require_signature: bool = False,
) -> dict[str, Any]:
    raw = str(source).strip()
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"}:
        extracted = _fetch_url(raw, home)
        try:
            return _install_dir(
                extracted, home=home, source=raw, require_signature=require_signature
            )
        finally:
            shutil.rmtree(extracted, ignore_errors=True)
    path = Path(raw).expanduser()
    if not path.exists():
        raise SkillRefused(f"skill source not found: {path}", reason="missing")
    if path.is_file() and path.suffix.lower() in {".zip", ".tar", ".gz", ".tgz"}:
        extracted = _extract_archive(path, home)
        try:
            return _install_dir(
                extracted, home=home, source=str(path), require_signature=require_signature
            )
        finally:
            shutil.rmtree(extracted, ignore_errors=True)
    if path.is_dir():
        return _install_dir(
            path, home=home, source=str(path.resolve()), require_signature=require_signature
        )
    raise SkillRefused(f"skill source is not a folder or archive: {path}", reason="source")


def _install_dir(
    src: Path,
    *,
    home: Path,
    source: str,
    require_signature: bool,
) -> dict[str, Any]:
    _assert_no_symlinks(src)
    record = load_skill_dir(src)
    dest = skill_dir(home, record.name)
    catalog_dir(home).mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    _copy_confined(src, dest)
    record.path = str(dest)
    status = _signature_status(dest, require_signature=require_signature)
    return upsert(home, record, source=source, signature_status=status)


def _copy_confined(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if any(part in {"..", ""} for part in rel.parts):
            raise SkillPathDenied(f"skill member escapes catalog: {rel}")
        target = dest / rel
        if path.is_symlink():
            raise SkillPathDenied(f"skill contains a symlink: {rel}")
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _assert_no_symlinks(root: Path) -> None:
    if root.is_symlink():
        raise SkillPathDenied(f"skill root is a symlink: {root}")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise SkillPathDenied(f"skill contains a symlink: {path}")


def _extract_archive(archive: Path, home: Path) -> Path:
    size = archive.stat().st_size
    if size > MAX_ARCHIVE_BYTES:
        raise SkillRefused("skill archive exceeds size cap", reason="too_large")
    catalog_dir(home).mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="ra-skill-", dir=str(catalog_dir(home))))
    if archive.suffix.lower() == ".zip" or archive.name.endswith(".zip"):
        _extract_zip(archive, tmp)
    else:
        _extract_tar(archive, tmp)
    inner = _skill_root(tmp)
    return inner


def _extract_zip(archive: Path, dest: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            _reject_member(name, info.is_dir())
            if stat.S_ISLNK(info.external_attr >> 16):
                raise SkillPathDenied(f"archive member is a symlink: {name}")
            target = (dest / name).resolve()
            if dest.resolve() not in target.parents and target != dest.resolve():
                raise SkillPathDenied(f"zip slip refused: {name}")
        zf.extractall(dest)


def _extract_tar(archive: Path, dest: Path) -> None:
    with tarfile.open(archive) as tf:
        for member in tf.getmembers():
            name = member.name.replace("\\", "/")
            _reject_member(name, member.isdir())
            if member.issym() or member.islnk():
                raise SkillPathDenied(f"archive member is a symlink: {name}")
            target = (dest / name).resolve()
            if dest.resolve() not in target.parents and target != dest.resolve():
                raise SkillPathDenied(f"tar slip refused: {name}")
        tf.extractall(dest)


def _reject_member(name: str, is_dir: bool) -> None:
    del is_dir
    if name.startswith("/") or name.startswith("\\"):
        raise SkillPathDenied(f"absolute archive member refused: {name}")
    parts = Path(name).parts
    if ".." in parts:
        raise SkillPathDenied(f"archive traversal refused: {name}")


def _skill_root(extracted: Path) -> Path:
    if (extracted / "SKILL.md").is_file():
        return extracted
    children = [p for p in extracted.iterdir() if p.is_dir() and not p.name.startswith(".")]
    if len(children) == 1 and (children[0] / "SKILL.md").is_file():
        return children[0]
    raise SkillRefused("archive does not contain SKILL.md", reason="missing")


def _fetch_url(url: str, home: Path) -> Path:
    import urllib.request

    catalog_dir(home).mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="ra-skill-url-", dir=str(catalog_dir(home))))
    dest = tmp / "download"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
            data = resp.read(MAX_URL_BYTES + 1)
    except OSError as extra:
        raise SkillRefused(f"skill URL fetch failed: {extra}", reason="url") from extra
    if len(data) > MAX_URL_BYTES:
        raise SkillRefused("skill URL exceeds size cap", reason="too_large")
    dest.write_bytes(data)
    if zipfile.is_zipfile(dest):
        inner = Path(tempfile.mkdtemp(prefix="ra-skill-unz-", dir=str(tmp)))
        _extract_zip(dest, inner)
        return _skill_root(inner)
    raise SkillRefused("skill URL must be a zip archive", reason="source")


def _signature_status(folder: Path, *, require_signature: bool) -> str:
    sig = folder / "SKILL.md.sig"
    digest = digest_skill_dir(folder)
    if not sig.is_file():
        if require_signature:
            raise SkillRefused("unsigned skill: signature required", reason="unsigned")
        return "unsigned"
    try:
        from readyagents.skills.digest import KIND_SKILL
        from readyagents.trust.sign import verify_artifact

        verify_artifact(folder / "SKILL.md", kind=KIND_SKILL)
        return "signed"
    except Exception:
        text = sig.read_text(encoding="utf-8").strip()
        if prefixed(text) == digest or text == digest:
            return "signed"
        raise SkillRefused("skill signature mismatch", reason="forged") from None
