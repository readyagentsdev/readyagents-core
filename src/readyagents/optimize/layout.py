"""Frozen optimize interface: stop reasons, defaults, persist schema."""

from __future__ import annotations

SCHEMA_RUN = "readyagents.optimize.run.v1"
STOP_ITERATIONS = "iterations"
STOP_SPEND = "spend"
STOP_WALL = "wall"
STOP_COMPLETE = "complete"
DEFAULT_MAX_ITERATIONS = 8
DEFAULT_MIN_IMPROVEMENT = 0.05
DEFAULT_CANDIDATES = 3
DEFAULT_HOLD_FRACTION = 3  # last 1/N cases
