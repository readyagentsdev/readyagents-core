"""Shipped example workflows (H-06).

The wheel carries ``examples/`` as package data (``readyagents/examples/``);
a source checkout resolves to the repo-root ``examples/`` instead, so the same
code serves ``pip install`` users, editable installs, and the test suite.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

import readyagents
from readyagents.errors import ConfigError, PathError, TrustError
from readyagents.paths import resolve_within

# Preferred suffix when a stem matches several files (calc_pipeline.yaml/.json).
_SUFFIX_PREFERENCE = (".yaml", ".yml", ".json")

_SKIP_NAMES = {"__pycache__", ".DS_Store"}

# Aligned with workflow loader / trust digest include expansion.
_MAX_INCLUDE_DEPTH = 8


def examples_root() -> Traversable | Path:
    """Directory holding the shipped examples, packaged or repo-root."""
    packaged = resources.files("readyagents") / "examples"
    try:
        if packaged.is_dir():
            return packaged
    except (FileNotFoundError, NotADirectoryError, OSError):
        pass
    fallback = Path(readyagents.__file__).resolve().parent.parent.parent / "examples"
    if fallback.is_dir():
        return fallback
    raise ConfigError("shipped examples are not available in this install")


@contextmanager
def _examples_dir() -> Iterator[Path]:
    """Yield a real filesystem path to the shipped examples root."""
    root = examples_root()
    if isinstance(root, Path):
        yield root
        return
    with resources.as_file(root) as path:
        yield Path(path)


def _walk_files(root: Traversable | Path) -> list[str]:
    """Sorted posix-style relative paths of every example file."""
    found: list[str] = []

    def visit(node: Traversable | Path, prefix: str) -> None:
        for entry in sorted(node.iterdir(), key=lambda e: e.name):
            if entry.name in _SKIP_NAMES or entry.name.endswith((".pyc", ".pyo")):
                continue
            rel = f"{prefix}{entry.name}"
            if entry.is_dir():
                visit(entry, rel + "/")
            elif entry.is_file():
                found.append(rel)

    visit(root, "")
    return found


def list_examples() -> list[str]:
    """Relative paths of all shipped examples, sorted."""
    return _walk_files(examples_root())


def resolve_example(name: str) -> tuple[str, bytes]:
    """Resolve a name to (relative path, content).

    Accepts an exact relative path (``bench/suite.yaml``) or a bare stem
    (``calc_pipeline``). A stem matching several suffixes prefers YAML, then
    JSON. Anything else is an error listing the candidates.
    """
    relpaths = list_examples()
    if name in relpaths:
        return name, _read(name)
    stem_matches = sorted({p for p in relpaths if Path(p).stem == name or Path(p).name == name})
    if not stem_matches:
        raise ConfigError(
            f"Unknown example '{name}'. Run `readyagents new --list-examples` to list them."
        )
    if len(stem_matches) == 1:
        return stem_matches[0], _read(stem_matches[0])
    for suffix in _SUFFIX_PREFERENCE:
        for candidate in stem_matches:
            if candidate.endswith(suffix):
                return candidate, _read(candidate)
    raise ConfigError(f"Ambiguous example '{name}': {', '.join(stem_matches)}. Give the full path.")


def _read(relpath: str) -> bytes:
    node: Traversable | Path = examples_root()
    for part in relpath.split("/"):
        node = node.joinpath(part)  # type: ignore[assignment]
    with resources.as_file(node) as path:
        return Path(path).read_bytes()


def materialize_example(name: str, dest: Path) -> list[Path]:
    """Copy an example into ``dest`` as ``workflow.<suffix>`` plus a schema file.

    Recursively copies ``type: include`` ``path:`` children beside the parent,
    keeping relative names. Mirrors ``create_project``: refuses to overwrite
    existing files. Cycle, path-escape, and depth guards match the loader.
    """
    from readyagents.workflow.jsonschema import workflow_json_schema_text

    relpath, content = resolve_example(name)
    suffix = Path(relpath).suffix or ".yaml"
    dest = dest.expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    workflow = dest / f"workflow{suffix}"
    schema_file = dest / "workflow.schema.json"
    for path in (workflow, schema_file):
        if path.exists():
            raise ConfigError(f"Refusing to overwrite existing file: {path}")
    workflow.write_bytes(content)
    schema_file.write_text(workflow_json_schema_text(), encoding="utf-8", newline="\n")
    written = [workflow, schema_file]
    with _examples_dir() as examples_fs:
        written.extend(
            copy_include_tree(
                parent_rel=relpath,
                parent_content=content,
                examples_root=examples_fs,
                dest=dest,
            )
        )
    return written


def copy_include_tree(
    *,
    parent_rel: str,
    parent_content: bytes,
    examples_root: Path,
    dest: Path,
) -> list[Path]:
    """Copy include children for ``parent_rel`` under ``dest``.

    ``examples_root`` is the shipped examples directory (or a test fixture root).
    ``parent_rel`` is posix-relative to that root. Public for unit tests.
    """
    examples_root = examples_root.expanduser().resolve()
    dest = dest.expanduser().resolve()
    top_dir = (examples_root / parent_rel).resolve().parent
    written: list[Path] = []
    seen: set[Path] = set()
    _copy_includes_from(
        content=parent_content,
        source_rel=parent_rel.replace("\\", "/"),
        source_suffix=Path(parent_rel).suffix or ".yaml",
        examples_root=examples_root,
        top_dir=top_dir,
        dest=dest,
        stack=(),
        seen=seen,
        written=written,
    )
    return written


def _copy_includes_from(
    *,
    content: bytes,
    source_rel: str,
    source_suffix: str,
    examples_root: Path,
    top_dir: Path,
    dest: Path,
    stack: tuple[Path, ...],
    seen: set[Path],
    written: list[Path],
) -> None:
    source_path = (examples_root / source_rel).resolve()
    if source_path in stack:
        cycle = " -> ".join(str(p) for p in (*stack, source_path))
        raise TrustError(
            f"include cycle while materializing example: {cycle}",
            artifact=str(source_path),
            reason="cycle",
        )
    if len(stack) >= _MAX_INCLUDE_DEPTH:
        raise TrustError(
            f"include depth exceeded ({_MAX_INCLUDE_DEPTH}) while materializing "
            f"example at {source_path}",
            artifact=str(source_path),
            reason="depth",
        )
    parent_dir = source_path.parent
    document = _parse_document(content, source_suffix, artifact=str(source_path))
    for raw_path in _include_paths(document):
        child = _confine_include(raw_path, parent_dir, artifact=str(source_path))
        try:
            child_rel = child.relative_to(examples_root).as_posix()
        except ValueError as exc:
            raise TrustError(
                f"include path escapes the example root: {raw_path} (from {source_path})",
                artifact=str(child),
                reason="escape",
            ) from exc
        try:
            dest_child = _dest_beside_parent(child, top_dir=top_dir, dest=dest)
        except ValueError as exc:
            raise TrustError(
                f"include path escapes the example root: {raw_path} (from {source_path})",
                artifact=str(child),
                reason="escape",
            ) from exc
        if child in seen:
            continue
        seen.add(child)
        if dest_child.exists():
            raise ConfigError(f"Refusing to overwrite existing file: {dest_child}")
        dest_child.parent.mkdir(parents=True, exist_ok=True)
        child_bytes = child.read_bytes()
        dest_child.write_bytes(child_bytes)
        written.append(dest_child)
        _copy_includes_from(
            content=child_bytes,
            source_rel=child_rel,
            source_suffix=child.suffix or ".yaml",
            examples_root=examples_root,
            top_dir=top_dir,
            dest=dest,
            stack=stack + (source_path,),
            seen=seen,
            written=written,
        )


def _dest_beside_parent(child: Path, *, top_dir: Path, dest: Path) -> Path:
    """Map a confined child path under the example top dir onto ``dest``."""
    rel = child.resolve().relative_to(top_dir.resolve())
    return (dest / rel).resolve()


def _confine_include(raw_path: str, root: Path, *, artifact: str) -> Path:
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


def _parse_document(content: bytes, suffix: str, *, artifact: str) -> Mapping[str, Any]:
    text = content.decode("utf-8")
    if text.startswith("\ufeff"):
        text = text[1:]
    if suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as extra:
            raise ConfigError(f"could not parse example {artifact}: {extra}") from extra
    else:
        import yaml

        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as extra:
            raise ConfigError(f"could not parse example {artifact}: {extra}") from extra
    if not isinstance(data, dict):
        raise ConfigError(f"example {artifact} must be a mapping")
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
        if not text:
            continue
        if "{{" in text or "{%" in text:
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
