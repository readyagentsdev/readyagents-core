"""Parser bounds: size, depth, node count. Typed refuses."""

from __future__ import annotations

from typing import Any

from readyagents.errors import ImportBoundDepth, ImportBoundNodes, ImportBoundOversized

MAX_BYTES = 1_048_576
MAX_DEPTH = 24
MAX_NODES = 400


def check_size(data: bytes | str) -> None:
    n = len(data.encode("utf-8") if isinstance(data, str) else data)
    if n > MAX_BYTES:
        raise ImportBoundOversized(f"import source is {n} bytes; max is {MAX_BYTES}")


def check_nodes(count: int) -> None:
    if count > MAX_NODES:
        raise ImportBoundNodes(f"import source has {count} nodes; max is {MAX_NODES}")


def check_depth(value: Any, *, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise ImportBoundDepth(f"import source nesting exceeds {MAX_DEPTH}")
    if isinstance(value, dict):
        for item in value.values():
            check_depth(item, depth=depth + 1)
        return
    if isinstance(value, list):
        for item in value:
            check_depth(item, depth=depth + 1)
