"""Offline-by-default benchmark harness. Not a model-quality claim."""

from __future__ import annotations

from readyagents.bench.compare import compare_results
from readyagents.bench.layout import MODE_LIVE, MODE_OFFLINE, SCHEMA_RESULT
from readyagents.bench.run import BenchReport, run_bench

__all__ = [
    "MODE_LIVE",
    "MODE_OFFLINE",
    "SCHEMA_RESULT",
    "BenchReport",
    "compare_results",
    "run_bench",
]
