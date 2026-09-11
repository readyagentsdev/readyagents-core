"""Helpers to emit a synthetic (empty-entry) cassette document."""

from __future__ import annotations

import json
from pathlib import Path

from readyagents import __version__
from readyagents.replay.cassette import CASSETTE_VERSION


def write_synthetic_cassette(path: Path, *, run_id: str, workflow: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "cassette_version": CASSETTE_VERSION,
        "readyagents_version": __version__,
        "run_id": run_id,
        "workflow": workflow,
        "recorded_at": "2026-01-01T00:00:00+00:00",
        "redacted": True,
        "positional_fallback": False,
        "entries": {},
        "determinism": {
            "sealed": [],
            "recomputed": [],
            "unsealable": [],
            "misses": [],
            "positional_fallback": False,
        },
        "blocked_nodes": [],
    }
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
