"""Digest a skill folder. Same sha256: prefix as packs and workflows."""

from __future__ import annotations

import hashlib
from pathlib import Path

from readyagents.errors import SkillRefused
from readyagents.trust.digest import KIND_SKILL, prefixed

__all__ = ["KIND_SKILL", "digest_skill_dir"]


def digest_skill_dir(root: Path) -> str:
    folder = Path(root)
    if not folder.is_dir():
        raise SkillRefused(f"skill folder not found: {folder}", reason="missing")
    h = hashlib.sha256()
    for path in _iter_files(folder):
        rel = path.relative_to(folder).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return prefixed(h.hexdigest())


def _iter_files(folder: Path):
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise SkillRefused(f"skill contains a symlink: {path}", reason="symlink")
        if path.is_file() and path.suffix.lower() != ".sig":
            yield path
