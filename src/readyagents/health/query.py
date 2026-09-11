"""Bounded health query over the existing run store. No new store, no daemon."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from readyagents.errors import ConfigError
from readyagents.health.cluster import FailureCluster, add_observation, rank_clusters
from readyagents.health.fingerprint import fingerprint_from_result
from readyagents.health.flaky import classify_stability, input_digest
from readyagents.health.layout import DEFAULT_LIMIT, DEFAULT_WINDOW, HARD_MAX_RUNS, HARD_MAX_WINDOW
from readyagents.health.score import NodeHealth, score_node
from readyagents.run_store.base import RunQuery, RunStore
from readyagents.workflow.state import RunState


@dataclass
class WorkflowHealth:
    name: str
    runs: int = 0
    succeeded: int = 0
    failed: int = 0
    success_rate: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "runs": self.runs,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "success_rate": self.success_rate,
        }


@dataclass
class HealthReport:
    clusters: list[FailureCluster] = field(default_factory=list)
    nodes: list[NodeHealth] = field(default_factory=list)
    workflows: list[WorkflowHealth] = field(default_factory=list)
    scanned: int = 0
    truncated: bool = False
    window: int = DEFAULT_WINDOW
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "clusters": [row.as_dict() for row in self.clusters],
            "nodes": [row.as_dict() for row in self.nodes],
            "workflows": [row.as_dict() for row in self.workflows],
            "scanned": self.scanned,
            "truncated": self.truncated,
            "window": self.window,
            "limit": self.limit,
            "cursor": self.cursor,
        }


def query_health(
    store: RunStore,
    *,
    workflow: str | None = None,
    window: int = DEFAULT_WINDOW,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    secrets: list[str] | None = None,
    redactor: Any = None,
    fixtures: dict[str, str] | None = None,
) -> HealthReport:
    window = _bound(window, HARD_MAX_WINDOW, DEFAULT_WINDOW)
    limit = _bound(limit, HARD_MAX_RUNS, DEFAULT_LIMIT)
    cap = min(HARD_MAX_RUNS, limit)
    fetched = store.list(RunQuery(workflow=workflow, limit=cap + 1, cursor=cursor))
    truncated = len(fetched) > cap
    rows = fetched[:cap]
    buckets: dict[str, FailureCluster] = {}
    by_node: dict[str, list[dict[str, Any]]] = {}
    by_workflow: dict[str, list[str]] = {}
    flaky_obs: dict[str, list[tuple[str, bool]]] = {}
    for stored in rows:
        state = stored.state
        name = state.workflow_name
        by_workflow.setdefault(name, []).append(state.status)
        cost = int((state.usage or {}).get("cost_micros") or 0)
        seen = state.finished_at or state.started_at or ""
        for result in state.results:
            node_id = str(result.node_id)
            ok = result.status == "ok" and not result.error
            if result.status == "quarantined":
                ok = False
            sample = {
                "ok": ok,
                "attempts": int(result.attempts or 1),
                "latency_ms": _latency_ms(result),
                "cost_micros": int((result.usage or {}).get("cost_micros") or 0),
            }
            by_node.setdefault(node_id, []).append(sample)
            digest = input_digest(state, node_id)
            if digest:
                flaky_obs.setdefault(node_id, []).append((digest, ok))
            if ok:
                continue
            fp = fingerprint_from_result(result, secrets=secrets, redactor=redactor)
            if fp is None:
                continue
            add_observation(
                buckets,
                fp,
                run_id=state.run_id,
                seen_at=seen,
                cost_micros=cost if state.status in {"failed", "error"} else 0,
            )
    clusters = rank_clusters(list(buckets.values()))
    if fixtures:
        for cluster in clusters:
            cluster.fixture = fixtures.get(cluster.fingerprint.id)
    nodes = [
        _with_stability(score_node(samples, node_id=nid, window=window), flaky_obs.get(nid) or [])
        for nid, samples in sorted(by_node.items())
    ]
    workflows = [_workflow_health(name, statuses) for name, statuses in sorted(by_workflow.items())]
    next_cursor = rows[-1].state.run_id if truncated and rows else None
    return HealthReport(
        clusters=clusters,
        nodes=nodes,
        workflows=workflows,
        scanned=len(rows),
        truncated=truncated,
        window=window,
        limit=limit,
        cursor=next_cursor,
    )


def node_samples(
    store: RunStore,
    *,
    workflow: str,
    node_id: str,
    window: int,
    exclude_run_id: str | None = None,
) -> list[dict[str, Any]]:
    window = _bound(window, HARD_MAX_WINDOW, DEFAULT_WINDOW)
    fetched = store.list(RunQuery(workflow=workflow, limit=HARD_MAX_RUNS))
    out: list[dict[str, Any]] = []
    for stored in fetched:
        state = stored.state
        if exclude_run_id and state.run_id == exclude_run_id:
            continue
        for result in state.results:
            if str(result.node_id) != node_id:
                continue
            out.append(
                {
                    "ok": result.status == "ok" and not result.error,
                    "attempts": int(result.attempts or 1),
                    "latency_ms": _latency_ms(result),
                    "cost_micros": int((result.usage or {}).get("cost_micros") or 0),
                    "run_id": state.run_id,
                }
            )
        if len(out) >= window:
            break
    return out[:window]


def find_runs_for_fingerprint(
    store: RunStore,
    fingerprint_id: str,
    *,
    workflow: str | None = None,
    secrets: list[str] | None = None,
    redactor: Any = None,
) -> list[RunState]:
    fetched = store.list(RunQuery(workflow=workflow, limit=HARD_MAX_RUNS))
    hits: list[RunState] = []
    for stored in fetched:
        for result in stored.state.results:
            fp = fingerprint_from_result(result, secrets=secrets, redactor=redactor)
            if fp is not None and fp.id == fingerprint_id:
                hits.append(stored.state)
                break
    return hits


def _with_stability(health: NodeHealth, observations: list[tuple[str, bool]]) -> NodeHealth:
    label = classify_stability(observations) if observations else ""
    health.flaky = label == "flaky"
    health.broken = label == "broken" or (health.broken and not health.flaky)
    if health.flaky:
        health.broken = False
    return health


def _workflow_health(name: str, statuses: list[str]) -> WorkflowHealth:
    total = len(statuses)
    succeeded = sum(1 for item in statuses if item == "succeeded")
    failed = sum(1 for item in statuses if item in {"failed", "error"})
    rate = (succeeded / total) if total else 0.0
    return WorkflowHealth(
        name=name,
        runs=total,
        succeeded=succeeded,
        failed=failed,
        success_rate=round(rate, 6),
    )


def _latency_ms(result: Any) -> float:
    total = getattr(result, "total_ms", None)
    if total is not None:
        return float(total)
    return 0.0


def _bound(value: int, hard: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError("health window/limit must be an integer") from exc
    if number <= 0:
        return default
    return min(number, hard)
