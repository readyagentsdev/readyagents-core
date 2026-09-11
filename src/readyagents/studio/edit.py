"""Conservative YAML field edit: comments and key order stay; disk is source of truth."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

from readyagents.atomic import atomic_write_text
from readyagents.errors import ConfigError, PathError, WorkflowError
from readyagents.paths import resolve_within
from readyagents.workflow.runner import confine_under, load_workflow

_FORBIDDEN_PARTS = frozenset({".keys", ".grok", ".git"})
_FORBIDDEN_NAMES = frozenset({".env", ".env-ai", ".env.example"})
_WORKFLOW_SUFFIX = frozenset({".yaml", ".yml", ".json"})


class ExternalChange(ConfigError):
    """The file on disk changed while the studio had it open."""


class ReadOnlyError(ConfigError):
    """Studio was started with --read-only; writes are refused server-side."""


def assert_workflow_path(path: Path, workspace: Path) -> Path:
    """Confine to the workspace. Never open policy, secrets, or packs."""
    resolved = confine_under(path, workspace, what="workflow")
    parts = {part.lower() for part in resolved.parts}
    if parts & _FORBIDDEN_PARTS:
        raise PathError(f"Studio cannot open '{path}'")
    name = resolved.name.lower()
    if name in _FORBIDDEN_NAMES or name.startswith(".env"):
        raise PathError(f"Studio cannot open secrets file '{path}'")
    if "policy" in name and resolved.suffix.lower() in {".yaml", ".yml"}:
        raise PathError("Studio never edits policy files")
    if resolved.suffix.lower() == ".py":
        raise PathError("Studio never edits packs")
    if resolved.suffix.lower() not in _WORKFLOW_SUFFIX:
        raise ConfigError("Studio only opens workflow YAML or JSON")
    return resolved


def file_fingerprint(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    stat = path.stat()
    return {
        "path": str(path),
        "mtime": stat.st_mtime,
        "size": stat.st_size,
        "digest": hashlib.sha256(data).hexdigest(),
    }


def refuse_if_changed(path: Path, expected: dict[str, Any] | None) -> None:
    if not expected:
        return
    current = file_fingerprint(path)
    if current["digest"] != expected.get("digest"):
        raise ExternalChange(
            f"Refusing to overwrite '{path}': the file changed on disk while open."
        )
    expected_mtime = expected.get("mtime")
    if expected_mtime is not None and float(expected_mtime) != current["mtime"]:
        raise ExternalChange(
            f"Refusing to overwrite '{path}': the file changed on disk while open."
        )


def apply_field_edit(source: str, node_id: str, field: str, value: Any) -> str:
    """Replace one node field in the original YAML text. Comments elsewhere stay."""
    if field in {"id"}:
        raise ConfigError("Studio cannot rename node ids")
    if source.lstrip().startswith("{") or source.lstrip().startswith("["):
        raise ConfigError("Studio YAML edit requires a YAML workflow, not JSON")
    root = yaml.compose(source, Loader=yaml.SafeLoader)
    if root is None:
        raise WorkflowError("Empty workflow")
    mapping = _find_node_mapping(root, node_id)
    if mapping is None:
        raise ConfigError(f"Unknown node '{node_id}'")
    encoded = _yaml_scalar(value)
    for key_node, value_node in mapping.value:
        key = key_node.value if isinstance(key_node, ScalarNode) else None
        if key != field:
            continue
        start = int(value_node.start_mark.index)
        end = int(value_node.end_mark.index)
        return source[:start] + encoded + source[end:]
    last = mapping.value[-1] if mapping.value else None
    if last is None:
        raise ConfigError(f"Node '{node_id}' has no fields to edit")
    _key_node, value_node = last
    insert_at = int(value_node.end_mark.index)
    indent = " " * int(mapping.start_mark.column + 2)
    snippet = f"\n{indent}{field}: {encoded}"
    return source[:insert_at] + snippet + source[insert_at:]


def save_field_edit(
    path: Path,
    *,
    node_id: str,
    field: str,
    value: Any,
    expected: dict[str, Any] | None,
    workspace: Path,
) -> dict[str, Any]:
    target = assert_workflow_path(path, workspace)
    if not target.is_file():
        raise ConfigError(f"Workflow file not found: {target}")
    refuse_if_changed(target, expected)
    original = target.read_text(encoding="utf-8")
    patched = apply_field_edit(original, node_id, field, value)
    try:
        load_workflow(target, source=patched, display_path=str(path))
    except WorkflowError:
        raise
    atomic_write_text(target, patched, encoding="utf-8", newline="\n")
    return {
        "path": str(target),
        "node_id": node_id,
        "field": field,
        "fingerprint": file_fingerprint(target),
        "diff": _bounded_diff(original, patched),
    }


def validate_source(path: Path, source: str) -> dict[str, Any]:
    from readyagents.workflow.source_map import problem_to_json

    try:
        spec = load_workflow(path, source=source, display_path=str(path))
        return {"ok": True, "error": None, "message": "", "problems": [], "name": spec.name}
    except WorkflowError as exc:
        problems = [problem_to_json(item) for item in (exc.problems or [])]
        return {
            "ok": False,
            "error": type(exc).__name__,
            "message": str(exc),
            "problems": problems,
            "name": None,
        }


def _find_node_mapping(root: Any, node_id: str) -> MappingNode | None:
    found = _search_mapping(root, node_id)
    return found


def _search_mapping(node: Any, node_id: str) -> MappingNode | None:
    if isinstance(node, MappingNode):
        ident = None
        for key_node, value_node in node.value:
            key = key_node.value if isinstance(key_node, ScalarNode) else None
            if key == "id" and isinstance(value_node, ScalarNode):
                ident = value_node.value
        if ident == node_id:
            return node
        for _key_node, value_node in node.value:
            hit = _search_mapping(value_node, node_id)
            if hit is not None:
                return hit
    elif isinstance(node, SequenceNode):
        for item in node.value:
            hit = _search_mapping(item, node_id)
            if hit is not None:
                return hit
    return None


def _yaml_scalar(value: Any) -> str:
    dumped = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True).strip()
    if dumped.endswith("\n..."):
        dumped = dumped[: -len("\n...")].strip()
    if dumped == "..." or dumped == "---":
        dumped = yaml.safe_dump(value, default_flow_style=False, allow_unicode=True).strip()
    return dumped


def _bounded_diff(before: str, after: str, limit: int = 4000) -> str:
    import difflib

    lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            lineterm="",
            n=3,
        )
    )
    text = "\n".join(lines)
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def confined_out_dir(raw: str, workspace: Path) -> Path:
    return resolve_within(raw, workspace, what="studio output")
