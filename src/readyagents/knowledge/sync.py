"""Foreground knowledge sync. Content-hash identity, not a watcher."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from readyagents.errors import KnowledgeError
from readyagents.knowledge.forget import forget_document
from readyagents.knowledge.ingest import _by_document, ingest_into
from readyagents.knowledge.walk import walk_source
from readyagents.memory.protocol import open_memory_store
from readyagents.memory.scope import validate_scope


def sync_workflow(
    workflow: Any,
    *,
    home: Path,
    workspace: Path,
    node_id: str | None = None,
    apply_removed: bool = True,
    backend: str = "json",
) -> dict[str, Any]:
    nodes = [n for n in workflow.nodes if str(n.type) == "ingest"]
    if node_id:
        nodes = [n for n in nodes if n.id == node_id]
        if not nodes:
            raise KnowledgeError(f"no ingest node {node_id!r}")
    if not nodes:
        raise KnowledgeError("workflow has no type: ingest node")
    store = open_memory_store(home, backend=backend)
    reports: list[dict[str, Any]] = []
    try:
        for node in nodes:
            scope = validate_scope(
                str(node.scope or ""),
                pattern=node.scope_pattern or None,
                allowed=list(getattr(workflow, "memory_scopes", None) or []) or None,
                workflow_name=str(workflow.name),
            )
            chunk_spec = dict(getattr(node, "chunk", None) or {})
            report = ingest_into(
                store,
                source=node.source,
                scope=scope,
                strategy=str(chunk_spec.get("strategy") or "paragraph"),
                max_chars=int(chunk_spec.get("max_chars") or 1200),
                overlap=int(chunk_spec.get("overlap") or 0),
                on_change=str(getattr(node, "on_change", None) or "supersede"),
                workspace=workspace,
                node_id=node.id,
            )
            if apply_removed and report.get("removed"):
                seen = _walk_ids(node.source, workspace)
                existing = _by_document(store.list(scope=scope))
                forgotten = 0
                for doc_id in list(existing):
                    if doc_id not in seen:
                        forgotten += forget_document(store, scope=scope, document_id=doc_id)
                report["forgotten"] = forgotten
            reports.append(report)
    finally:
        store.close()
    totals = {"added": 0, "updated": 0, "unchanged": 0, "removed": 0}
    for row in reports:
        for key in totals:
            totals[key] += int(row.get(key) or 0)
    return {"nodes": reports, **totals}


def _walk_ids(source: Any, workspace: Path) -> set[str]:
    try:
        files = walk_source(source, workspace=workspace)
    except Exception:  # noqa: BLE001
        return set()
    return {str(item["document_id"]) for item in files}
