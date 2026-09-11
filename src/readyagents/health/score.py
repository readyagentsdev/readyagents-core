"""Per-node health score over a declared window. No prediction."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import pstdev
from typing import Any


@dataclass
class NodeHealth:
    node_id: str
    window: int
    samples: int
    successes: int
    failures: int
    success_rate: float
    retry_rate: float
    latency_variance: float
    cost_variance: float
    score: float
    flaky: bool = False
    broken: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "window": self.window,
            "samples": self.samples,
            "successes": self.successes,
            "failures": self.failures,
            "success_rate": self.success_rate,
            "retry_rate": self.retry_rate,
            "latency_variance": self.latency_variance,
            "cost_variance": self.cost_variance,
            "score": self.score,
            "flaky": self.flaky,
            "broken": self.broken,
        }


def score_node(
    samples: list[dict[str, Any]],
    *,
    node_id: str,
    window: int,
) -> NodeHealth:
    """Combine success rate, retry rate, latency variance, and cost variance.

    ``samples`` is newest-first (store order). The first ``window`` items are
    used. Score is in [0, 1]; 1 is healthy. No prediction.
    """
    window = max(1, int(window))
    rows = list(samples)[:window]
    n = len(rows)
    if n == 0:
        return NodeHealth(
            node_id=node_id,
            window=window,
            samples=0,
            successes=0,
            failures=0,
            success_rate=0.0,
            retry_rate=0.0,
            latency_variance=0.0,
            cost_variance=0.0,
            score=0.0,
        )
    successes = sum(1 for row in rows if row.get("ok"))
    failures = n - successes
    success_rate = successes / n
    retries = 0
    for row in rows:
        attempts = int(row.get("attempts") or 1)
        if attempts > 1:
            retries += 1
    retry_rate = retries / n
    latencies = [float(row.get("latency_ms") or 0) for row in rows]
    costs = [float(row.get("cost_micros") or 0) for row in rows]
    latency_variance = _coeff_var(latencies)
    cost_variance = _coeff_var(costs)
    score = success_rate
    score *= max(0.0, 1.0 - 0.3 * retry_rate)
    score *= max(0.0, 1.0 - 0.2 * min(1.0, latency_variance))
    score *= max(0.0, 1.0 - 0.2 * min(1.0, cost_variance))
    return NodeHealth(
        node_id=node_id,
        window=window,
        samples=n,
        successes=successes,
        failures=failures,
        success_rate=round(success_rate, 6),
        retry_rate=round(retry_rate, 6),
        latency_variance=round(latency_variance, 6),
        cost_variance=round(cost_variance, 6),
        score=round(score, 6),
        broken=failures == n and n >= 2,
    )


def _coeff_var(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    if mean == 0:
        return 0.0
    return float(pstdev(values) / abs(mean))
