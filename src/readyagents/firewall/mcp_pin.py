"""Hash MCP tool names, descriptions, and schemas. Rug-pulls become policy events."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PinSnapshot:
    server: str
    digest: str
    tools: tuple[tuple[str, str], ...]  # (qualified name, description)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def snapshot_tools(server: str, tools: Mapping[str, Any]) -> PinSnapshot:
    rows: list[tuple[str, str, str]] = []
    for name, tool in sorted(tools.items()):
        desc = str(getattr(tool, "description", "") or "")
        schema = getattr(tool, "schema", None) or {}
        rows.append((str(name), desc, _canonical(schema)))
    blob = _canonical([{"name": n, "description": d, "schema": s} for n, d, s in rows])
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    return PinSnapshot(
        server=server,
        digest=digest,
        tools=tuple((n, d) for n, d, _s in rows),
    )


def grouped_snapshots(tools: Mapping[str, Any]) -> dict[str, PinSnapshot]:
    """Group qualified ``server.tool`` names by server."""
    buckets: dict[str, dict[str, Any]] = {}
    for name, tool in tools.items():
        server = name.split(".", 1)[0] if "." in name else name
        buckets.setdefault(server, {})[name] = tool
    return {server: snapshot_tools(server, group) for server, group in buckets.items()}
