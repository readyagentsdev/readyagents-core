"""JSON → SQLite run-store migration. Non-destructive; never deletes source JSON."""

from __future__ import annotations

import json
import sqlite3
import stat
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from readyagents.config import Settings, get_settings
from readyagents.errors import ConfigError, RunStoreError
from readyagents.run_store.sqlite_store import SQLiteRunStore
from readyagents.workflow.state import RunState, utc_now

ConflictPolicy = Literal["error", "skip-identical"]
_BATCH = 50
_CANONICAL_DUMP = {"sort_keys": True, "separators": (",", ":"), "ensure_ascii": False}


@dataclass
class PlannedRecord:
    path: Path
    run_id: str
    state: RunState
    canonical: str
    raw_bytes: bytes


@dataclass
class MigrationReport:
    ok: bool
    command: str = "runs migrate"
    source_backend: str = "json"
    destination_backend: str = "sqlite"
    scanned: int = 0
    imported: int = 0
    skipped: int = 0
    conflicts: int = 0
    invalid: int = 0
    verified: bool = False
    dry_run: bool = False
    error: str | None = None
    message: str | None = None
    invalid_paths: list[str] = field(default_factory=list)
    conflict_ids: list[str] = field(default_factory=list)

    def as_envelope(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("invalid_paths", None)
        payload.pop("conflict_ids", None)
        if not self.ok:
            payload["error"] = self.error or "RunStoreError"
            payload["message"] = self.message or "migration failed"
        else:
            payload.pop("error", None)
            payload.pop("message", None)
        return payload


def _canonical_json(record: Mapping[str, Any]) -> str:
    cleaned = {k: v for k, v in record.items() if k != "_store_revision"}
    return json.dumps(cleaned, **_CANONICAL_DUMP)


def _normalize_state(data: Mapping[str, Any]) -> tuple[RunState, str]:
    state = RunState.from_record(data)
    if not state.run_id:
        raise ValueError("missing run_id")
    canonical = _canonical_json(state.to_record())
    return state, canonical


def resolve_source_dir(path: Path | str | None, settings: Settings) -> Path:
    if path is None:
        source = settings.runs_dir()
    else:
        source = Path(path).expanduser()
        if not source.is_absolute():
            source = Path.cwd() / source
        source = source.resolve()
    if source.exists() and not source.is_dir():
        raise ConfigError(f"Migration source is not a directory: {source}")
    return source


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def resolve_database_path(path: Path | str | None, settings: Settings) -> Path:
    home = settings.home_path().resolve()
    constrained = False
    if path is None:
        given = settings.run_db
        if given is None:
            dest = home / "runs.sqlite3"
            constrained = True
        else:
            dest = Path(given).expanduser()
            if not dest.is_absolute():
                dest = home / dest
                constrained = True
    else:
        dest = Path(path).expanduser()
        if not dest.is_absolute():
            dest = home / dest
            constrained = True
    if dest.is_symlink():
        target = dest.resolve()
        if constrained and not _is_under(target, home):
            raise ConfigError(
                f"Database symlink target is outside READYAGENTS_HOME: {dest}"
            )
        dest = target
    else:
        dest = dest.resolve()
    if dest.exists():
        mode = dest.stat().st_mode
        if stat.S_ISDIR(mode):
            raise ConfigError(f"Database path is a directory: {dest}")
        if not stat.S_ISREG(mode):
            raise ConfigError(f"Database path is not a regular file: {dest}")
    parent = dest.parent
    if parent.exists() and not parent.is_dir():
        raise ConfigError(f"Database parent is not a directory: {parent}")
    return dest


def _reject_alias(source: Path, dest: Path) -> None:
    if dest.resolve() == source.resolve():
        raise ConfigError("Source directory and database path must not be the same")
    try:
        dest.resolve().relative_to(source.resolve())
    except ValueError:
        return
    raise ConfigError("Database path must not live inside the JSON source directory")


def _source_files(source: Path) -> list[Path]:
    if not source.is_dir():
        return []
    files = [p for p in source.glob("*.json") if p.is_file() and not p.name.startswith(".")]
    return sorted(files, key=lambda p: p.name)


def _plan_file(path: Path) -> PlannedRecord | str:
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return f"{path.name}: not valid JSON ({exc})"
    if not isinstance(data, dict) or not data.get("run_id"):
        return f"{path.name}: invalid run record"
    try:
        state, canonical = _normalize_state(data)
    except (TypeError, ValueError, KeyError) as exc:
        return f"{path.name}: {exc}"
    if str(data.get("run_id")) != state.run_id:
        return f"{path.name}: run_id mismatch"
    return PlannedRecord(
        path=path,
        run_id=state.run_id,
        state=state,
        canonical=canonical,
        raw_bytes=raw,
    )


def plan_migration(
    source: Path,
    dest: Path,
    *,
    on_conflict: ConflictPolicy = "error",
    skip_invalid: bool = False,
) -> tuple[list[PlannedRecord], MigrationReport]:
    report = MigrationReport(ok=True)
    files = _source_files(source)
    report.scanned = len(files)
    planned: list[PlannedRecord] = []
    seen: dict[str, Path] = {}
    dest_rows: dict[str, str] = {}
    if dest.exists():
        dest_rows = _destination_canonicals(dest)

    for path in files:
        item = _plan_file(path)
        if isinstance(item, str):
            report.invalid += 1
            report.invalid_paths.append(item)
            continue
        if item.run_id in seen:
            report.conflicts += 1
            report.conflict_ids.append(item.run_id)
            continue
        seen[item.run_id] = path
        existing = dest_rows.get(item.run_id)
        if existing is not None:
            if on_conflict == "skip-identical" and existing == item.canonical:
                report.skipped += 1
                continue
            report.conflicts += 1
            report.conflict_ids.append(item.run_id)
            continue
        planned.append(item)

    if report.invalid and not skip_invalid:
        report.ok = False
        report.error = "RunStoreError"
        report.message = (
            f"Invalid source records: {report.invalid}. "
            "Pass --skip-invalid to ignore them, or fix the JSON."
        )
        return [], report
    if report.conflicts and on_conflict == "error":
        report.ok = False
        report.error = "RunStoreConflict"
        report.message = (
            f"Conflicting run id(s): {', '.join(report.conflict_ids[:8])}. "
            "Pass --on-conflict skip-identical to skip identical rows."
        )
        return [], report
    if report.conflicts and on_conflict == "skip-identical":
        report.ok = False
        report.error = "RunStoreConflict"
        report.message = "Non-identical destination collision(s): " + ", ".join(
            report.conflict_ids[:8]
        )
        return [], report
    return planned, report


def _destination_canonicals(dest: Path) -> dict[str, str]:
    store = SQLiteRunStore(dest)
    try:
        rows: dict[str, str] = {}
        conn = store._conn()
        for row in conn.execute("SELECT run_id, record_json FROM runs"):
            run_id = str(row[0])
            try:
                data = json.loads(row[1])
                if not isinstance(data, dict):
                    continue
                _state, canonical = _normalize_state(data)
                rows[run_id] = canonical
            except (TypeError, ValueError, json.JSONDecodeError):
                rows[run_id] = ""
        return rows
    finally:
        store.close()


def _insert_batch(conn: sqlite3.Connection, batch: list[PlannedRecord]) -> None:
    now = utc_now()
    rows = []
    for item in batch:
        record = json.loads(item.canonical)
        rows.append(
            (
                item.run_id,
                item.state.workflow_name,
                item.state.status,
                item.state.started_at,
                item.state.finished_at,
                item.state.pending_node,
                1,
                json.dumps(record, ensure_ascii=False),
                now,
            )
        )
    conn.executemany(
        """
        INSERT INTO runs (
            run_id, workflow, status, started_at, finished_at,
            pending_node, revision, record_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def verify_migration(dest: Path, planned: list[PlannedRecord]) -> None:
    store = SQLiteRunStore(dest)
    try:
        conn = store._conn()
        dest_count = int(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
        if dest_count < len(planned):
            raise RunStoreError(
                f"Verification failed: destination has {dest_count} rows, "
                f"expected at least {len(planned)}"
            )
        for item in planned:
            row = conn.execute(
                "SELECT run_id, record_json FROM runs WHERE run_id = ?",
                (item.run_id,),
            ).fetchone()
            if row is None:
                raise RunStoreError(f"Verification failed: missing run {item.run_id}")
            data = json.loads(row[1])
            if str(data.get("run_id")) != item.run_id:
                raise RunStoreError(f"Verification failed: run_id mismatch for {item.run_id}")
            _state, canonical = _normalize_state(data)
            if canonical != item.canonical:
                raise RunStoreError(f"Verification failed: record mismatch for {item.run_id}")
    finally:
        store.close()


def migrate_json_to_sqlite(
    *,
    source: Path | str | None = None,
    database: Path | str | None = None,
    settings: Settings | None = None,
    on_conflict: ConflictPolicy = "error",
    skip_invalid: bool = False,
    verify: bool = True,
    dry_run: bool = False,
    fail_after: int | None = None,
) -> MigrationReport:
    """Copy JSON run files into SQLite. Never deletes or rewrites source JSON."""
    settings = settings or get_settings()
    if on_conflict not in {"error", "skip-identical"}:
        raise ConfigError("on-conflict must be error or skip-identical")
    src = resolve_source_dir(source, settings)
    dest = resolve_database_path(database, settings)
    _reject_alias(src, dest)

    snapshots = {p: p.read_bytes() for p in _source_files(src)}
    planned, report = plan_migration(src, dest, on_conflict=on_conflict, skip_invalid=skip_invalid)
    report.dry_run = dry_run
    if not report.ok:
        _assert_sources_unchanged(snapshots)
        return report

    if dry_run:
        report.imported = 0
        report.verified = False
        _assert_sources_unchanged(snapshots)
        return report

    dest.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteRunStore(dest, busy_timeout_ms=settings.sqlite_busy_timeout_ms)
    imported = 0
    try:
        conn = store._conn()
        pending: list[PlannedRecord] = []
        for item in planned:
            pending.append(item)
            if len(pending) >= _BATCH:
                imported = _commit_batch(conn, pending, imported, fail_after)
                pending = []
        if pending:
            imported = _commit_batch(conn, pending, imported, fail_after)
        report.imported = imported
        if verify:
            verify_migration(dest, planned)
            report.verified = True
        else:
            report.verified = False
    except RunStoreError as exc:
        report.ok = False
        report.error = type(exc).__name__
        report.message = str(exc)
        report.imported = imported
        report.verified = False
        _assert_sources_unchanged(snapshots)
        return report
    finally:
        store.close()

    _assert_sources_unchanged(snapshots)
    return report


def _commit_batch(
    conn: sqlite3.Connection,
    batch: list[PlannedRecord],
    imported: int,
    fail_after: int | None,
) -> int:
    conn.execute("BEGIN")
    try:
        if fail_after is not None and imported + len(batch) > fail_after:
            raise RunStoreError("injected mid-batch failure")
        _insert_batch(conn, batch)
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        raise RunStoreError("destination insert conflict") from exc
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return imported + len(batch)


def _assert_sources_unchanged(snapshots: dict[Path, bytes]) -> None:
    for path, original in snapshots.items():
        current = path.read_bytes()
        if current != original:
            raise RunStoreError(f"Source JSON was modified: {path.name}")
