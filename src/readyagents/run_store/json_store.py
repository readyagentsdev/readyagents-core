"""JSON-file RunStore. Default backend; additive ``_store_revision`` field."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from readyagents.errors import ConfigError, RunStoreConflict
from readyagents.run_store.base import RunQuery, StoredRun
from readyagents.workflow.state import RunState

_REV_KEY = "_store_revision"


class JsonRunStore:
    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = Path(runs_dir)
        self._locks: dict[str, threading.Lock] = {}
        self._meta = threading.Lock()
        self._closed = False

    def _run_lock(self, run_id: str) -> threading.Lock:
        with self._meta:
            lock = self._locks.get(run_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[run_id] = lock
            return lock

    def save(
        self,
        state: RunState,
        *,
        redactor: Any = None,
        expected_revision: int | None = None,
    ) -> int:
        self._ensure_open()
        run_id = state.run_id
        path = self.runs_dir / f"{run_id}.json"
        with self._run_lock(run_id):
            current = None
            if path.is_file():
                current = self._read_path(path)
            if expected_revision is not None:
                if current is None:
                    raise RunStoreConflict(f"Run not found: {run_id}")
                if current.revision != expected_revision:
                    raise RunStoreConflict(
                        f"Run {run_id} revision {current.revision} != expected {expected_revision}"
                    )
                next_rev = expected_revision + 1
            elif current is None:
                next_rev = 1
            else:
                next_rev = current.revision + 1
            record_obj: Any = state.to_record()
            if redactor is not None:
                record_obj = redactor.redact(record_obj)
            if not isinstance(record_obj, dict):
                record_obj = dict(state.to_record())
            record_obj[_REV_KEY] = next_rev
            self.runs_dir.mkdir(parents=True, exist_ok=True)
            from readyagents.atomic import atomic_write_text

            atomic_write_text(
                path,
                json.dumps(record_obj, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            return next_rev

    def get(self, run_id: str, *, allow_prefix: bool = True) -> StoredRun:
        self._ensure_open()
        path = self._resolve_path(run_id, allow_prefix=allow_prefix)
        return self._read_path(path)

    def list(self, query: RunQuery | None = None) -> list[StoredRun]:
        self._ensure_open()
        query = query or RunQuery()
        if not self.runs_dir.is_dir():
            return []
        found: list[StoredRun] = []
        for path in self.runs_dir.glob("*.json"):
            if path.name.startswith("."):
                continue
            try:
                found.append(self._read_path(path))
            except (OSError, json.JSONDecodeError, ConfigError, TypeError, ValueError):
                continue
        found.sort(key=lambda item: (item.state.started_at, item.state.run_id), reverse=True)
        if query.status:
            wanted = query.status.strip().lower()
            found = [item for item in found if item.state.status == wanted]
        if query.workflow:
            wanted_wf = query.workflow.strip()
            found = [item for item in found if item.state.workflow_name == wanted_wf]
        if query.cursor:
            ids = [item.state.run_id for item in found]
            try:
                idx = ids.index(query.cursor)
                found = found[idx + 1 :]
            except ValueError:
                pass
        if query.limit and query.limit > 0:
            found = found[: query.limit]
        return [
            StoredRun(state=item.state, revision=item.revision, cursor=item.state.run_id)
            for item in found
        ]

    def delete(self, run_id: str, *, allow_prefix: bool = True) -> StoredRun:
        stored = self.get(run_id, allow_prefix=allow_prefix)
        path = self.runs_dir / f"{stored.state.run_id}.json"
        if not path.is_file():
            raise ConfigError(f"Run not found: {run_id}")
        path.unlink()
        return stored

    def gc(
        self,
        *,
        statuses: list[str] | None = None,
        include_paused: bool = False,
        keep: int = 0,
    ) -> list[str]:
        wanted = {s.strip().lower() for s in (statuses or ["succeeded", "failed", "cancelled"])}
        if include_paused:
            wanted.add("paused")
        found = self.list(RunQuery())
        if keep and keep > 0:
            found = found[keep:]
        deleted: list[str] = []
        for item in found:
            if item.state.status == "paused" and not include_paused:
                continue
            if item.state.status not in wanted:
                continue
            path = self.runs_dir / f"{item.state.run_id}.json"
            if path.is_file():
                path.unlink()
                deleted.append(item.state.run_id)
        return deleted

    def close(self) -> None:
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise ConfigError("Run store is closed")

    def _resolve_path(self, run_id: str, *, allow_prefix: bool) -> Path:
        exact = self.runs_dir / f"{run_id}.json"
        if exact.is_file():
            return exact
        if not allow_prefix:
            raise ConfigError(f"Run not found: {run_id}")
        matches = sorted(self.runs_dir.glob(f"{run_id}*.json")) if self.runs_dir.is_dir() else []
        matches = [p for p in matches if not p.name.startswith(".")]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            ids = ", ".join(p.stem for p in matches[:8])
            raise ConfigError(f"Run id '{run_id}' is ambiguous. Matches: {ids}")
        raise ConfigError(f"Run not found: {run_id}")

    def _read_path(self, path: Path) -> StoredRun:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Corrupt run record {path}: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"Cannot read run record {path}: {exc}") from exc
        if not isinstance(data, dict) or not data.get("run_id"):
            raise ConfigError(f"Invalid run record: {path}")
        raw_rev = data.get(_REV_KEY, 1)
        try:
            revision = int(raw_rev)
        except (TypeError, ValueError):
            revision = 1
        if revision < 1:
            revision = 1
        state = RunState.from_record(data)
        return StoredRun(state=state, revision=revision, cursor=state.run_id)
