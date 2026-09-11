"""type: ingest — file/directory sources into the shipped memory store."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import uuid4

from readyagents.errors import KnowledgeError
from readyagents.knowledge.chunk import chunk_text
from readyagents.knowledge.cite import (
    META_DOC,
    META_END,
    META_HASH,
    META_HEADING,
    META_INDEX,
    META_INGESTED,
    META_KIND,
    META_SOURCE,
    META_START,
    META_STRATEGY,
    META_VERSION,
    citation_from_record,
)
from readyagents.knowledge.walk import walk_source
from readyagents.memory.node import _policy, _store
from readyagents.memory.protocol import MemoryRecord
from readyagents.memory.scope import validate_scope
from readyagents.workflow.state import utc_now
from readyagents.workflow.templates import interpolate


def run_ingest_node(node: Any, state: Any, ctx: Any) -> Any:
    ns = state.mapping()
    rendered = interpolate(str(node.scope or ""), ns).strip()
    allowed = list(getattr(ctx.workflow, "memory_scopes", None) or [])
    scope = validate_scope(
        rendered,
        pattern=(node.scope_pattern or None),
        allowed=allowed or None,
        workflow_name=str(ctx.workflow.name),
    )
    source = node.source
    if isinstance(source, str):
        source = interpolate(source, ns)
    elif isinstance(source, dict):
        source = {
            key: interpolate(val, ns) if isinstance(val, str) else val
            for key, val in source.items()
        }
    chunk_spec = dict(getattr(node, "chunk", None) or {})
    strategy = str(chunk_spec.get("strategy") or "paragraph").strip().lower().replace("-", "_")
    max_chars = int(chunk_spec.get("max_chars") or 1200)
    overlap = int(chunk_spec.get("overlap") or 0)
    on_change = str(getattr(node, "on_change", None) or "supersede").strip().lower()
    if on_change not in {"supersede", "keep_versions"}:
        raise KnowledgeError(f"on_change must be supersede or keep_versions, not {on_change!r}")
    if ctx.dry_run:
        return {"dry_run": True, "scope": scope, "strategy": strategy}
    workspace = Path(getattr(ctx, "workflow_dir", None) or Path.cwd())
    _policy(ctx, state, node, "memory.write", {"scope": scope, "op": "ingest"})
    store = _store(ctx)
    try:
        report = ingest_into(
            store,
            source=source,
            scope=scope,
            strategy=strategy,
            max_chars=max_chars,
            overlap=overlap,
            on_change=on_change,
            workspace=workspace,
            run_id=state.run_id,
            node_id=node.id,
        )
        _maybe_freshness(node, store, scope)
        return report
    finally:
        closer = getattr(store, "close", None)
        if callable(closer):
            closer()


def ingest_into(
    store: Any,
    *,
    source: dict[str, Any] | str,
    scope: str,
    strategy: str,
    max_chars: int,
    overlap: int,
    on_change: str,
    workspace: Path,
    run_id: str = "",
    node_id: str = "",
    max_files: int | None = None,
    max_bytes: int | None = None,
    max_depth: int | None = None,
) -> dict[str, Any]:
    files = walk_source(
        source,
        workspace=workspace,
        max_files=int(max_files or 100),
        max_bytes=int(max_bytes or 10_000_000),
        max_depth=int(max_depth or 8),
    )
    existing = _by_document(store.list(scope=scope))
    seen: set[str] = set()
    added = updated = unchanged = 0
    written: list[dict[str, Any]] = []
    now = utc_now()
    for item in files:
        doc_id = str(item["document_id"])
        seen.add(doc_id)
        digest = hashlib.sha256(item["bytes"]).hexdigest()
        current = existing.get(doc_id) or []
        live_hash = None
        if current:
            live_hash = str((current[0].metadata or {}).get(META_HASH) or "")
        if live_hash == digest:
            unchanged += 1
            continue
        chunks = chunk_text(item["text"], strategy=strategy, max_chars=max_chars, overlap=overlap)
        if on_change == "supersede" and current:
            for rec in current:
                store.forget(record_id=rec.id)
            updated += 1
        elif current:
            updated += 1
        else:
            added += 1
        for index, chunk in enumerate(chunks):
            record = MemoryRecord(
                id=uuid4().hex,
                scope=scope,
                text=chunk.text,
                metadata={
                    META_DOC: doc_id,
                    META_VERSION: digest,
                    META_HASH: digest,
                    META_START: chunk.start,
                    META_END: chunk.end,
                    META_KIND: chunk.range_kind,
                    META_HEADING: chunk.heading_path,
                    META_SOURCE: item["path"],
                    META_INDEX: index,
                    META_STRATEGY: strategy,
                    META_INGESTED: now,
                    "node_id": node_id,
                },
                created_at=now,
                source_run_id=run_id,
                provenance="ingest",
            )
            store.write(record)
            cite = citation_from_record(record)
            if cite:
                written.append(cite)
    removed = 0
    for doc_id, _recs in existing.items():
        if doc_id in seen:
            continue
        removed += 1
        # Detectable; do not auto-delete on ingest node (sync --apply forgets).
    return {
        "scope": scope,
        "added": added,
        "updated": updated,
        "unchanged": unchanged,
        "removed": removed,
        "chunks": len(written),
        "citations": written[:50],
        "on_change": on_change,
        "strategy": strategy,
    }


def _by_document(records: list[MemoryRecord]) -> dict[str, list[MemoryRecord]]:
    grouped: dict[str, list[MemoryRecord]] = {}
    for rec in records:
        doc = str((rec.metadata or {}).get(META_DOC) or "")
        if not doc:
            continue
        grouped.setdefault(doc, []).append(rec)
    for recs in grouped.values():
        recs.sort(
            key=lambda r: str((r.metadata or {}).get(META_INGESTED) or r.created_at), reverse=True
        )
    return grouped


def _maybe_freshness(node: Any, store: Any, scope: str) -> None:
    from readyagents.knowledge.freshness import assert_fresh

    spec = getattr(node, "freshness", None)
    if not spec:
        return
    assert_fresh(store, scope, spec)
