"""Refuse secret *values* in a package tree. Names in ``secrets:`` are allowed."""

from __future__ import annotations

import re
from pathlib import Path

from readyagents.errors import PackageRefused
from readyagents.package.layout import MANIFEST_NAME

_TEXT_SUFFIX = {".yaml", ".yml", ".md", ".json", ".txt", ".sh", ".env", ".toml", ".cfg"}
_SK = re.compile(r"sk-[A-Za-z0-9]{8,}")
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_ASSIGNED = re.compile(
    r"(?im)^(?:export\s+)?(?:api[_-]?key|secret|password|token|private[_-]?key)\s*[:=]\s*\S+"
)
_INLINE = re.compile(
    r"(?i)(?:api[_-]?key|secret|password|token)\s*:\s*['\"]?(sk-|ghp_|AKIA)[A-Za-z0-9]"
)


def scan_tree_for_secret_values(root: Path) -> None:
    folder = Path(root)
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in _TEXT_SUFFIX and path.name not in {MANIFEST_NAME, ".env"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as extra:
            raise PackageRefused(f"package file unreadable: {path}", reason="unreadable") from extra
        scan_text_for_secret_values(text, path=path)


def scan_text_for_secret_values(text: str, *, path: Path | str) -> None:
    if _PEM.search(text):
        raise PackageRefused(f"package contains a private key: {path}", reason="secret")
    if _SK.search(text):
        raise PackageRefused(f"package contains a secret value: {path}", reason="secret")
    if _INLINE.search(text):
        raise PackageRefused(f"package contains a secret value: {path}", reason="secret")
    for match in _ASSIGNED.finditer(text):
        line = match.group(0)
        # Manifest `secrets: [NAME]` is names only; an assignment is a value.
        if re.search(r"[:=]\s*\S+", line):
            raise PackageRefused(f"package contains a secret value: {path}", reason="secret")
