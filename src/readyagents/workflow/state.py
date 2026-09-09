"""Immutable-ish run state and persisted run records."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from readyagents.errors import ConfigError, WorkflowError


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class NodeResult:
    node_id: str
    type: str
    status: str
    output: Any = None
    error: str | None = None
    attempts: int = 1
    started_at: str = ""
    finished_at: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    tool_rounds: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RunState:
    """Inputs + per-node outputs. Treat as append-only during a run."""

    run_id: str
    workflow_name: str
    inputs: dict[str, Any]
    node_outputs: dict[str, Any] = field(default_factory=dict)
    output_keys: dict[str, Any] = field(default_factory=dict)
    results: list[NodeResult] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    status: str = "running"
    pending_node: str | None = None
    pending: dict[str, Any] | None = None
    usage: dict[str, int] = field(default_factory=dict)
    provenance: dict[str, dict[str, Any]] = field(default_factory=dict)
    started_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    _last_node_usage: dict[str, int] = field(default_factory=dict, repr=False, compare=False)
    _usage_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @classmethod
    def start(
        cls,
        workflow_name: str,
        inputs: Mapping[str, Any],
        *,
        metadata: Mapping[str, Any] | None = None,
        run_id: str | None = None,
    ) -> RunState:
        return cls(
            run_id=run_id or uuid4().hex,
            workflow_name=workflow_name,
            inputs=dict(inputs),
            metadata=dict(metadata or {}),
        )

    def mapping(self) -> dict[str, Any]:
        """Template namespace: inputs, node ids, output keys, metadata."""
        ns: dict[str, Any] = {}
        ns.update(self.metadata)
        ns.update(self.inputs)
        ns.update(self.node_outputs)
        ns.update(self.output_keys)
        ns["inputs"] = self.inputs
        ns["outputs"] = self.node_outputs
        ns["run_id"] = self.run_id
        return ns

    def record(
        self,
        node_id: str,
        output: Any,
        *,
        node_type: str,
        output_key: str | None = None,
        attempts: int = 1,
        started_at: str = "",
        finished_at: str = "",
        usage: Mapping[str, int] | None = None,
        tool_rounds: list[dict[str, Any]] | None = None,
    ) -> None:
        self.node_outputs[node_id] = output
        if output_key:
            self.output_keys[output_key] = output
        self.results.append(
            NodeResult(
                node_id=node_id,
                type=node_type,
                status="ok",
                output=_jsonable(output),
                attempts=attempts,
                started_at=started_at,
                finished_at=finished_at,
                usage={str(k): int(v) for k, v in dict(usage or {}).items()},
                tool_rounds=[dict(row) for row in (tool_rounds or [])],
            )
        )

    def record_error(
        self, node_id: str, node_type: str, message: str, *, attempts: int = 1
    ) -> None:
        self.errors.append(message)
        self.results.append(
            NodeResult(
                node_id=node_id,
                type=node_type,
                status="error",
                error=message,
                attempts=attempts,
                finished_at=utc_now(),
            )
        )

    def finish(self, status: str) -> None:
        self.status = status
        self.finished_at = utc_now()

    def add_usage(self, **amounts: Any) -> None:
        cleaned = _clean_usage(amounts)
        if not cleaned:
            return
        with self._usage_lock:
            _add_usage_into(self.usage, cleaned)

    def note_node_usage(self, amounts: Mapping[str, Any], *, rollup: bool = True) -> None:
        """Attach usage to the current node. Accumulates so parallel branches merge."""
        cleaned = _clean_usage(amounts)
        if not cleaned:
            return
        with self._usage_lock:
            _add_usage_into(self._last_node_usage, cleaned)
            if rollup:
                _add_usage_into(self.usage, cleaned)

    def take_node_usage(self) -> dict[str, int]:
        with self._usage_lock:
            usage = dict(self._last_node_usage)
            self._last_node_usage = {}
            return usage

    def node_usage_map(self) -> dict[str, dict[str, int]]:
        return {r.node_id: dict(r.usage) for r in self.results if r.usage}

    def to_record(self) -> dict[str, Any]:
        return {
            "record_version": 1,
            "run_id": self.run_id,
            "workflow": self.workflow_name,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "pending_node": self.pending_node,
            "pending": _jsonable(self.pending) if self.pending else None,
            "inputs": _jsonable(self.inputs),
            "outputs": _jsonable(self.output_keys or self.node_outputs),
            "output_keys": _jsonable(self.output_keys),
            "node_outputs": _jsonable(self.node_outputs),
            "node_results": [
                {
                    "node_id": r.node_id,
                    "type": r.type,
                    "status": r.status,
                    "output": r.output,
                    "error": r.error,
                    "attempts": r.attempts,
                    "started_at": r.started_at,
                    "finished_at": r.finished_at,
                    "usage": dict(r.usage),
                    "tool_rounds": list(r.tool_rounds),
                }
                for r in self.results
            ],
            "metadata": _jsonable(self.metadata),
            "errors": list(self.errors),
            "usage": dict(self.usage),
            "provenance": _jsonable(self.provenance) if self.provenance else {},
        }

    @classmethod
    def from_record(cls, data: Mapping[str, Any]) -> RunState:
        results = [
            NodeResult(
                node_id=str(row.get("node_id", "")),
                type=str(row.get("type", "")),
                status=str(row.get("status", "")),
                output=row.get("output"),
                error=row.get("error"),
                attempts=int(row.get("attempts") or 1),
                started_at=str(row.get("started_at") or ""),
                finished_at=str(row.get("finished_at") or ""),
                usage={
                    str(k): int(v)
                    for k, v in dict(row.get("usage") or {}).items()
                    if _is_intlike(v)
                },
                tool_rounds=[
                    dict(item)
                    for item in (row.get("tool_rounds") or [])
                    if isinstance(item, Mapping)
                ],
            )
            for row in data.get("node_results") or []
            if isinstance(row, Mapping)
        ]
        pending_raw = data.get("pending")
        pending = dict(pending_raw) if isinstance(pending_raw, Mapping) else None
        return cls(
            run_id=str(data.get("run_id") or ""),
            workflow_name=str(data.get("workflow") or data.get("workflow_name") or ""),
            inputs=dict(data.get("inputs") or {}),
            node_outputs=dict(data.get("node_outputs") or {}),
            output_keys=dict(data.get("output_keys") or data.get("outputs") or {}),
            results=results,
            metadata=dict(data.get("metadata") or {}),
            errors=list(data.get("errors") or []),
            status=str(data.get("status") or "running"),
            pending_node=data.get("pending_node"),
            pending=pending,
            usage={str(k): int(v) for k, v in dict(data.get("usage") or {}).items()},
            provenance={
                str(k): dict(v)
                for k, v in dict(data.get("provenance") or {}).items()
                if isinstance(v, Mapping)
            },
            started_at=str(data.get("started_at") or utc_now()),
            finished_at=data.get("finished_at"),
        )


def persist_run(state: RunState, runs_dir: Path, *, redactor: Any | None = None) -> Path:
    """Atomically write `<run_id>.json` (temp file + os.replace)."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{state.run_id}.json"
    record_obj: Any = state.to_record()
    if redactor is not None:
        record_obj = redactor.redact(record_obj)
    record = json.dumps(record_obj, indent=2, ensure_ascii=False) + "\n"
    from readyagents.atomic import atomic_write_text

    atomic_write_text(path, record, encoding="utf-8", newline="\n")
    return path


def delete_run(runs_dir: Path, run_id: str) -> Path:
    """Remove one run JSON file. ``run_id`` may be a unique prefix."""
    state = load_run(runs_dir, run_id)
    path = Path(runs_dir) / f"{state.run_id}.json"
    if not path.is_file():
        raise ConfigError(f"Run not found: {run_id}")
    path.unlink()
    return path


def gc_runs(
    runs_dir: Path,
    *,
    statuses: list[str] | None = None,
    include_paused: bool = False,
    keep: int = 0,
    min_age_seconds: float | None = None,
    override_retention: bool = False,
) -> list[str]:
    """Delete local run files. Never deletes ``paused`` unless ``include_paused``."""
    wanted = {s.strip().lower() for s in (statuses or ["succeeded", "failed", "cancelled"])}
    if include_paused:
        wanted.add("paused")
    found = list_runs(runs_dir)
    if keep and keep > 0:
        found = found[keep:]
    deleted: list[str] = []
    for state in found:
        if state.status == "paused" and not include_paused:
            continue
        if state.status not in wanted:
            continue
        if in_retention_window(state, min_age_seconds) and not override_retention:
            continue
        path = Path(runs_dir) / f"{state.run_id}.json"
        if path.is_file():
            path.unlink()
            deleted.append(state.run_id)
    return deleted


def in_retention_window(state: RunState, min_age_seconds: float | None) -> bool:
    """True when the record is younger than the configured retention window."""
    if not min_age_seconds:
        return False
    stamp = state.finished_at or state.started_at
    if not stamp:
        return True
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - dt).total_seconds()
    return age < float(min_age_seconds)


def mark_cancelled(state: RunState) -> RunState:
    """Mark a run cancelled. Does not persist."""
    state.finish("cancelled")
    return state


def load_run(runs_dir: Path, run_id: str) -> RunState:
    """Load a persisted run. `run_id` may be a unique prefix."""
    runs_dir = Path(runs_dir)
    exact = runs_dir / f"{run_id}.json"
    if exact.is_file():
        return _read_run(exact)
    matches = sorted(runs_dir.glob(f"{run_id}*.json")) if runs_dir.is_dir() else []
    if len(matches) == 1:
        return _read_run(matches[0])
    if len(matches) > 1:
        ids = ", ".join(p.stem for p in matches[:8])
        raise ConfigError(f"Run id '{run_id}' is ambiguous. Matches: {ids}")
    raise ConfigError(f"Run not found: {run_id}")


def list_runs(
    runs_dir: Path,
    *,
    status: str | None = None,
    workflow: str | None = None,
    limit: int = 0,
) -> list[RunState]:
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return []
    states: list[RunState] = []
    for path in runs_dir.glob("*.json"):
        if path.name.startswith("."):
            continue
        try:
            states.append(_read_run(path))
        except (OSError, json.JSONDecodeError, ConfigError, TypeError, ValueError):
            continue
    states.sort(key=lambda s: s.started_at, reverse=True)
    if status:
        wanted = status.strip().lower()
        states = [s for s in states if s.status == wanted]
    if workflow:
        wanted_wf = workflow.strip()
        states = [s for s in states if s.workflow_name == wanted_wf]
    if limit and limit > 0:
        states = states[:limit]
    return states


def _read_run(path: Path) -> RunState:
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Corrupt run record {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read run record {path}: {exc}") from exc
    if not isinstance(data, dict) or not data.get("run_id"):
        raise ConfigError(f"Invalid run record: {path}")
    return RunState.from_record(data)


def build_decisions(approve: list[str], reject: list[str]) -> dict[str, str]:
    decisions: dict[str, str] = {}
    for node_id in approve:
        decisions[node_id] = "approve"
    for node_id in reject:
        decisions[node_id] = "reject"
    return decisions


def parse_decision_payload(data: Any) -> dict[str, str]:
    """Accept a few JSON shapes used by external decision injection."""
    if data is None:
        return {}
    if isinstance(data, Mapping):
        if "decisions" in data and isinstance(data["decisions"], Mapping):
            return {str(k): str(v).strip().lower() for k, v in data["decisions"].items()}
        if "decisions" in data and isinstance(data["decisions"], list):
            return parse_decision_payload(data["decisions"])
        node = data.get("node_id") or data.get("node") or data.get("id")
        decision = data.get("decision") or data.get("value")
        if node and decision is not None:
            return {str(node): str(decision).strip().lower()}
        return {str(k): str(v).strip().lower() for k, v in data.items() if v is not None}
    if isinstance(data, list):
        merged: dict[str, str] = {}
        for item in data:
            merged.update(parse_decision_payload(item))
        return merged
    raise WorkflowError("Decision payload must be a JSON object or list")


def load_decision_file(path: Path | str) -> dict[str, str]:
    file = Path(path)
    if not file.is_file():
        from readyagents.errors import ConfigError

        raise ConfigError(f"Decision file not found: {file}")
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"Decision file {file} is not valid JSON: {exc}") from exc
    return parse_decision_payload(data)


def _clean_usage(amounts: Mapping[str, Any] | None) -> dict[str, int]:
    cleaned: dict[str, int] = {}
    for key, raw in dict(amounts or {}).items():
        if raw is None:
            continue
        try:
            cleaned[str(key)] = int(raw)
        except (TypeError, ValueError):
            continue
    return cleaned


def _add_usage_into(target: dict[str, int], amounts: Mapping[str, int]) -> None:
    for key, value in amounts.items():
        target[key] = int(target.get(key, 0)) + int(value)


def _is_intlike(value: Any) -> bool:
    try:
        int(value)
        return True
    except (TypeError, ValueError):
        return False


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def parse_input_pairs(pairs: list[str]) -> dict[str, Any]:
    """Parse CLI `--input KEY=VALUE` pairs."""
    result: dict[str, Any] = {}
    for raw in pairs:
        if "=" not in raw:
            raise WorkflowError(f"Invalid --input '{raw}' (expected KEY=VALUE)")
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise WorkflowError(f"Invalid --input '{raw}' (empty key)")
        result[key] = _coerce_scalar(value)
    return result


def _coerce_scalar(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null" or lowered == "none":
        return None
    try:
        if value.strip().isdigit() or (
            value.strip().startswith("-") and value.strip()[1:].isdigit()
        ):
            return int(value.strip())
        return float(value) if "." in value.strip() else value
    except ValueError:
        return value
