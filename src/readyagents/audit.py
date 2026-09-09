"""Append-only audit trail for runs and decisions.

Resume snapshots still overwrite ``$READYAGENTS_HOME/runs/<id>.json``.
Audit events are a separate JSONL file that is never rewritten.

Each new write is hash-chained (``seq``, ``prev_hash``, ``entry_hash``).
Pre-chain lines are accepted and reported as unchained, not broken.
The chain is per file; rotation writes an anchor carrying the previous
file's final hash. Hash chaining is tamper-evident, not tamper-proof.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from readyagents.workflow.state import utc_now

GENESIS_HASH = "0" * 64
DEFAULT_ROTATE_BYTES = 10_485_760
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def audit_dir_for(home: Path) -> Path:
    return Path(home) / "audit"


def canonical_audit_json(payload: Mapping[str, Any]) -> str:
    """One serialization point so parallel appends cannot fork the chain."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def compute_entry_hash(payload: Mapping[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k != "entry_hash"}
    return hashlib.sha256(canonical_audit_json(body).encode("utf-8")).hexdigest()


def append_audit_event(
    audit_dir: Path,
    event: Mapping[str, Any],
    *,
    rotate_bytes: int | None = None,
) -> Path:
    """Append one JSON object as a line. Never truncates an existing file."""
    if rotate_bytes is None:
        rotate_bytes = DEFAULT_ROTATE_BYTES
    payload = dict(event)
    payload.setdefault("ts", utc_now())
    run_id = str(payload.get("run_id") or "unknown")
    audit_dir = Path(audit_dir)
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / f"{run_id}.jsonl"
    lock = _lock_for(path)
    with lock:
        if rotate_bytes > 0:
            _rotate_if_needed(path, rotate_bytes)
        seq, prev_hash = _tail_chain(path)
        payload["seq"] = seq + 1
        payload["prev_hash"] = prev_hash
        payload["entry_hash"] = compute_entry_hash(payload)
        line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        fd = os.open(path, flags, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
    return path


def list_audit_files(audit_dir: Path | str, run_id: str) -> list[Path]:
    """Oldest to newest: ``run_id.1.jsonl``, ``run_id.2.jsonl``, …, ``run_id.jsonl``."""
    folder = Path(audit_dir)
    files: list[Path] = []
    n = 1
    while True:
        rotated = folder / f"{run_id}.{n}.jsonl"
        if not rotated.is_file():
            break
        files.append(rotated)
        n += 1
    current = folder / f"{run_id}.jsonl"
    if current.is_file():
        files.append(current)
    return files


def read_audit_events(audit_dir: Path, run_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path in list_audit_files(audit_dir, run_id):
        events.extend(_read_file_events(path))
    return events


def make_auditor(audit_dir: Path, redactor: Any = None, *, rotate_bytes: int | None = None):
    """Return ``auditor(event, **fields)`` that appends a redacted JSONL line."""

    def _audit(event: str, **fields: Any) -> None:
        payload: dict[str, Any] = {"event": event, **fields}
        if redactor is not None:
            payload = redactor.redact(payload)
        append_audit_event(audit_dir, payload, rotate_bytes=rotate_bytes)

    return _audit


@dataclass
class VerifyReport:
    path: str
    total: int = 0
    chained: int = 0
    unchained: int = 0
    unchained_ranges: list[list[int]] = field(default_factory=list)
    first_break: int | None = None
    first_break_reason: str | None = None
    ok: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "total": self.total,
            "chained": self.chained,
            "unchained": self.unchained,
            "unchained_ranges": list(self.unchained_ranges),
            "first_break": self.first_break,
            "first_break_reason": self.first_break_reason,
            "ok": self.ok,
        }


def verify_audit_file(path: Path | str) -> VerifyReport:
    """Walk one JSONL chain. Unchained ranges are reported; a break fails."""
    file = Path(path)
    report = VerifyReport(path=str(file))
    if not file.is_file():
        report.ok = False
        report.first_break_reason = "file not found"
        return report
    raw_lines = file.read_text(encoding="utf-8").splitlines()
    prev_hash = GENESIS_HASH
    prev_seq = 0
    unchained_start: int | None = None
    for index, raw in enumerate(raw_lines, start=1):
        if not raw.strip():
            continue
        report.total += 1
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            report.ok = False
            report.first_break = index
            report.first_break_reason = f"truncated or invalid JSON at line {index}"
            _close_unchained(report, unchained_start, report.total - 1)
            return report
        if not isinstance(row, dict):
            report.ok = False
            report.first_break = index
            report.first_break_reason = f"non-object entry at line {index}"
            _close_unchained(report, unchained_start, report.total - 1)
            return report
        chained = _is_chained(row)
        if not chained:
            report.unchained += 1
            if unchained_start is None:
                unchained_start = report.total
            continue
        if unchained_start is not None:
            _close_unchained(report, unchained_start, report.total - 1)
            unchained_start = None
        seq = int(row["seq"])
        expected = compute_entry_hash(row)
        if row.get("entry_hash") != expected:
            report.ok = False
            report.first_break = seq
            report.first_break_reason = f"entry_hash mismatch at seq {seq}"
            return report
        if report.chained == 0:
            if row.get("event") != "chain_anchor" and row.get("prev_hash") != prev_hash:
                report.ok = False
                report.first_break = seq
                report.first_break_reason = f"prev_hash mismatch at seq {seq}"
                return report
        elif row.get("prev_hash") != prev_hash:
            report.ok = False
            report.first_break = seq
            report.first_break_reason = f"prev_hash mismatch at seq {seq}"
            return report
        if prev_seq and seq != prev_seq + 1:
            report.ok = False
            report.first_break = seq
            report.first_break_reason = f"sequence gap at seq {seq} (prev {prev_seq})"
            return report
        report.chained += 1
        prev_hash = str(row["entry_hash"])
        prev_seq = seq
    _close_unchained(report, unchained_start, report.total)
    return report


def verify_audit_dir(audit_dir: Path | str) -> list[VerifyReport]:
    folder = Path(audit_dir)
    if not folder.is_dir():
        return []
    reports = [verify_audit_file(path) for path in sorted(folder.glob("*.jsonl"))]
    return reports


def _is_chained(row: Mapping[str, Any]) -> bool:
    return "seq" in row and "prev_hash" in row and "entry_hash" in row


def _close_unchained(report: VerifyReport, start: int | None, end: int) -> None:
    if start is None or end < start:
        return
    report.unchained_ranges.append([start, end])


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def _read_file_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            events.append(row)
    return events


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
        if _is_chained(row):
            last_seq = int(row["seq"])
            last_hash = str(row["entry_hash"])
        else:
            last_seq = max(last_seq, count)
    return last_seq, last_hash


def _rotate_if_needed(path: Path, rotate_bytes: int) -> None:
    if not path.is_file() or path.stat().st_size < rotate_bytes:
        return
    n = 1
    while True:
        dest = path.with_name(f"{path.stem}.{n}{path.suffix}")
        if not dest.exists():
            break
        n += 1
    last_seq, last_hash = _tail_chain(path)
    path.replace(dest)
    anchor = {
        "event": "chain_anchor",
        "run_id": path.stem.split(".", 1)[0],
        "prev_file": dest.name,
        "prev_seq": last_seq,
        "ts": utc_now(),
        "seq": 1,
        "prev_hash": last_hash,
    }
    anchor["entry_hash"] = compute_entry_hash(anchor)
    line = json.dumps(anchor, ensure_ascii=False, default=str) + "\n"
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    fd = os.open(path, flags, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
