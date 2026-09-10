"""Canonical SHA-256 digests (algorithm v1) for workflows, packs, and MCP surfaces.

Freeze: a digest is ``sha256:<hex>``. Version ``1`` is recorded in lockfiles.
Workflow hashing covers the parsed document plus every resolved include's
digest, in sorted path order. Pack hashing is the file bytes. MCP hashing
reuses ``firewall.mcp_pin.snapshot_tools`` — do not fork a second surface hash.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from readyagents.errors import TrustError

DIGEST_ALGORITHM = "sha256"
DIGEST_VERSION = 1
DIGEST_PREFIX = "sha256:"
KIND_WORKFLOW = "workflow"
KIND_INCLUDE = "include"
KIND_PACK = "pack"
KIND_MCP = "mcp_server"
_MAX_INCLUDE_DEPTH = 8


def prefixed(digest: str) -> str:
    text = str(digest).strip()
    if text.startswith(DIGEST_PREFIX):
        return text
    return f"{DIGEST_PREFIX}{text}"


def digest_bytes(data: bytes) -> str:
    return f"{DIGEST_PREFIX}{hashlib.sha256(data).hexdigest()}"


def canonical_dumps(value: Any) -> str:
    """Deterministic JSON: sorted keys, no ASCII escape, tight separators."""
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_canon_default,
    )


def digest_canonical(value: Any) -> str:
    return digest_bytes(canonical_dumps(value).encode("utf-8"))


def digest_pack_bytes(data: bytes) -> str:
    return digest_bytes(data)


def digest_mcp_surface(server: str, tools: Mapping[str, Any]) -> str:
    """Compose ``snapshot_tools``; a description-only change is a digest change."""
    from readyagents.firewall.mcp_pin import snapshot_tools

    snap = snapshot_tools(server, tools)
    return prefixed(snap.digest)


def digest_workflow(path: Path | str, *, root: Path | None = None) -> str:
    """SHA-256 of the resolved include graph. Changing an include changes this."""
    report = inspect_workflow(path, root=root)
    return report.digest


class IncludeEntry:
    __slots__ = ("path", "resolved", "digest")

    def __init__(self, path: str, resolved: Path, digest: str) -> None:
        self.path = path
        self.resolved = resolved
        self.digest = digest

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "digest": self.digest}


class WorkflowDigest:
    __slots__ = ("path", "digest", "kind", "document", "includes")

    def __init__(
        self,
        path: Path,
        digest: str,
        *,
        kind: str,
        document: dict[str, Any],
        includes: list[IncludeEntry],
    ) -> None:
        self.path = path
        self.digest = digest
        self.kind = kind
        self.document = document
        self.includes = includes

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": str(self.path),
            "digest": self.digest,
            "includes": [item.as_dict() for item in self.includes],
        }


def inspect_workflow(
    path: Path | str,
    *,
    root: Path | None = None,
    kind: str = KIND_WORKFLOW,
    _stack: tuple[Path, ...] = (),
    _top: Path | None = None,
) -> WorkflowDigest:
    file = Path(path)
    if not file.is_file():
        raise TrustError(
            f"workflow artifact not found: {file}",
            artifact=str(file),
            reason="missing",
        )
    try:
        resolved = file.resolve()
    except OSError as exc:
        raise TrustError(
            f"workflow artifact unreadable: {file}",
            artifact=str(file),
            reason="unreadable",
        ) from exc
    if resolved in _stack:
        cycle = " -> ".join(str(p) for p in (*_stack, resolved))
        raise TrustError(
            f"include cycle: {cycle}",
            artifact=str(resolved),
            reason="cycle",
        )
    if len(_stack) >= _MAX_INCLUDE_DEPTH:
        raise TrustError(
            f"include depth exceeded ({_MAX_INCLUDE_DEPTH}) at {resolved}",
            artifact=str(resolved),
            reason="depth",
        )
    document = _load_mapping(resolved)
    top = _top or resolved.parent
    parent_dir = resolved.parent
    include_root = root if root is not None else parent_dir
    entries: list[IncludeEntry] = []
    seen: set[Path] = set()
    for raw_path in _include_paths(document):
        child = _confine_include(raw_path, include_root, artifact=str(resolved))
        if child in seen:
            continue
        seen.add(child)
        nested = inspect_workflow(
            child,
            root=child.parent,
            kind=KIND_INCLUDE,
            _stack=_stack + (resolved,),
            _top=top,
        )
        rel = _relpath(child, top)
        entries.append(IncludeEntry(rel, child, nested.digest))
    entries.sort(key=lambda item: item.path)
    payload = {
        "algorithm": DIGEST_ALGORITHM,
        "version": DIGEST_VERSION,
        "kind": KIND_WORKFLOW,
        "document": document,
        "includes": [item.as_dict() for item in entries],
    }
    digest = digest_canonical(payload)
    return WorkflowDigest(resolved, digest, kind=kind, document=document, includes=entries)


def _load_mapping(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TrustError(
            f"artifact unreadable: {path}",
            artifact=str(path),
            reason="unreadable",
        ) from exc
    if text.startswith("\ufeff"):
        text = text[1:]
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TrustError(
                f"could not parse {path}: {exc}",
                artifact=str(path),
                reason="malformed",
            ) from exc
    else:
        import yaml

        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise TrustError(
                f"could not parse {path}: {exc}",
                artifact=str(path),
                reason="malformed",
            ) from exc
    if not isinstance(data, dict):
        raise TrustError(
            f"workflow {path} must be a mapping",
            artifact=str(path),
            reason="malformed",
        )
    return data


def _include_paths(document: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    for node in _iter_nodes(document):
        kind = str(node.get("type") or "").strip().lower()
        if kind != "include":
            continue
        raw = node.get("path")
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if not text or "{{" in text or "{%" in text:
            continue
        found.append(text)
    return found


def _iter_nodes(document: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    for node in document.get("nodes") or []:
        if isinstance(node, dict):
            yield from _iter_node_tree(node)


def _iter_node_tree(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield node
    for branch in node.get("branches") or []:
        if isinstance(branch, dict):
            yield from _iter_node_tree(branch)
    body = node.get("body")
    if isinstance(body, dict):
        yield from _iter_node_tree(body)


def _confine_include(raw_path: str, root: Path, *, artifact: str) -> Path:
    from readyagents.errors import PathError
    from readyagents.paths import resolve_within

    try:
        resolved = resolve_within(raw_path, root, what="included workflow")
    except PathError as extra:
        raise TrustError(
            f"include path escapes the parent workflow directory: {raw_path} (from {artifact})",
            artifact=artifact,
            reason="escape",
        ) from extra
    if not resolved.is_file():
        raise TrustError(
            f"included workflow not found: {raw_path} (from {artifact})",
            artifact=str(resolved),
            reason="missing",
        )
    return resolved


def _relpath(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _canon_default(value: Any) -> Any:
    if isinstance(value, datetime):
        stamp = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return stamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, bytes):
        return value.decode("utf-8", "surrogateescape")
    raise TypeError(f"not JSON-serializable: {type(value).__name__}")
