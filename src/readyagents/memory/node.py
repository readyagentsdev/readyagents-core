"""type: memory — declared scoped write/read/search/forget."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from readyagents.errors import MemoryError, PolicyDenied
from readyagents.firewall.enforce import ToolRequest, apply_decision, evaluate, quarantine_text
from readyagents.knowledge.cite import citation_from_record
from readyagents.memory.compaction import apply_compacted_items, compact_text
from readyagents.memory.protocol import (
    MAX_QUERY_CHARS,
    MemoryRecord,
    bound_limit,
    open_memory_store,
)
from readyagents.memory.retrieve import embed_texts, embedding_search, hybrid_search
from readyagents.memory.scope import validate_scope
from readyagents.replay.record import contains_secret
from readyagents.workflow.schema import NodeSpec
from readyagents.workflow.state import RunState, utc_now
from readyagents.workflow.templates import interpolate

_OPS = {"write", "read", "search", "forget"}


def run_memory_node(node: NodeSpec, state: RunState, ctx: Any) -> Any:
    op = str(node.op or "").strip().lower()
    if op not in _OPS:
        raise MemoryError(f"memory op must be write, read, search, or forget, not {op!r}")
    ns = state.mapping()
    rendered = interpolate(str(node.scope or ""), ns).strip()
    allowed = list(getattr(ctx.workflow, "memory_scopes", None) or [])
    scope = validate_scope(
        rendered,
        pattern=(node.scope_pattern or None),
        allowed=allowed or None,
        workflow_name=str(ctx.workflow.name),
    )
    if ctx.dry_run:
        return f"[dry-run] memory {op} {scope}"
    store = _store(ctx)
    try:
        if op == "write":
            return _write(node, state, ctx, store, scope, ns)
        if op == "read":
            return _read(node, state, ctx, store, scope)
        if op == "search":
            return _search(node, state, ctx, store, scope, ns)
        return _forget(node, state, ctx, store, scope)
    finally:
        closer = getattr(store, "close", None)
        if callable(closer):
            closer()


def _store(ctx: Any) -> Any:
    from readyagents.config import get_settings

    home = getattr(ctx, "pin_home", None)
    settings = get_settings()
    backend = str(getattr(settings, "memory_store", "json") or "json")
    if home is None:
        home = settings.home_path()
    return open_memory_store(home, backend=backend)


def _write(node: NodeSpec, state: RunState, ctx: Any, store: Any, scope: str, ns: dict) -> dict:
    raw = interpolate(str(node.text or node.source or node.prompt or ""), ns)
    redacted = _redact(ctx, raw)
    _refuse_secret(ctx, state, node, redacted)
    _policy(ctx, state, node, "memory.write", {"scope": scope, "text": redacted})
    compacted, compaction = compact_text(
        redacted, getattr(node, "context", None), llm=ctx.llm, node_id=node.id
    )
    expires = _expires(node.ttl)
    record = MemoryRecord(
        id=uuid4().hex,
        scope=scope,
        text=compacted,
        metadata={"node_id": node.id},
        created_at=utc_now(),
        expires_at=expires,
        source_run_id=state.run_id,
        provenance="workflow",
    )
    vector = None
    if bool(getattr(node, "embed", False)):
        vectors = embed_texts([compacted], settings=_settings())
        if vectors:
            vector = vectors[0]
            _account_embed(ctx, compacted)
    store.write(record, vector=vector)
    _audit(
        ctx, "memory_write", run_id=state.run_id, node_id=node.id, scope=scope, record_id=record.id
    )
    body: dict[str, Any] = {
        "id": record.id,
        "scope": scope,
        "bytes": len(compacted.encode("utf-8")),
    }
    if compaction:
        body["compaction"] = compaction
    return body


def _read(node: NodeSpec, state: RunState, ctx: Any, store: Any, scope: str) -> dict:
    _policy(ctx, state, node, "memory.read", {"scope": scope})
    limit = bound_limit(node.limit)
    records = store.read(scope, limit=limit)
    payload = [_public(rec, ctx) for rec in records]
    joined = "\n".join(str(item.get("text") or "") for item in payload)
    kept, compaction = compact_text(
        joined, getattr(node, "context", None), llm=ctx.llm, node_id=node.id
    )
    if compaction:
        payload = apply_compacted_items(payload, kept)
    _audit(
        ctx,
        "memory_read",
        run_id=state.run_id,
        node_id=node.id,
        scope=scope,
        count=len(payload),
    )
    body: dict[str, Any] = {"records": payload, "scope": scope}
    if compaction:
        body["compaction"] = compaction
    return body


def _search(node: NodeSpec, state: RunState, ctx: Any, store: Any, scope: str, ns: dict) -> dict:
    query = interpolate(str(node.query or node.prompt or ""), ns)
    if len(query) > MAX_QUERY_CHARS:
        query = query[:MAX_QUERY_CHARS]
    _policy(ctx, state, node, "memory.search", {"scope": scope, "query": query})
    limit = bound_limit(node.limit)
    retrieval = "keyword"
    note = None
    hits = store.search(scope, query, limit=limit)
    blend_spec = getattr(node, "blend", None) or {}
    blend_weights: dict[str, float] | None = None
    want_embed = bool(getattr(node, "embed", False)) or bool(blend_spec)
    if want_embed:
        embedded = embed_texts([query], settings=_settings())
        records = store.read(scope)
        vectors = {rec.id: store.vector(rec.id) for rec in records}
        vectors = {key: val for key, val in vectors.items() if val}
        if embedded and vectors:
            _account_embed(ctx, query)
            if blend_spec:
                hits, blend_weights = hybrid_search(
                    records,
                    query,
                    vectors=vectors,
                    query_vector=embedded[0],
                    bm25_weight=float(blend_spec.get("bm25") or 0.5),
                    embedding_weight=float(blend_spec.get("embedding") or 0.5),
                    limit=limit,
                )
                retrieval = "hybrid"
            else:
                hits = embedding_search(records, vectors, embedded[0], limit=limit)
                retrieval = "embedding"
        else:
            note = "embeddings unavailable; keyword search"
    freshness = getattr(node, "freshness", None)
    if freshness:
        from readyagents.knowledge.freshness import assert_fresh

        assert_fresh(store, scope, freshness)
    payload = []
    for hit in hits:
        row = _public(hit.record, ctx)
        row["score"] = hit.score
        payload.append(row)
    joined = "\n".join(str(item.get("text") or "") for item in payload)
    kept, compaction = compact_text(
        joined, getattr(node, "context", None), llm=ctx.llm, node_id=node.id
    )
    if compaction:
        payload = apply_compacted_items(payload, kept)
    _audit(
        ctx,
        "memory_search",
        run_id=state.run_id,
        node_id=node.id,
        scope=scope,
        count=len(payload),
        retrieval=retrieval,
    )
    body: dict[str, Any] = {"hits": payload, "scope": scope, "retrieval": retrieval}
    if blend_weights:
        body["blend"] = blend_weights
        meta = getattr(state, "metadata", None)
        if isinstance(meta, dict):
            meta["knowledge_blend"] = dict(blend_weights)
    if note:
        body["note"] = note
    if compaction:
        body["compaction"] = compaction
    return body


def _forget(node: NodeSpec, state: RunState, ctx: Any, store: Any, scope: str) -> dict:
    _policy(ctx, state, node, "memory.forget", {"scope": scope})
    removed = store.forget(scope=scope)
    _audit(
        ctx,
        "memory_forget",
        run_id=state.run_id,
        node_id=node.id,
        scope=scope,
        removed=removed,
    )
    return {"removed": removed, "scope": scope}


def _public(record: MemoryRecord, ctx: Any) -> dict[str, Any]:
    text = record.text
    policy = getattr(ctx, "policy", None)
    if policy is not None:
        _rule_id, rule = policy.tool_rule("memory.read")
        if rule is not None and rule.quarantine:
            text = quarantine_text(text)
    row = {
        "id": record.id,
        "scope": record.scope,
        "text": text,
        "created_at": record.created_at,
        "expires_at": record.expires_at,
    }
    cite = citation_from_record(record)
    if cite:
        row["citation"] = cite
        row["document_id"] = cite["document_id"]
    return row


def _policy(
    ctx: Any, state: RunState, node: NodeSpec, name: str, arguments: dict[str, Any]
) -> None:
    decision = evaluate(
        ToolRequest(name=name, arguments=arguments, node_id=node.id, raw_arguments=arguments),
        state,
        getattr(ctx, "policy", None),
    )
    apply_decision(decision, node_id=node.id, run_id=state.run_id)


def _redact(ctx: Any, text: str) -> str:
    redactor = getattr(ctx, "redactor", None)
    if redactor is None:
        return text
    method = getattr(redactor, "redact_text", None)
    if callable(method):
        return str(method(text))
    method = getattr(redactor, "redact", None)
    if callable(method):
        out = method(text)
        return str(out) if out is not None else text
    return text


def _refuse_secret(ctx: Any, state: RunState, node: NodeSpec, text: str) -> None:
    secrets = list(getattr(ctx, "cassette_secrets", None) or [])
    if not secrets:
        return
    if contains_secret(text, secrets):
        _audit(
            ctx,
            "memory_secret_refused",
            run_id=state.run_id,
            node_id=node.id,
            reason="known secret value",
        )
        raise PolicyDenied(
            node.id,
            "refusing to persist a known secret value in memory",
            rule="secrets.memory",
        )


def _expires(ttl: str | None) -> str | None:
    if not ttl:
        return None
    from readyagents.approvals.gate import parse_expires_in

    seconds = parse_expires_in(str(ttl))
    stamp = datetime.now(UTC) + timedelta(seconds=seconds)
    return stamp.isoformat()


def _audit(ctx: Any, event: str, **fields: Any) -> None:
    auditor = getattr(ctx, "auditor", None)
    if callable(auditor):
        auditor(event, **fields)


def _settings() -> Any:
    from readyagents.config import get_settings

    return get_settings()


def _account_embed(ctx: Any, text: str) -> None:
    meter = getattr(ctx, "spend_meter", None)
    if meter is None:
        return
    from readyagents.cost.tokens import heuristic_tokens

    tokens = heuristic_tokens(text)
    consult = getattr(meter, "consult_before_call", None)
    if callable(consult):
        consult("openai:text-embedding-3-small", prompt_tokens=tokens)
    record = getattr(meter, "record_usage", None)
    if callable(record):
        record("openai:text-embedding-3-small", {"prompt_tokens": tokens, "total_tokens": tokens})
