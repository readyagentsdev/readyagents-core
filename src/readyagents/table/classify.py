"""type: classify — deterministic rules first, model only on the remainder."""

from __future__ import annotations

import json
from typing import Any

from readyagents.errors import TableError, TableRowError
from readyagents.table.expr import eval_predicate
from readyagents.table.node import _caps, _record, _replay, resolve_part, store_from
from readyagents.table.part import Column
from readyagents.workflow.templates import interpolate


def run_classify_node(node: Any, state: Any, ctx: Any) -> Any:
    if getattr(ctx, "dry_run", False):
        return {"dry_run": True, "op": "classify"}
    if getattr(ctx, "offline", False) and getattr(ctx, "cassette", None) is not None:
        replayed = _replay(node, ctx)
        if replayed is not None:
            return replayed
    store = store_from(ctx)
    part = resolve_part(node.source, state, ctx, store)
    rules = list(getattr(node, "rules", None) or [])
    remainder_spec = dict(getattr(node, "model_for_remainder", None) or {})
    on_error = str(getattr(node, "on_row_error", None) or "fail").strip().lower()
    if on_error not in {"fail", "skip", "quarantine"}:
        raise TableError("on_row_error must be fail, skip, or quarantine")
    decided: list[tuple[int, dict[str, Any], str, str]] = []
    remainder: list[tuple[int, dict[str, Any]]] = []
    for index, row in enumerate(store.iter_rows(part.sha256)):
        label = _rule_label(rules, row)
        if label is not None:
            decided.append((index, row, label, "rule"))
        else:
            remainder.append((index, row))
    model_calls = 0
    if remainder:
        if not remainder_spec.get("model") and not getattr(node, "model", None):
            raise TableError("classify remainder requires model_for_remainder.model")
        labelled, model_calls = _model_remainder(
            remainder,
            node=node,
            ctx=ctx,
            spec=remainder_spec,
            on_error=on_error,
        )
        decided.extend(labelled)
    decided.sort(key=lambda item: item[0])
    columns = list(part.columns) + [
        Column(name="label", type="str"),
        Column(name="decision", type="str"),
    ]
    good: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    skipped = 0
    for index, row, label, path in decided:
        if label is None:
            if on_error == "fail":
                raise TableRowError(index, reason="unlabelled")
            if on_error == "skip":
                skipped += 1
                continue
            quarantined.append({"row": index, "column": "label", "reason": "unlabelled"})
            continue
        out = dict(row)
        out["label"] = label
        out["decision"] = path
        good.append(out)
    max_rows, max_bytes = _caps(node)
    result = store.put_rows(
        columns,
        good,
        max_rows=max_rows,
        max_bytes=max_bytes,
        op="classify",
        input_sha256=[part.sha256],
    )
    if quarantined:
        err_cols = [
            Column(name="row", type="int"),
            Column(name="column", type="str"),
            Column(name="reason", type="str"),
        ]
        errors = store.put_rows(
            err_cols, quarantined, max_rows=max_rows, max_bytes=max_bytes, op="errors"
        )
        result.errors_sha256 = errors.sha256
        result.errors_row_count = errors.row_count
    meta = getattr(state, "metadata", None)
    if isinstance(meta, dict):
        bucket = meta.setdefault("classify", {})
        if not isinstance(bucket, dict):
            bucket = {}
            meta["classify"] = bucket
        bucket[node.id] = {
            "rule_rows": sum(1 for item in decided if item[3] == "rule"),
            "model_rows": sum(1 for item in decided if item[3] == "model"),
            "model_calls": model_calls,
            "skipped": skipped,
            "quarantined": len(quarantined),
            "batch": int(remainder_spec.get("batch") or 25),
        }
    _record(node, ctx, result)
    return result.as_ref()


def _rule_label(rules: list[Any], row: dict[str, Any]) -> str | None:
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        when = str(rule.get("when") or "").strip()
        if when and eval_predicate(when, row):
            return str(rule.get("label") or "")
    return None


def _model_remainder(
    remainder: list[tuple[int, dict[str, Any]]],
    *,
    node: Any,
    ctx: Any,
    spec: dict[str, Any],
    on_error: str,
) -> tuple[list[tuple[int, dict[str, Any], str | None, str]], int]:
    from readyagents.cost.tokens import heuristic_tokens
    from readyagents.llm.base import Message

    model = str(spec.get("model") or getattr(node, "model", None) or "")
    batch = max(1, int(spec.get("batch") or 25))
    allowed = [str(x) for x in (spec.get("labels") or [])]
    llm = getattr(ctx, "llm", None)
    if llm is None or not hasattr(llm, "complete"):
        raise TableError("classify remainder requires an LLM")
    out: list[tuple[int, dict[str, Any], str | None, str]] = []
    calls = 0
    for start in range(0, len(remainder), batch):
        chunk = remainder[start : start + batch]
        payload = [{"index": idx, "row": _public_row(row)} for idx, row in chunk]
        prompt = (
            "Label each row. Allowed labels: "
            + json.dumps(allowed)
            + ". Return a JSON array of {index, label} objects.\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        redactor = getattr(ctx, "redactor", None)
        if redactor is not None:
            method = getattr(redactor, "redact_text", None) or getattr(redactor, "redact", None)
            if callable(method):
                prompt = str(method(prompt))
        tokens = heuristic_tokens(prompt)
        meter = getattr(ctx, "spend_meter", None)
        if meter is not None:
            consult = getattr(meter, "consult_before_call", None)
            if callable(consult):
                consult(model, prompt_tokens=tokens)
        result = llm.complete([Message(role="user", content=prompt)], model=model)
        calls += 1
        if meter is not None:
            record = getattr(meter, "record_usage", None)
            if callable(record):
                usage = dict(getattr(result, "usage", None) or {})
                if not usage:
                    usage = {"prompt_tokens": tokens, "total_tokens": tokens}
                record(model, usage)
        labels = _parse_labels(getattr(result, "text", "") or "", chunk)
        for (idx, row), label in zip(chunk, labels, strict=True):
            if allowed and label is not None and label not in allowed:
                label = None
            if label is None and on_error == "fail":
                raise TableRowError(idx, column="label", reason="invalid label")
            out.append((idx, row, label, "model"))
    return out, calls


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {str(k): v for k, v in row.items()}


def _parse_labels(text: str, chunk: list[tuple[int, dict[str, Any]]]) -> list[str | None]:
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < 0:
        return [None] * len(chunk)
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return [None] * len(chunk)
    by_index: dict[int, str] = {}
    if isinstance(payload, list):
        for item, (idx, _row) in zip(payload, chunk, strict=False):
            if isinstance(item, dict):
                label = item.get("label")
                key = item.get("index", idx)
                try:
                    by_index[int(key)] = str(label) if label is not None else ""
                except (TypeError, ValueError):
                    continue
            elif isinstance(item, str):
                by_index[idx] = item
    return [by_index.get(idx) or None for idx, _row in chunk]


def interpolate_source(raw: Any, state: Any) -> Any:
    if isinstance(raw, str):
        return interpolate(raw, state.mapping())
    return raw
