"""Stdlib BM25 ranking and optional local vectors. No vector database."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence

from readyagents.memory.protocol import MAX_QUERY_CHARS, MemoryHit, MemoryRecord, bound_limit

_TOKEN = re.compile(r"[0-9A-Za-z_]+", re.UNICODE)
K1 = 1.5
B = 0.75


def tokenize(text: str) -> list[str]:
    return [tok.lower() for tok in _TOKEN.findall(text or "")]


def bm25_search(
    records: Sequence[MemoryRecord],
    query: str,
    *,
    limit: int = 5,
) -> list[MemoryHit]:
    """Okapi BM25: IDF + saturating TF + length normalization. Deterministic."""
    q = (query or "").strip()
    if len(q) > MAX_QUERY_CHARS:
        q = q[:MAX_QUERY_CHARS]
    q_tokens = tokenize(q)
    if not q_tokens or not records:
        return []
    docs = [tokenize(item.text) for item in records]
    n_docs = len(docs)
    avgdl = sum(len(doc) or 1 for doc in docs) / n_docs
    df: Counter[str] = Counter()
    for doc in docs:
        df.update(set(doc))
    idf = {
        term: math.log((n_docs - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5) + 1.0)
        for term in set(q_tokens)
    }
    scored: list[tuple[float, str, MemoryRecord]] = []
    q_counts = Counter(q_tokens)
    for record, doc in zip(records, docs, strict=True):
        tf = Counter(doc)
        dl = len(doc) or 1
        score = 0.0
        for term, qf in q_counts.items():
            freq = tf.get(term, 0)
            if freq <= 0:
                continue
            denom = freq + K1 * (1.0 - B + B * (dl / avgdl))
            tf_sat = (freq * (K1 + 1.0)) / denom
            score += idf.get(term, 0.0) * tf_sat * qf
        if score > 0:
            scored.append((score, record.id, record))
    scored.sort(key=lambda row: (-row[0], row[1]))
    cap = bound_limit(limit)
    return [MemoryHit(record=row[2], score=row[0]) for row in scored[:cap]]


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    a = 0.0
    b = 0.0
    for x, y in zip(left, right, strict=True):
        dot += x * y
        a += x * x
        b += y * y
    if a <= 0 or b <= 0:
        return 0.0
    return dot / math.sqrt(a * b)


def embedding_search(
    records: Sequence[MemoryRecord],
    vectors: dict[str, list[float]],
    query_vector: Sequence[float],
    *,
    limit: int = 5,
) -> list[MemoryHit]:
    scored: list[tuple[float, str, MemoryRecord]] = []
    for record in records:
        vec = vectors.get(record.id)
        if not vec:
            continue
        score = cosine(query_vector, vec)
        if score > 0:
            scored.append((score, record.id, record))
    scored.sort(key=lambda row: (-row[0], row[1]))
    cap = bound_limit(limit)
    return [MemoryHit(record=row[2], score=row[0]) for row in scored[:cap]]


def embed_texts(
    texts: Sequence[str], *, settings: object | None = None
) -> list[list[float]] | None:
    """BYOK embeddings. Returns None when unavailable so callers degrade to BM25."""
    if not texts:
        return []
    key = None
    if settings is not None:
        key = getattr(settings, "openai_api_key", None)
    if not key:
        import os

        key = (os.environ.get("OPENAI_API_KEY") or "").strip() or None
    if not key:
        return None
    try:
        return _openai_embed(list(texts), api_key=str(key))
    except Exception:  # noqa: BLE001
        return None


def _openai_embed(texts: list[str], *, api_key: str) -> list[list[float]]:
    import json
    import urllib.error
    import urllib.request

    payload = json.dumps(
        {"model": "text-embedding-3-small", "input": texts},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/embeddings",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "readyagents-memory",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 — BYOK public API
        body = json.loads(resp.read().decode("utf-8"))
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list) or len(data) != len(texts):
        raise RuntimeError("embedding response mismatch")
    out: list[list[float]] = []
    for row in sorted(data, key=lambda item: int(item.get("index", 0))):
        vec = row.get("embedding") if isinstance(row, dict) else None
        if not isinstance(vec, list):
            raise RuntimeError("embedding missing")
        out.append([float(x) for x in vec])
    return out
