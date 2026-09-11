"""Collect labelled metrics from a finished run. Offline and live stay separate."""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass, field
from typing import Any

from readyagents import __version__
from readyagents.bench.layout import MODE_LIVE, TIMING_LIVE, TIMING_OFFLINE
from readyagents.workflow.state import RunState


@dataclass
class Timing:
    kind: str
    wall_ms: float
    ttft_ms: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "wall_ms": self.wall_ms, "ttft_ms": self.ttft_ms}


@dataclass
class ScenarioMetrics:
    name: str
    shape: str
    success: bool
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    node_count: int = 0
    determinism: dict[str, Any] = field(default_factory=dict)
    timing_offline: Timing | None = None
    timing_live: Timing | None = None
    cassette_digest: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "shape": self.shape,
            "success": self.success,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": self.cost_usd,
            "tool_calls": self.tool_calls,
            "node_count": self.node_count,
            "determinism": dict(self.determinism),
            "timing_offline": (
                None if self.timing_offline is None else self.timing_offline.as_dict()
            ),
            "timing_live": None if self.timing_live is None else self.timing_live.as_dict(),
            "cassette_digest": self.cassette_digest,
        }


def collect(
    state: RunState,
    *,
    name: str,
    shape: str,
    wall_ms: float,
    mode: str,
    cassette_digest: str = "",
) -> ScenarioMetrics:
    usage = dict(state.usage or {})
    tokens_in = int(usage.get("prompt_tokens") or 0)
    tokens_out = int(usage.get("completion_tokens") or 0)
    micros = int(usage.get("cost_micros") or 0)
    tools = 0
    ttft: float | None = None
    for row in state.results:
        rounds = len(row.tool_rounds or [])
        if rounds:
            tools += rounds
        elif str(row.type) == "tool":
            tools += 1
        if row.ttft_ms is not None and (ttft is None or row.ttft_ms < ttft):
            ttft = float(row.ttft_ms)
    det = state.metadata.get("determinism") if isinstance(state.metadata, dict) else None
    if not isinstance(det, dict):
        det = {}
    timing = Timing(
        kind=TIMING_LIVE if mode == MODE_LIVE else TIMING_OFFLINE,
        wall_ms=round(float(wall_ms), 3),
        ttft_ms=ttft,
    )
    metrics = ScenarioMetrics(
        name=name,
        shape=shape,
        success=state.status == "succeeded",
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=round(micros / 1_000_000.0, 6),
        tool_calls=tools,
        node_count=len(state.results),
        determinism={k: det.get(k) for k in ("sealed", "recomputed", "unsealable", "misses")},
        cassette_digest=cassette_digest,
    )
    if mode == MODE_LIVE:
        metrics.timing_live = timing
    else:
        metrics.timing_offline = timing
    return metrics


def method_statement(
    *,
    mode: str,
    cassette_digests: dict[str, str],
    reproduce: str,
) -> dict[str, Any]:
    return {
        "hardware": f"{platform.machine()} {platform.processor()}".strip(),
        "os": platform.platform(),
        "python": sys.version.split()[0],
        "package_version": __version__,
        "cassette_digests": dict(sorted(cassette_digests.items())),
        "mode": mode,
        "offline_vs_live": (
            "offline_engine timing is the engine's own work; "
            "live_e2e timing includes the provider. They are never one number."
        ),
        "reproduce": reproduce,
    }
