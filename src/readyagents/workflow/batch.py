"""Foreground batch runner: one workflow, many input rows.

A failed row is recorded and does not stop the batch unless
``continue_on_error`` is false. Per-row isolation is a fresh
``run_workflow_file`` call (own run id, inputs, taint, spend snapshot).
The command is foreground and ends — no always-on worker.

Results JSONL is sorted by row index. Progress is streamed as rows complete
(unordered). JSON is the low-concurrency default for ``readyagents run``;
batch defaults to the SQLite run store when persisting.
"""

from __future__ import annotations

import csv
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from readyagents.errors import (
    ApprovalRequired,
    BudgetExceeded,
    CancellationRequested,
    ConfigError,
    GovernorShutdown,
    ReadyAgentsError,
)
from readyagents.llm.resilience import usd_to_micros
from readyagents.workflow.cancellation import CancellationToken
from readyagents.workflow.governor import ConcurrencyGovernor, RunPriority, get_governor
from readyagents.workflow.runner import load_workflow, run_workflow_file
from readyagents.workflow.state import RunState

_JSONL_SUFFIXES = {".jsonl", ".json"}
_CSV_SUFFIXES = {".csv"}


@dataclass
class BatchRowResult:
    index: int
    status: str
    run_id: str | None = None
    error: str | None = None
    error_type: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] | None = None

    def as_record(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "status": self.status,
            "run_id": self.run_id,
            "error": self.error,
            "error_type": self.error_type,
            "inputs": dict(self.inputs),
            "outputs": dict(self.outputs) if self.outputs is not None else None,
        }

    def as_summary_row(self) -> dict[str, Any]:
        """Compact row for ``--json``: no inputs/outputs (secrets stay in the JSONL)."""
        return {
            "index": self.index,
            "status": self.status,
            "run_id": self.run_id,
            "error": self.error,
            "error_type": self.error_type,
        }


@dataclass
class BatchReport:
    workflow: str
    total: int
    succeeded: int = 0
    failed: int = 0
    paused: int = 0
    skipped: int = 0
    cancelled: int = 0
    concurrency: int = 1
    max_spend: float | None = None
    spend_micros: int = 0
    out: str | None = None
    results: list[BatchRowResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow,
            "total": self.total,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "paused": self.paused,
            "skipped": self.skipped,
            "cancelled": self.cancelled,
            "concurrency": self.concurrency,
            "max_spend": self.max_spend,
            "spend_usd": self.spend_micros / 1_000_000 if self.spend_micros else 0.0,
            "out": self.out,
            "rows": [row.as_summary_row() for row in self.results],
        }


def load_input_rows(path: Path | str) -> list[dict[str, Any]]:
    file = Path(path)
    if not file.is_file():
        raise ConfigError(f"Input file not found: {file}")
    suffix = file.suffix.lower()
    text = file.read_text(encoding="utf-8")
    if text.startswith("\ufeff"):
        text = text[1:]
    if suffix in _CSV_SUFFIXES:
        return _load_csv(text, file)
    if suffix in _JSONL_SUFFIXES or suffix == "":
        return _load_jsonl(text, file)
    raise ConfigError(f"Input file must be JSONL or CSV: {file}")


def _load_jsonl(text: str, file: Path) -> list[dict[str, Any]]:
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Could not parse {file}: {exc}") from exc
        if not isinstance(data, list):
            raise ConfigError(f"JSON input {file} must be a list of objects")
        rows: list[dict[str, Any]] = []
        for i, item in enumerate(data):
            rows.append(_row_mapping(item, file, i))
        return rows
    rows = []
    for i, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as extra:
            raise ConfigError(f"Could not parse {file} line {i + 1}: {extra}") from extra
        rows.append(_row_mapping(item, file, i))
    if not rows:
        raise ConfigError(f"Input file is empty: {file}")
    return rows


def _load_csv(text: str, file: Path) -> list[dict[str, Any]]:
    reader = csv.DictReader(text.splitlines())
    if reader.fieldnames is None:
        raise ConfigError(f"CSV input {file} has no header row")
    rows: list[dict[str, Any]] = []
    for raw in reader:
        item = {str(k): _csv_value(v) for k, v in raw.items() if k is not None}
        rows.append(item)
    if not rows:
        raise ConfigError(f"Input file is empty: {file}")
    return rows


def _csv_value(raw: str | None) -> Any:
    if raw is None:
        return ""
    text = str(raw).strip()
    if text == "":
        return ""
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        return text


def _row_mapping(item: Any, file: Path, index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ConfigError(f"{file} row {index} must be a JSON object")
    nested = item.get("inputs")
    if isinstance(nested, dict) and all(isinstance(k, str) for k in nested):
        return dict(nested)
    return {str(k): v for k, v in item.items()}


def run_batch(
    path: Path | str,
    rows: Sequence[Mapping[str, Any]] | Path | str,
    *,
    concurrency: int = 8,
    continue_on_error: bool = True,
    max_spend: float | None = None,
    out: Path | str | None = None,
    governor: ConcurrencyGovernor | None = None,
    persist: bool = True,
    store: Any = None,
    settings: Any = None,
    on_progress: Callable[[BatchRowResult, int, int], None] | None = None,
    cancellation: CancellationToken | None = None,
    run_kwargs: Mapping[str, Any] | None = None,
) -> BatchReport:
    source = Path(path)
    workflow = load_workflow(source)
    loaded = load_input_rows(rows) if isinstance(rows, (str, Path)) else [dict(r) for r in rows]
    if not loaded:
        raise ConfigError("batch requires at least one input row")
    limit = max(1, int(concurrency))
    if governor is None:
        governor = get_governor()
    limit = min(limit, governor.global_limit, governor.max_concurrency)
    token = cancellation or CancellationToken()
    governor.on_shutdown(lambda: token.request(reason="batch shutdown"))
    shared = None
    if max_spend is not None:
        from readyagents.cost.meter import SharedBudget

        micros = usd_to_micros(max_spend)
        shared = SharedBudget(int(micros or 0))
    extra = dict(run_kwargs or {})
    extra.setdefault("persist", persist)
    if store is not None:
        extra["store"] = store
    if settings is not None:
        extra["settings"] = settings
    extra["governor"] = governor
    extra["priority"] = RunPriority.BATCH
    extra["cancellation"] = token
    if shared is not None:
        extra["shared_budget"] = shared
        extra.setdefault("max_spend", max_spend)

    total = len(loaded)
    results: list[BatchRowResult | None] = [None] * total
    stop_submit = threading.Event()

    def _run_one(index: int, inputs: dict[str, Any]) -> BatchRowResult:
        if token.is_requested() or governor.is_shutdown() or stop_submit.is_set():
            return BatchRowResult(
                index=index,
                status="cancelled" if token.is_requested() or governor.is_shutdown() else "skipped",
                inputs=dict(inputs),
                error="batch stopped",
                error_type="stopped",
            )
        try:
            with governor.acquire(
                workflow=workflow.name,
                provider=_provider_of(extra, workflow),
                priority=RunPriority.BATCH,
            ):
                if token.is_requested() or governor.is_shutdown():
                    return BatchRowResult(
                        index=index,
                        status="cancelled",
                        inputs=dict(inputs),
                        error="batch shutdown",
                        error_type="GovernorShutdown",
                    )
                state = run_workflow_file(source, inputs=dict(inputs), **extra)
                return _from_state(index, inputs, state)
        except ApprovalRequired as exc:
            state = getattr(exc, "state", None)
            return BatchRowResult(
                index=index,
                status="paused",
                run_id=exc.run_id,
                inputs=dict(inputs),
                error=str(exc),
                error_type=type(exc).__name__,
                outputs=_outputs(state) if isinstance(state, RunState) else None,
            )
        except GovernorShutdown as exc:
            return BatchRowResult(
                index=index,
                status="cancelled",
                inputs=dict(inputs),
                error=str(exc),
                error_type=type(exc).__name__,
            )
        except (BudgetExceeded, CancellationRequested, ReadyAgentsError) as exc:
            state = getattr(exc, "state", None)
            status = "cancelled" if isinstance(exc, CancellationRequested) else "failed"
            if isinstance(state, RunState) and state.status in {"failed", "cancelled", "paused"}:
                status = state.status
            return BatchRowResult(
                index=index,
                status=status,
                run_id=getattr(exc, "run_id", None)
                or (state.run_id if isinstance(state, RunState) else None),
                inputs=dict(inputs),
                error=str(exc),
                error_type=type(exc).__name__,
                outputs=_outputs(state) if isinstance(state, RunState) else None,
            )
        except Exception as exc:  # noqa: BLE001
            return BatchRowResult(
                index=index,
                status="failed",
                inputs=dict(inputs),
                error=str(exc),
                error_type=type(exc).__name__,
            )

    next_i = 0
    in_flight: set[Any] = set()
    with ThreadPoolExecutor(max_workers=limit, thread_name_prefix="ra-batch") as pool:

        def _submit_more() -> None:
            nonlocal next_i
            while next_i < total and len(in_flight) < limit:
                if stop_submit.is_set() or governor.is_shutdown() or token.is_requested():
                    return
                idx = next_i
                next_i += 1
                fut = pool.submit(_run_one, idx, dict(loaded[idx]))
                in_flight.add(fut)

        _submit_more()
        while in_flight:
            done, pending = wait(in_flight, return_when=FIRST_COMPLETED)
            in_flight = set(pending)
            for fut in done:
                row = fut.result()
                results[row.index] = row
                if row.status == "failed" and not continue_on_error:
                    stop_submit.set()
                if on_progress is not None:
                    finished = sum(1 for item in results if item is not None)
                    on_progress(row, finished, total)
            _submit_more()

    for idx in range(next_i, total):
        if results[idx] is None:
            results[idx] = BatchRowResult(
                index=idx,
                status="skipped",
                inputs=dict(loaded[idx]),
                error="not started",
                error_type="skipped",
            )

    ordered = [item for item in results if item is not None]
    report = BatchReport(
        workflow=workflow.name,
        total=total,
        concurrency=limit,
        max_spend=max_spend,
        spend_micros=shared.cost_micros if shared is not None else 0,
        out=str(out) if out is not None else None,
        results=ordered,
    )
    for row in ordered:
        if row.status == "succeeded":
            report.succeeded += 1
        elif row.status == "paused":
            report.paused += 1
        elif row.status == "cancelled":
            report.cancelled += 1
        elif row.status == "skipped":
            report.skipped += 1
        else:
            report.failed += 1
    if out is not None:
        _write_results(Path(out), ordered)
    return report


def write_results_jsonl(path: Path | str, rows: Sequence[BatchRowResult]) -> None:
    _write_results(Path(path), rows)


def _write_results(path: Path, rows: Sequence[BatchRowResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        _dump_rows(handle, rows)


def _dump_rows(handle: TextIO, rows: Sequence[BatchRowResult]) -> None:
    for row in sorted(rows, key=lambda item: item.index):
        handle.write(json.dumps(row.as_record(), ensure_ascii=False) + "\n")


def _from_state(index: int, inputs: Mapping[str, Any], state: RunState) -> BatchRowResult:
    status = state.status or "failed"
    error = None
    error_type = None
    if status != "succeeded":
        error = "; ".join(state.errors) if state.errors else status
        error_type = status
    return BatchRowResult(
        index=index,
        status=status,
        run_id=state.run_id,
        inputs=dict(inputs),
        outputs=_outputs(state),
        error=error,
        error_type=error_type,
    )


def _outputs(state: RunState) -> dict[str, Any]:
    if state.output_keys:
        return dict(state.output_keys)
    return dict(state.node_outputs or {})


def _provider_of(run_kwargs: Mapping[str, Any], workflow: Any = None) -> str | None:
    """Provider key for governor acquire. CLI batch has no llm in run_kwargs."""
    llm = run_kwargs.get("llm")
    name = getattr(llm, "name", None)
    if name:
        return str(name).strip().lower() or None
    ref = ""
    if workflow is not None:
        ref = str(getattr(workflow, "default_model", None) or "").strip()
    if not ref:
        settings = run_kwargs.get("settings")
        if settings is None:
            from readyagents.config import get_settings

            settings = get_settings()
        ref = str(getattr(settings, "default_model", "") or "").strip()
    if not ref:
        return None
    from readyagents.llm.base import parse_model_ref

    try:
        return parse_model_ref(ref)[0]
    except ValueError:
        return None
