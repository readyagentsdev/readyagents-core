"""Hash MCP tool names, descriptions, and schemas. Rug-pulls become policy events."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SAFE_SERVER = re.compile(r"^[A-Za-z0-9._-]+$")


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


def pin_path(home: Path, server: str) -> Path:
    name = server if _SAFE_SERVER.fullmatch(server) else hashlib.sha256(server.encode()).hexdigest()
    return Path(home) / "mcp-pins" / f"{name}.json"


def load_pin_digest(home: Path | None, server: str) -> str | None:
    """Return a stored digest, ``None`` on first use, or ``''`` if the pin file is corrupt."""
    if home is None:
        return None
    path = pin_path(Path(home), server)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return ""
    if isinstance(data, dict):
        digest = data.get("digest")
        if isinstance(digest, str) and digest:
            return digest
    return ""


def store_pin_digest(home: Path | None, server: str, digest: str) -> None:
    if home is None or not server or not digest:
        return
    from readyagents.atomic import atomic_write_text

    payload = json.dumps({"server": server, "digest": digest}, sort_keys=True, ensure_ascii=False)
    atomic_write_text(pin_path(Path(home), server), payload + "\n", encoding="utf-8", newline="\n")
