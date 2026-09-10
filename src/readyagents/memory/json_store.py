"""JSON memory backend. Atomic rewrite under $READYAGENTS_HOME/memory/."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from readyagents.atomic import atomic_write_text
from readyagents.errors import ConfigError, MemoryError
from readyagents.memory.protocol import (
    MAX_RECORDS_PER_SCOPE,
    MemoryHit,
    MemoryRecord,
    bound_limit,
    bound_text,
)
from readyagents.memory.retrieve import bm25_search
from readyagents.workflow.state import utc_now


class JsonMemoryStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / "store.json"
        self._lock = threading.RLock()
        self._closed = False

    def write(self, record: MemoryRecord, *, vector: list[float] | None = None) -> str:
        self._ensure_open()
        bound_text(record.text)
        with self._lock:
            data = self._load()
            records = data["records"]
            live = [
                item
                for item in records.values()
                if item.scope == record.scope and not _expired(item) and item.id != record.id
            ]
            if len(live) >= MAX_RECORDS_PER_SCOPE:
                raise MemoryError(
                    f"memory scope {record.scope!r} exceeds {MAX_RECORDS_PER_SCOPE} records"
                )
            records[record.id] = record
            if vector is not None:
                data["vectors"][record.id] = [float(x) for x in vector]
            else:
                data["vectors"].pop(record.id, None)
            self._dump(data)
            return record.id

    def read(self, scope: str, *, limit: int = 0) -> list[MemoryRecord]:
        self._ensure_open()
        with self._lock:
            data = self._load()
            items = [
                rec for rec in data["records"].values() if rec.scope == scope and not _expired(rec)
            ]
        items.sort(key=lambda rec: rec.created_at, reverse=True)
        if limit and limit > 0:
            items = items[: bound_limit(limit)]
        return items

    def get(self, record_id: str) -> MemoryRecord:
        self._ensure_open()
        with self._lock:
            data = self._load()
            rec = data["records"].get(record_id)
        if rec is None or _expired(rec):
            raise ConfigError(f"Memory record not found: {record_id}")
        return rec

    def search(self, scope: str, query: str, *, limit: int = 5) -> list[MemoryHit]:
        return bm25_search(self.read(scope), query, limit=limit)

    def forget(
        self,
        *,
        scope: str | None = None,
        record_id: str | None = None,
        subject: str | None = None,
    ) -> int:
        self._ensure_open()
        with self._lock:
            data = self._load()
            remove: list[str] = []
            if record_id:
                if record_id in data["records"]:
                    remove.append(record_id)
            else:
                want_scope = scope
                if subject:
                    want_scope = f"subject:{subject}"
                for rec_id, rec in data["records"].items():
                    if want_scope is None or rec.scope == want_scope:
                        remove.append(rec_id)
            for rec_id in remove:
                data["records"].pop(rec_id, None)
                data["vectors"].pop(rec_id, None)
            self._dump(data)
            return len(remove)

    def list(self, *, scope: str | None = None, limit: int = 0) -> list[MemoryRecord]:
        self._ensure_open()
        with self._lock:
            data = self._load()
            items = [
                rec
                for rec in data["records"].values()
                if not _expired(rec) and (scope is None or rec.scope == scope)
            ]
        items.sort(key=lambda rec: rec.created_at, reverse=True)
        if limit and limit > 0:
            items = items[: bound_limit(limit)]
        return items

    def vector(self, record_id: str) -> list[float] | None:
        self._ensure_open()
        with self._lock:
            data = self._load()
            vec = data["vectors"].get(record_id)
        if not isinstance(vec, list):
            return None
        return [float(x) for x in vec]

    def close(self) -> None:
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise ConfigError("Memory store is closed")

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"records": {}, "vectors": {}}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {"records": {}, "vectors": {}}
        records: dict[str, MemoryRecord] = {}
        blob = raw.get("records") if isinstance(raw, dict) else None
        if isinstance(blob, dict):
            for _key, value in blob.items():
                if not isinstance(value, dict):
                    continue
                try:
                    rec = MemoryRecord.from_dict(value)
                except (TypeError, ValueError):
                    continue
                if rec.id:
                    records[rec.id] = rec
        vectors: dict[str, list[float]] = {}
        vecs = raw.get("vectors") if isinstance(raw, dict) else None
        if isinstance(vecs, dict):
            for key, value in vecs.items():
                if isinstance(value, list):
                    try:
                        vectors[str(key)] = [float(x) for x in value]
                    except (TypeError, ValueError):
                        continue
        return {"records": records, "vectors": vectors}

    def _dump(self, data: dict[str, Any]) -> None:
        payload = {
            "records": {key: rec.as_dict() for key, rec in data["records"].items()},
            "vectors": data["vectors"],
        }
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.path,
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )


def _expired(record: MemoryRecord) -> bool:
    if not record.expires_at:
        return False
    return record.expires_at <= utc_now()
