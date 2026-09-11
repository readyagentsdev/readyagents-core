"""Frozen bench interface: metric keys, result document, baseline schema."""

from __future__ import annotations

SCHEMA_RESULT = "readyagents.bench.result.v1"
SCHEMA_BASELINE = "readyagents.bench.baseline.v1"
SCHEMA_SUITE = "readyagents.bench.suite.v1"
MODE_OFFLINE = "offline"
MODE_LIVE = "live"
TIMING_OFFLINE = "offline_engine"
TIMING_LIVE = "live_e2e"
DEFAULT_WALL_PCT = 100.0
DEFAULT_WALL_MIN_MS = 500.0
SHAPES = (
    "classify",
    "research",
    "approval",
    "foreach",
    "team",
    "document",
)
METRIC_KEYS = (
    "tokens_in",
    "tokens_out",
    "cost_usd",
    "tool_calls",
    "node_count",
    "success",
)
SECRET_NEEDLES = (
    "sk-",
    "ghp_",
    "-----BEGIN",
    "AKIA",
    "api_key",
    "password=",
)
