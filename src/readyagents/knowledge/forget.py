"""Complete document erasure across chunks, vectors, and cite targets."""

from __future__ import annotations

from typing import Any

from readyagents.knowledge.cite import META_DOC
from readyagents.knowledge.ingest import _by_document


def forget_document(store: Any, *, scope: str, document_id: str) -> int:
    grouped = _by_document(store.list(scope=scope))
    recs = grouped.get(document_id) or []
    removed = 0
    for rec in recs:
        store.forget(record_id=rec.id)
        removed += 1
        leftover = getattr(store, "vector", None)
        if callable(leftover):
            leftover(rec.id)  # still None after forget
    # Second pass: any stray metadata match
    for rec in store.list(scope=scope):
        if str((rec.metadata or {}).get(META_DOC) or "") == document_id:
            store.forget(record_id=rec.id)
            removed += 1
    return removed
