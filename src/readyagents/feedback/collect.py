"""Load corrections from the existing run store."""

from __future__ import annotations

from typing import Any

from readyagents.feedback.capture import corrections_from_state
from readyagents.feedback.record import Correction
from readyagents.run_store import open_run_store
from readyagents.run_store.base import RunQuery


def collect_corrections(settings: Any) -> list[tuple[Any, Correction]]:
    store = open_run_store(settings)
    out: list[tuple[Any, Correction]] = []
    try:
        rows = store.list(RunQuery(limit=0))
        for item in rows:
            state = item.state
            for corr in corrections_from_state(state):
                out.append((state, corr))
    finally:
        closer = getattr(store, "close", None)
        if callable(closer):
            closer()
    return out
