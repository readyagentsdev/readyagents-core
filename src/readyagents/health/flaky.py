"""Flaky vs broken: identical cassette inputs, differing outcomes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from readyagents.replay.cassette import Cassette


def input_digest(state: Any, node_id: str) -> str | None:
    """Content hash of cassette inputs for this node. None without a cassette."""
    cassette = _cassette(state)
    if cassette is None:
        return None
    parts: list[str] = []
    for key, entry in cassette.entries.items():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("node_id") or "") not in {"", node_id} and f":{node_id}" not in key:
            continue
        blob = json.dumps(
            {
                "kind": entry.get("kind"),
                "digest": entry.get("digest") or key,
                "messages": entry.get("messages"),
                "arguments": entry.get("arguments"),
                "name": entry.get("name"),
            },
            sort_keys=True,
            default=str,
        )
        parts.append(blob)
    if not parts:
        return None
    joined = "\n".join(sorted(parts))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def classify_stability(
    observations: list[tuple[str, bool]],
) -> str:
    """``observations`` is (input_digest, ok). Returns flaky, broken, or healthy."""
    by_digest: dict[str, set[bool]] = {}
    for digest, ok in observations:
        if not digest:
            continue
        by_digest.setdefault(digest, set()).add(bool(ok))
    mixed = [digest for digest, outcomes in by_digest.items() if len(outcomes) > 1]
    if mixed:
        return "flaky"
    oks = [ok for _digest, ok in observations]
    if oks and not any(oks):
        return "broken"
    if oks and all(oks):
        return "healthy"
    return "broken" if oks else "unknown"


def _cassette(state: Any) -> Cassette | None:
    raw = None
    metadata = getattr(state, "metadata", None) or {}
    if isinstance(metadata, dict):
        raw = metadata.get("cassette")
    if isinstance(raw, str) and raw.strip() and Path(raw).is_file():
        try:
            return Cassette.load(raw)
        except Exception:  # noqa: BLE001
            return None
    return None
