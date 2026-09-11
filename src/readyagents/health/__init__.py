"""Failure fingerprints, health query, declared recovery. No daemon, no telemetry."""

from __future__ import annotations

from readyagents.health.cluster import FailureCluster, rank_clusters
from readyagents.health.fingerprint import FailureFingerprint, classify_failure, fingerprint
from readyagents.health.layout import (
    BELOW_GATE,
    DEFAULT_LIMIT,
    DEFAULT_WINDOW,
    FAILURE_CLASSES,
    HARD_MAX_RUNS,
    RECOVERY_ACTIONS,
)
from readyagents.health.query import HealthReport, query_health

__all__ = [
    "BELOW_GATE",
    "DEFAULT_LIMIT",
    "DEFAULT_WINDOW",
    "FAILURE_CLASSES",
    "FailureCluster",
    "FailureFingerprint",
    "HARD_MAX_RUNS",
    "HealthReport",
    "RECOVERY_ACTIONS",
    "classify_failure",
    "fingerprint",
    "query_health",
    "rank_clusters",
]
