"""Cluster failures by fingerprint and rank by impact (count + cost burned)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from readyagents.health.fingerprint import FailureFingerprint


@dataclass
class FailureCluster:
    fingerprint: FailureFingerprint
    count: int = 0
    run_ids: list[str] = field(default_factory=list)
    first_seen: str = ""
    last_seen: str = ""
    cost_micros: int = 0
    node_id: str = ""
    fixture: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.fingerprint.id,
            "class": self.fingerprint.klass,
            "node_id": self.node_id or self.fingerprint.node_id,
            "count": self.count,
            "run_ids": list(self.run_ids),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "cost_micros": self.cost_micros,
            "normalized": self.fingerprint.normalized,
            "fixture": self.fixture,
        }

    @property
    def impact(self) -> tuple[int, int]:
        return (int(self.cost_micros), int(self.count))


def rank_clusters(clusters: list[FailureCluster]) -> list[FailureCluster]:
    """Highest cost burned, then count. First line is the thing worth fixing."""
    return sorted(clusters, key=lambda row: (-row.cost_micros, -row.count, row.fingerprint.id))


def add_observation(
    buckets: dict[str, FailureCluster],
    fp: FailureFingerprint,
    *,
    run_id: str,
    seen_at: str,
    cost_micros: int = 0,
) -> None:
    row = buckets.get(fp.id)
    if row is None:
        buckets[fp.id] = FailureCluster(
            fingerprint=fp,
            count=1,
            run_ids=[run_id] if run_id else [],
            first_seen=seen_at,
            last_seen=seen_at,
            cost_micros=max(0, int(cost_micros)),
            node_id=fp.node_id,
        )
        return
    row.count += 1
    if run_id and run_id not in row.run_ids:
        row.run_ids.append(run_id)
    if seen_at:
        if not row.first_seen or seen_at < row.first_seen:
            row.first_seen = seen_at
        if not row.last_seen or seen_at > row.last_seen:
            row.last_seen = seen_at
    row.cost_micros += max(0, int(cost_micros))
