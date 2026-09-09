"""Append-only hash-chained spend ledger under ``$READYAGENTS_HOME/ledger/``."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from readyagents.audit import GENESIS_HASH, compute_entry_hash
from readyagents.errors import WorkflowError
from readyagents.workflow.state import utc_now

LEDGER_NAME = "spend.jsonl"
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_BY_CHOICES = frozenset({"day", "workflow", "model", "actor", "label"})


def ledger_dir_for(home: Path) -> Path:
    return Path(home) / "ledger"


def parse_labels(pairs: list[str]) -> dict[str, str]:
    """Parse ``--label KEY=VALUE``. Values stay strings (no scalar coerce)."""
    result: dict[str, str] = {}
    for raw in pairs:
        if "=" not in raw:
            raise WorkflowError(f"Invalid --label '{raw}' (expected KEY=VALUE)")
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise WorkflowError(f"Invalid --label '{raw}' (empty key)")
        result[key] = value
    return result


def append_spend(
    ledger_dir: Path,
    entry: Mapping[str, Any],
    *,
    redactor: Any = None,
) -> Path:
    """Append one hash-chained JSON object. Never truncates an existing file."""
    payload = dict(entry)
    payload.setdefault("event", "spend")
    payload.setdefault("ts", utc_now())
    if redactor is not None:
        payload = redactor.redact(payload)
    folder = Path(ledger_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / LEDGER_NAME
    lock = _lock_for(path)
    with lock:
        seq, prev_hash = _tail_chain(path)
        payload["seq"] = seq + 1
        payload["prev_hash"] = prev_hash
        payload["entry_hash"] = compute_entry_hash(payload)
        line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        fd = os.open(path, flags, 0o600)
        try:
            from readyagents.permissions import restrict_file

            restrict_file(path)
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
    return path


def read_spend_entries(ledger_dir: Path | str) -> list[dict[str, Any]]:
    path = Path(ledger_dir) / LEDGER_NAME
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def spend_entry_from_state(
    state: Any, *, labels: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Build a ledger payload from a terminal ``RunState``."""
    spend = {}
    if isinstance(getattr(state, "metadata", None), dict):
        raw = state.metadata.get("spend")
        if isinstance(raw, dict):
            spend = raw
        stored_labels = state.metadata.get("labels")
        if labels is None and isinstance(stored_labels, dict):
            labels = {str(k): str(v) for k, v in stored_labels.items()}
    usage = dict(getattr(state, "usage", None) or {})
    actor = None
    if isinstance(getattr(state, "metadata", None), dict):
        actor = state.metadata.get("actor")
    by_model = spend.get("by_model") if isinstance(spend.get("by_model"), dict) else {}
    return {
        "event": "spend",
        "run_id": getattr(state, "run_id", None),
        "workflow": getattr(state, "workflow_name", None),
        "status": getattr(state, "status", None),
        "actor": actor,
        "labels": dict(labels or {}),
        "prompt_tokens": int(usage.get("prompt_tokens") or spend.get("prompt_tokens") or 0),
        "completion_tokens": int(
            usage.get("completion_tokens") or spend.get("completion_tokens") or 0
        ),
        "total_tokens": int(usage.get("total_tokens") or spend.get("total_tokens") or 0),
        "cost_micros": int(usage.get("cost_micros") or spend.get("cost_micros") or 0),
        "unpriced": bool(spend.get("unpriced")),
        "unpriced_models": list(spend.get("unpriced_models") or []),
        "cache_hits": int(usage.get("cache_hits") or spend.get("cache_hits") or 0),
        "cache_misses": int(usage.get("cache_misses") or spend.get("cache_misses") or 0),
        "cache_savings_micros": int(
            usage.get("cache_savings_micros") or spend.get("cache_savings_micros") or 0
        ),
        "by_model": {str(k): dict(v) for k, v in by_model.items() if isinstance(v, dict)},
        "models": list(by_model.keys()),
    }


@dataclass
class SpendRow:
    key: str
    runs: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_micros: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    cache_savings_micros: int = 0
    unpriced: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "runs": self.runs,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_micros": self.cost_micros,
            "cost_usd": self.cost_micros / 1_000_000,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cache_savings_micros": self.cache_savings_micros,
            "unpriced": self.unpriced,
        }


@dataclass
class SpendAggregate:
    by: str
    since: str | None
    rows: list[SpendRow] = field(default_factory=list)
    skipped_corrupt: int = 0
    files: int = 0

    def as_dict(self) -> dict[str, Any]:
        total = SpendRow(key="total")
        for row in self.rows:
            total.runs += row.runs
            total.prompt_tokens += row.prompt_tokens
            total.completion_tokens += row.completion_tokens
            total.total_tokens += row.total_tokens
            total.cost_micros += row.cost_micros
            total.cache_hits += row.cache_hits
            total.cache_misses += row.cache_misses
            total.cache_savings_micros += row.cache_savings_micros
            total.unpriced = total.unpriced or row.unpriced
        return {
            "by": self.by,
            "since": self.since,
            "rows": [row.as_dict() for row in self.rows],
            "total": total.as_dict(),
            "skipped_corrupt": self.skipped_corrupt,
            "files": self.files,
        }


def query_spend(
    ledger_dir: Path | str,
    *,
    since: str | None = None,
    by: str = "day",
) -> SpendAggregate:
    dimension = (by or "day").strip().lower()
    if dimension not in _BY_CHOICES:
        raise WorkflowError(f"Invalid --by '{by}'. Expected day, workflow, model, actor, or label.")
    folder = Path(ledger_dir)
    path = folder / LEDGER_NAME
    agg = SpendAggregate(by=dimension, since=since, files=1 if path.is_file() else 0)
    since_dt = _parse_since(since) if since else None
    latest_by_run: dict[str, dict[str, Any]] = {}
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                agg.skipped_corrupt += 1
                continue
            if not isinstance(row, dict):
                agg.skipped_corrupt += 1
                continue
            if row.get("event") == "chain_anchor":
                continue
            run_id = str(row.get("run_id") or "")
            if run_id:
                latest_by_run[run_id] = row
            else:
                latest_by_run[f"anon-{len(latest_by_run)}"] = row
    buckets: dict[str, SpendRow] = {}
    for row in latest_by_run.values():
        if since_dt is not None and not _on_or_after(row.get("ts"), since_dt):
            continue
        keys = _group_keys(row, dimension)
        for key in keys:
            bucket = buckets.setdefault(key, SpendRow(key=key))
            _add_row(bucket, row)
    agg.rows = [buckets[k] for k in sorted(buckets)]
    return agg


def _group_keys(row: Mapping[str, Any], dimension: str) -> list[str]:
    if dimension == "day":
        ts = str(row.get("ts") or "")
        return [ts[:10] if len(ts) >= 10 else "unknown"]
    if dimension == "workflow":
        return [str(row.get("workflow") or "unknown")]
    if dimension == "actor":
        return [str(row.get("actor") or "(anonymous)")]
    if dimension == "model":
        models = row.get("models") or list((row.get("by_model") or {}).keys())
        if isinstance(models, list) and models:
            return [str(m) for m in models]
        return ["unknown"]
    if dimension == "label":
        labels = row.get("labels") or {}
        if isinstance(labels, dict) and labels:
            return [f"{k}={v}" for k, v in sorted(labels.items())]
        return ["(none)"]
    return ["unknown"]


def _add_row(bucket: SpendRow, row: Mapping[str, Any]) -> None:
    bucket.runs += 1
    bucket.prompt_tokens += int(row.get("prompt_tokens") or 0)
    bucket.completion_tokens += int(row.get("completion_tokens") or 0)
    bucket.total_tokens += int(row.get("total_tokens") or 0)
    bucket.cost_micros += int(row.get("cost_micros") or 0)
    bucket.cache_hits += int(row.get("cache_hits") or 0)
    bucket.cache_misses += int(row.get("cache_misses") or 0)
    bucket.cache_savings_micros += int(row.get("cache_savings_micros") or 0)
    bucket.unpriced = bucket.unpriced or bool(row.get("unpriced"))


def _parse_since(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError as exc:
            raise WorkflowError(f"Invalid --since '{value}' (expected YYYY-MM-DD)") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _on_or_after(ts: Any, since: datetime) -> bool:
    if not ts:
        return True
    try:
        text = str(ts).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed >= since


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def _tail_chain(path: Path) -> tuple[int, str]:
    if not path.is_file():
        return 0, GENESIS_HASH
    last_seq = 0
    last_hash = GENESIS_HASH
    count = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        count += 1
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if "seq" in row and "prev_hash" in row and "entry_hash" in row:
            last_seq = int(row["seq"])
            last_hash = str(row["entry_hash"])
        else:
            last_seq = max(last_seq, count)
    return last_seq, last_hash
