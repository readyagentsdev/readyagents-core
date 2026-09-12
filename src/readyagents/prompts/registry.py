"""Load, save, register, address, diff, and rollback prompt versions."""

from __future__ import annotations

import json
from difflib import unified_diff
from pathlib import Path
from typing import Any

from readyagents.atomic import atomic_write_text
from readyagents.errors import OptimizeRefused
from readyagents.prompts.layout import SCHEMA_REGISTRY, content_hash, sidecar_path
from readyagents.prompts.record import PromptObject, PromptRegistry, PromptVersion
from readyagents.workflow.runner import load_workflow
from readyagents.workflow.state import utc_now


def load_registry(workflow: Path | str) -> PromptRegistry:
    path = sidecar_path(workflow)
    if not path.is_file():
        return PromptRegistry(workflow=Path(workflow).name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OptimizeRefused(f"malformed prompt registry: {exc}", reason="registry") from exc
    if not isinstance(data, dict):
        raise OptimizeRefused("prompt registry must be a mapping", reason="registry")
    schema = data.get("schema")
    if schema and schema != SCHEMA_REGISTRY:
        raise OptimizeRefused(f"unknown prompt registry schema {schema!r}", reason="registry")
    return PromptRegistry.from_dict(data)


def save_registry(workflow: Path | str, registry: PromptRegistry) -> Path:
    path = sidecar_path(workflow)
    registry.workflow = Path(workflow).name
    registry.schema = SCHEMA_REGISTRY
    payload = json.dumps(registry.as_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    atomic_write_text(path, payload)
    return path


def register_literals(workflow: Path | str) -> PromptRegistry:
    """Auto-register literal ``prompt:`` fields. Never rewrites the workflow file."""
    source = Path(workflow)
    before = source.read_bytes()
    spec = load_workflow(source)
    registry = load_registry(source)
    for node in spec.nodes:
        text = node.prompt
        if not isinstance(text, str) or not text.strip():
            continue
        prompt_id = str(getattr(node, "prompt_id", None) or node.id)
        obj = registry.prompts.get(prompt_id)
        digest = content_hash(text)
        if obj is None:
            version = PromptVersion(
                version=1,
                content_hash=digest,
                text=text,
                source="literal",
                created_at=utc_now(),
            )
            registry.prompts[prompt_id] = PromptObject(
                id=prompt_id,
                node_id=node.id,
                active_version=1,
                versions=[version],
            )
            continue
        active = obj.active()
        if active is not None and active.content_hash == digest:
            continue
        if any(row.content_hash == digest for row in obj.versions):
            continue
        nxt = max((row.version for row in obj.versions), default=0) + 1
        obj.versions.append(
            PromptVersion(
                version=nxt,
                content_hash=digest,
                text=text,
                source="literal",
                created_at=utc_now(),
                parent=obj.active_version,
            )
        )
    save_registry(source, registry)
    after = source.read_bytes()
    if after != before:
        raise OptimizeRefused("register_literals rewrote the workflow file", reason="rewrite")
    return registry


def add_version(
    workflow: Path | str,
    prompt_id: str,
    text: str,
    *,
    source: str = "candidate",
    activate: bool = False,
    node_id: str | None = None,
) -> PromptVersion:
    registry = load_registry(workflow)
    obj = registry.prompts.get(prompt_id)
    digest = content_hash(text)
    if obj is None:
        version = PromptVersion(
            version=1,
            content_hash=digest,
            text=text,
            source=source,
            created_at=utc_now(),
        )
        registry.prompts[prompt_id] = PromptObject(
            id=prompt_id,
            node_id=node_id or prompt_id,
            active_version=1 if activate else 1,
            versions=[version],
        )
        save_registry(workflow, registry)
        return version
    existing = next((row for row in obj.versions if row.content_hash == digest), None)
    if existing is not None:
        if activate:
            obj.active_version = existing.version
            save_registry(workflow, registry)
        return existing
    nxt = max((row.version for row in obj.versions), default=0) + 1
    version = PromptVersion(
        version=nxt,
        content_hash=digest,
        text=text,
        source=source,
        created_at=utc_now(),
        parent=obj.active_version or None,
    )
    obj.versions.append(version)
    if activate:
        obj.active_version = version.version
    save_registry(workflow, registry)
    return version


def get_prompt(
    workflow: Path | str,
    prompt_id: str,
    *,
    version: int | None = None,
) -> PromptVersion:
    registry = load_registry(workflow)
    obj = registry.prompts.get(prompt_id)
    if obj is None:
        raise OptimizeRefused(f"prompt {prompt_id!r} not in registry", reason="missing")
    row = obj.version(version) if version is not None else obj.active()
    if row is None:
        raise OptimizeRefused(
            f"prompt {prompt_id!r} version {version!r} not found",
            reason="missing",
        )
    return row


def list_prompts(workflow: Path | str) -> list[dict[str, Any]]:
    registry = load_registry(workflow)
    rows: list[dict[str, Any]] = []
    for obj in registry.prompts.values():
        active = obj.active()
        rows.append(
            {
                "id": obj.id,
                "node_id": obj.node_id,
                "active_version": obj.active_version,
                "versions": len(obj.versions),
                "content_hash": active.content_hash if active else "",
            }
        )
    return rows


def history(workflow: Path | str, prompt_id: str) -> list[dict[str, Any]]:
    registry = load_registry(workflow)
    obj = registry.prompts.get(prompt_id)
    if obj is None:
        raise OptimizeRefused(f"prompt {prompt_id!r} not in registry", reason="missing")
    return [row.as_dict() for row in obj.versions]


def diff_versions(
    workflow: Path | str,
    prompt_id: str,
    *,
    left: int | None = None,
    right: int | None = None,
) -> str:
    registry = load_registry(workflow)
    obj = registry.prompts.get(prompt_id)
    if obj is None:
        raise OptimizeRefused(f"prompt {prompt_id!r} not in registry", reason="missing")
    if not obj.versions:
        raise OptimizeRefused(f"prompt {prompt_id!r} has no versions", reason="missing")
    if right is None:
        right = obj.active_version or obj.versions[-1].version
    if left is None:
        left = right - 1 if right > 1 else obj.versions[0].version
    a = obj.version(left)
    b = obj.version(right)
    if a is None or b is None:
        raise OptimizeRefused("diff needs two stored versions", reason="missing")
    lines = unified_diff(
        a.text.splitlines(keepends=True),
        b.text.splitlines(keepends=True),
        fromfile=f"{prompt_id}@{a.version}:{a.content_hash[:12]}",
        tofile=f"{prompt_id}@{b.version}:{b.content_hash[:12]}",
        lineterm="",
    )
    return "\n".join(lines)


def rollback(workflow: Path | str, prompt_id: str, *, version: int | None = None) -> PromptVersion:
    source = Path(workflow)
    before = source.read_bytes()
    registry = load_registry(source)
    obj = registry.prompts.get(prompt_id)
    if obj is None:
        raise OptimizeRefused(f"prompt {prompt_id!r} not in registry", reason="missing")
    if version is None:
        current = obj.active()
        if current is None or current.parent is None:
            prior = None
            for row in reversed(obj.versions):
                if row.version != obj.active_version:
                    prior = row
                    break
            if prior is None:
                raise OptimizeRefused("no previous version to restore", reason="rollback")
            version = prior.version
        else:
            version = current.parent
    row = obj.version(int(version))
    if row is None:
        raise OptimizeRefused(f"prompt {prompt_id!r} version {version} not found", reason="missing")
    obj.active_version = row.version
    save_registry(source, registry)
    after = source.read_bytes()
    if after != before:
        raise OptimizeRefused("rollback rewrote the workflow file", reason="rewrite")
    restored = get_prompt(source, prompt_id)
    if restored.content_hash != row.content_hash or restored.text != row.text:
        raise OptimizeRefused(
            "rollback did not restore the previous version exactly", reason="rollback"
        )
    return restored


def resolve_prompt(
    node: Any,
    *,
    workflow_dir: Path | str | None,
    workflow_name: str | None = None,
    source_path: Path | str | None = None,
    fallback: str | None = None,
) -> str:
    """Use a promoted sidecar version when present; otherwise the literal.

    Does not write. A workflow that never ran optimize has no sidecar and
    takes the existing engine path (the literal).
    """
    literal = fallback if fallback is not None else (getattr(node, "prompt", None) or "")
    if workflow_dir is None:
        return literal
    directory = Path(workflow_dir)
    seen: list[Path] = []
    stems: list[str] = []
    if source_path:
        stems.append(Path(source_path).stem)
    if workflow_name:
        stems.append(Path(str(workflow_name)).stem)
    for stem in stems:
        seen.append(directory / f"{stem}.prompts.json")
    prompt_id = str(getattr(node, "prompt_id", None) or getattr(node, "id", "") or "")
    pinned = getattr(node, "prompt_version", None)
    for path in seen:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        registry = PromptRegistry.from_dict(data)
        obj = registry.prompts.get(prompt_id)
        if obj is None:
            continue
        try:
            row = obj.version(int(pinned)) if pinned not in (None, "") else obj.active()
        except (TypeError, ValueError):
            row = obj.active()
        if row is not None:
            return row.text
    return literal
