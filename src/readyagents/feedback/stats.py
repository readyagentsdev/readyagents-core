"""Correction rates with sample sizes. No significance claim."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from readyagents.errors import FeedbackRefused
from readyagents.feedback.collect import collect_corrections
from readyagents.feedback.layout import HUMAN, STATS_BY
from readyagents.feedback.record import StatsReport


def feedback_stats(*, settings: Any, by: str = "node") -> StatsReport:
    dim = str(by or "node").strip().lower()
    if dim not in STATS_BY:
        raise FeedbackRefused(f"unknown stats dimension {by!r}", reason="stats")
    buckets: dict[str, list[str]] = defaultdict(list)
    for _state, corr in collect_corrections(settings):
        key = _key(corr, dim)
        buckets[key].append(corr.kind)
    rows: list[dict[str, Any]] = []
    sample = 0
    for key, kinds in sorted(buckets.items()):
        n = len(kinds)
        sample += n
        human = sum(1 for item in kinds if item == HUMAN)
        rows.append(
            {
                dim: key,
                "n": n,
                "sample_size": n,
                "human": human,
                "implicit": n - human,
                "correction_rate": round(human / n, 4) if n else 0.0,
                "significance": None,
            }
        )
    return StatsReport(ok=True, by=dim, rows=rows, sample_size=sample)


def _key(corr: Any, dim: str) -> str:
    if dim == "node":
        return corr.node_id or "unknown"
    if dim == "model":
        return corr.model or "unknown"
    if dim == "label":
        return corr.label or corr.signal or "unlabelled"
    if dim == "week":
        return _iso_week(str(corr.ts or ""))
    return "unknown"


def _iso_week(stamp: str) -> str:
    """ISO year-week (2026-W37), never a calendar day."""
    from datetime import datetime

    text = str(stamp or "").strip()
    if not text:
        return "unknown"
    parsed = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return "unknown"
    iso = parsed.isocalendar()
    return f"{iso[0]}-W{int(iso[1]):02d}"
