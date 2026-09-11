"""Bounded, contained source walks. Archives refuse zip-slip."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

from readyagents.errors import (
    KnowledgeArchiveRefused,
    KnowledgePathDenied,
    KnowledgeWalkExceeded,
    PathError,
)
from readyagents.paths import resolve_within

DEFAULT_MAX_FILES = 100
DEFAULT_MAX_BYTES = 10_000_000
DEFAULT_MAX_DEPTH = 8
DEFAULT_MAX_FILE_BYTES = 1_000_000
_ARCHIVE_SUFFIX = {".zip"}


def walk_source(
    spec: dict[str, Any] | str,
    *,
    workspace: Path,
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> list[dict[str, Any]]:
    """Return [{document_id, path, bytes, text}] for file/directory sources."""
    parsed = _parse_spec(spec)
    kind = parsed["kind"]
    if kind == "connector":
        raise KnowledgePathDenied("connector ingest requires a registered connector extra")
    root = _contained(parsed["path"], workspace)
    glob = parsed.get("glob") or "**/*"
    files: list[Path] = []
    if kind == "file" or root.is_file():
        files = [root]
        walk_root = root.parent
    else:
        walk_root = root
        files = _list_dir(root, glob=glob, max_depth=max_depth, max_files=max_files)
    total = 0
    out: list[dict[str, Any]] = []
    for path in files:
        if path.suffix.lower() in _ARCHIVE_SUFFIX:
            extracted = _safe_zip(path, workspace=workspace, max_file_bytes=max_file_bytes)
            for item in extracted:
                total += len(item["bytes"])
                if total > max_bytes:
                    raise KnowledgeWalkExceeded("bytes", total, max_bytes)
                out.append(item)
            continue
        data = _read_file(path, max_file_bytes=max_file_bytes)
        total += len(data)
        if total > max_bytes:
            raise KnowledgeWalkExceeded("bytes", total, max_bytes)
        if len(out) >= max_files:
            raise KnowledgeWalkExceeded("files", len(out) + 1, max_files)
        rel = _rel_id(path, walk_root)
        out.append(
            {
                "document_id": rel,
                "path": str(path),
                "bytes": data,
                "text": _decode(data, path),
            }
        )
    return out


def _parse_spec(spec: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(spec, str):
        return {"kind": "file", "path": spec, "glob": None}
    if not isinstance(spec, dict):
        raise KnowledgePathDenied("ingest source must be a path or {kind, path}")
    kind = str(spec.get("kind") or "file").strip().lower()
    if kind not in {"file", "directory", "connector"}:
        raise KnowledgePathDenied("ingest source.kind must be file, directory, or connector")
    path = spec.get("path") or spec.get("url") or spec.get("name")
    if not path:
        raise KnowledgePathDenied("ingest source requires path")
    return {"kind": kind, "path": str(path), "glob": spec.get("glob"), "name": spec.get("name")}


def _contained(path: str | Path, workspace: Path) -> Path:
    try:
        return resolve_within(path, workspace, must_exist=True, what="knowledge source")
    except PathError as exc:
        raise KnowledgePathDenied(str(exc)) from exc


def _list_dir(root: Path, *, glob: str, max_depth: int, max_files: int) -> list[Path]:
    found: list[Path] = []
    pattern = glob or "**/*"
    for path in sorted(root.glob(pattern)):
        if path.is_symlink():
            resolved = path.resolve()
            try:
                resolve_within(resolved, root.resolve(), what="knowledge symlink")
            except PathError as exc:
                raise KnowledgePathDenied(str(exc)) from exc
            if not resolved.is_file():
                continue
            path = resolved
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        depth = len(rel.parts)
        if depth > max_depth:
            raise KnowledgeWalkExceeded("depth", depth, max_depth)
        found.append(path)
        if len(found) > max_files:
            raise KnowledgeWalkExceeded("files", len(found), max_files)
    return found


def _read_file(path: Path, *, max_file_bytes: int) -> bytes:
    size = path.stat().st_size
    if size > max_file_bytes:
        raise KnowledgeWalkExceeded("file_bytes", int(size), max_file_bytes)
    return path.read_bytes()


def _decode(data: bytes, path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from readyagents.media.pdf import extract_with_pypdf, is_pdf, split_simple_pdf

        if is_pdf(data):
            pages = extract_with_pypdf(data, max_pages=50)
            if pages is None:
                pages = split_simple_pdf(data, max_pages=50)
            return "\n\n".join(
                f"[page {row['page']}] {row.get('text') or ''}".strip() for row in pages
            )
    return data.decode("utf-8", "replace")


def _rel_id(path: Path, root: Path) -> str:
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        rel = Path(path.name)
    token = str(rel).replace("\\", "/")
    return token.strip("/") or path.name


def _safe_zip(path: Path, *, workspace: Path, max_file_bytes: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                if name.endswith("/"):
                    continue
                if name.startswith("/") or name.startswith("\\"):
                    raise KnowledgeArchiveRefused(f"archive absolute path refused: {name}")
                parts = Path(name).parts
                if ".." in parts:
                    raise KnowledgeArchiveRefused(f"archive traversal refused: {name}")
                if info.file_size > max_file_bytes:
                    raise KnowledgeWalkExceeded("file_bytes", int(info.file_size), max_file_bytes)
                data = zf.read(info)
                out.append(
                    {
                        "document_id": f"{path.name}/{name}",
                        "path": f"{path}:{name}",
                        "bytes": data,
                        "text": data.decode("utf-8", "replace"),
                    }
                )
    except zipfile.BadZipFile as exc:
        raise KnowledgeArchiveRefused(f"malformed archive: {exc}") from exc
    _ = workspace
    return out
