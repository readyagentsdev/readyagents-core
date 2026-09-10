"""Declared context compaction. Always records what was dropped."""

from __future__ import annotations

from typing import Any

from readyagents.cost.tokens import heuristic_tokens
from readyagents.errors import MemoryError
from readyagents.workflow.schema import ContextSpec


def compact_text(
    text: str,
    spec: ContextSpec | None,
    *,
    llm: Any = None,
    node_id: str = "",
) -> tuple[str, dict[str, Any] | None]:
    if spec is None or spec.max_tokens is None:
        return text, None
    before = heuristic_tokens(text)
    cap = int(spec.max_tokens)
    if before <= cap:
        return text, None
    strategy = (spec.on_exceed or "truncate").strip().lower()
    if strategy == "fail":
        raise MemoryError(f"memory context exceeded max_tokens={cap} (tokens_before={before})")
    note = None
    if strategy == "summarize":
        summarized, note = _summarize(text, spec, llm=llm, cap=cap)
        if summarized is not None:
            after = heuristic_tokens(summarized)
            dropped = text if after < before else ""
            return summarized, {
                "strategy": "summarize",
                "tokens_before": before,
                "tokens_after": after,
                "dropped": dropped[:8_000],
            }
        strategy = "truncate"
    cut = max(1, cap * 4)
    kept = text[:cut]
    dropped = text[cut:]
    payload: dict[str, Any] = {
        "strategy": "truncate",
        "tokens_before": before,
        "tokens_after": heuristic_tokens(kept),
        "dropped": dropped[:8_000],
    }
    if note:
        payload["note"] = note
    return kept, payload


def apply_compacted_items(
    items: list[dict[str, Any]], kept: str, *, join: str = "\n"
) -> list[dict[str, Any]]:
    """Rewrite item texts so dropped content is not in the node output."""
    if not items:
        return items
    joined = join.join(str(item.get("text") or "") for item in items)
    if kept == joined:
        return items
    if joined.startswith(kept):
        remaining = kept
        out: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            row = dict(item)
            original = str(item.get("text") or "")
            rest = remaining[len(original) :] if remaining.startswith(original) else None
            if rest is not None and (rest == "" or rest.startswith(join)):
                row["text"] = original
                remaining = rest[len(join) :] if rest.startswith(join) else rest
                out.append(row)
                continue
            row["text"] = remaining
            out.append(row)
            for extra in items[index + 1 :]:
                blank = dict(extra)
                blank["text"] = ""
                out.append(blank)
            return out
        return out
    first = dict(items[0])
    first["text"] = kept
    out = [first]
    for extra in items[1:]:
        blank = dict(extra)
        blank["text"] = ""
        out.append(blank)
    return out


def _summarize(
    text: str, spec: ContextSpec, *, llm: Any, cap: int
) -> tuple[str | None, str | None]:
    if llm is None:
        return None, "summarize unavailable; truncated"
    model = spec.summarize_model or getattr(llm, "name", None) or "openai:gpt-4o-mini"
    try:
        from readyagents.llm.base import Message

        result = llm.complete(
            [
                Message(
                    role="user",
                    content=f"Summarize the following in at most {cap} tokens.\n\n{text[:20_000]}",
                )
            ],
            model=model,
        )
        out = getattr(result, "text", None)
        if isinstance(out, str) and out.strip():
            return out.strip(), None
    except Exception as extra:  # noqa: BLE001
        return None, f"summarize failed: {type(extra).__name__}"
    return None, "summarize returned empty"
