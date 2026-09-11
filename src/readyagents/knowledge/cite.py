"""Structured citations on the existing memory record. Scope-gated resolve."""

from __future__ import annotations

from typing import Any

from readyagents.errors import KnowledgeCiteDenied, KnowledgeError
from readyagents.memory.protocol import MemoryRecord
from readyagents.memory.scope import validate_scope

META_DOC = "document_id"
META_VERSION = "document_version"
META_HASH = "content_hash"
META_START = "range_start"
META_END = "range_end"
META_KIND = "range_kind"
META_HEADING = "heading_path"
META_SOURCE = "source_path"
META_INDEX = "chunk_index"
META_STRATEGY = "chunk_strategy"
META_INGESTED = "ingested_at"


def citation_from_record(record: MemoryRecord) -> dict[str, Any] | None:
    meta = dict(record.metadata or {})
    doc = str(meta.get(META_DOC) or "")
    if not doc:
        return None
    start = meta.get(META_START)
    end = meta.get(META_END)
    return {
        "document_id": doc,
        "version": str(meta.get(META_VERSION) or ""),
        "range": {
            "kind": str(meta.get(META_KIND) or "bytes"),
            "start": int(start) if start is not None and str(start) != "" else 0,
            "end": int(end) if end is not None and str(end) != "" else 0,
        },
        "heading_path": str(meta.get(META_HEADING) or ""),
        "record_id": record.id,
        "scope": record.scope,
    }


def parse_citation(raw: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw, dict):
        doc = str(raw.get("document_id") or raw.get("doc") or "").strip()
        if not doc:
            raise KnowledgeError("citation requires document_id")
        rng = raw.get("range") if isinstance(raw.get("range"), dict) else {}
        return {
            "document_id": doc,
            "version": str(raw.get("version") or raw.get("document_version") or ""),
            "range": {
                "kind": str(rng.get("kind") or raw.get("range_kind") or "bytes"),
                "start": int(rng.get("start") or raw.get("start") or 0),
                "end": int(rng.get("end") or raw.get("end") or 0),
            },
            "heading_path": str(raw.get("heading_path") or ""),
            "record_id": str(raw.get("record_id") or ""),
            "scope": str(raw.get("scope") or ""),
        }
    text = str(raw or "").strip()
    if not text:
        raise KnowledgeError("citation is empty")
    try:
        import json

        if text.startswith("{"):
            return parse_citation(json.loads(text))
    except (TypeError, ValueError):
        pass
    # compact: document_id@version#bytes:start-end
    doc = text
    version = ""
    kind = "bytes"
    start = 0
    end = 0
    if "@" in doc:
        doc, rest = doc.split("@", 1)
        version = rest
        if "#" in version:
            version, span = version.split("#", 1)
            if ":" in span:
                kind, nums = span.split(":", 1)
                if "-" in nums:
                    a, b = nums.split("-", 1)
                    start, end = int(a or 0), int(b or 0)
    return {
        "document_id": doc,
        "version": version,
        "range": {"kind": kind, "start": start, "end": end},
        "heading_path": "",
        "record_id": "",
        "scope": "",
    }


def resolve_citation(
    store: Any,
    citation: str | dict[str, Any],
    *,
    scope: str,
    allowed: list[str] | None = None,
    workflow_name: str | None = None,
) -> dict[str, Any]:
    """Return the exact source span. Same scope gate as retrieval."""
    validate_scope(scope, allowed=allowed, workflow_name=workflow_name)
    spec = parse_citation(citation)
    want_scope = str(spec.get("scope") or scope)
    if want_scope != scope:
        raise KnowledgeCiteDenied(f"citation scope {want_scope!r} is outside {scope!r}")
    records = store.list(scope=scope)
    hits = [
        rec
        for rec in records
        if str((rec.metadata or {}).get(META_DOC) or "") == spec["document_id"]
    ]
    if spec.get("version"):
        versioned = [
            rec
            for rec in hits
            if str((rec.metadata or {}).get(META_VERSION) or "") == spec["version"]
        ]
        if versioned:
            hits = versioned
    if spec.get("record_id"):
        hits = [rec for rec in hits if rec.id == spec["record_id"]]
    rng = spec.get("range") or {}
    start = int(rng.get("start") or 0)
    end = int(rng.get("end") or 0)
    if start or end:
        ranged = []
        for rec in hits:
            meta = rec.metadata or {}
            rs = int(meta.get(META_START) or 0)
            re = int(meta.get(META_END) or 0)
            if rs == start and re == end:
                ranged.append(rec)
        if ranged:
            hits = ranged
    if not hits:
        raise KnowledgeCiteDenied(
            f"citation {spec['document_id']!r} is not retrievable in scope {scope!r}"
        )
    record = hits[0]
    text = record.text
    kind = str(rng.get("kind") or "bytes")
    if kind == "bytes" and (start or end) and end > start:
        raw = text.encode("utf-8")
        # Prefer the record's own stored span when it matches; else slice the chunk.
        meta = record.metadata or {}
        rs = int(meta.get(META_START) or 0)
        if rs == start and int(meta.get(META_END) or 0) == end:
            span = text
        else:
            rel_start = max(0, start - rs)
            rel_end = rel_start + (end - start)
            span = raw[rel_start:rel_end].decode("utf-8", "replace")
    else:
        span = text
    return {
        "text": span,
        "citation": citation_from_record(record),
        "scope": scope,
    }


def collect_retrieved_citations(mapping: dict[str, Any]) -> list[str]:
    """Document ids from search/ingest hits in run state. Used by require_citation."""
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            cite = value.get("citation")
            if isinstance(cite, dict) and cite.get("document_id"):
                doc = str(cite["document_id"])
                if doc not in found:
                    found.append(doc)
            doc = value.get("document_id") or (value.get("metadata") or {}).get("document_id")
            if doc and str(doc) not in found and "text" in value:
                found.append(str(doc))
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(mapping)
    return found
