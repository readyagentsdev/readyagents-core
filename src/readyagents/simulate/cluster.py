"""Cluster failures by shape, not by raw input. One representative per key."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from readyagents.atomic import atomic_write_text
from readyagents.simulate.layout import CLUSTERS_NAME
from readyagents.testing.eval import EvalResult


def cluster_key(result: EvalResult) -> str:
    status = ""
    node = ""
    if result.state is not None:
        status = str(result.state.status or "")
        if result.state.results:
            node = str(result.state.results[-1].node_id)
        elif result.state.errors:
            node = "error"
    reason = _shape(result.reason or "")
    raw = f"{status}|{reason}|{node}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _shape(reason: str) -> str:
    import re

    text = reason.split(":")[0][:160]
    text = re.sub(r"[0-9a-f]{12,}", "", text, flags=re.I)
    text = re.sub(r"(?:/|[A-Za-z]:\\)[^\s]+", "<path>", text)
    return text.strip()


def load_clusters(out_dir: Path | None) -> set[str]:
    if out_dir is None:
        return set()
    path = Path(out_dir) / CLUSTERS_NAME
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, list):
        return set()
    return {str(item) for item in keys if item}


def save_clusters(out_dir: Path, keys: set[str]) -> Path:
    dest = Path(out_dir)
    dest.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"keys": sorted(keys)}
    path = dest / CLUSTERS_NAME
    atomic_write_text(
        path,
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
        restrict=False,
    )
    return path


def pick_representatives(results: list[EvalResult]) -> dict[str, EvalResult]:
    chosen: dict[str, EvalResult] = {}
    for row in results:
        if row.passed:
            continue
        key = cluster_key(row)
        if key not in chosen:
            chosen[key] = row
    return chosen
