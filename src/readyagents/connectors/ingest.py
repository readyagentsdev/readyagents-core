"""Inbound file/CSV ingestion from the workspace."""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping
from typing import Any

from readyagents.connectors.context import ConnectorContext
from readyagents.connectors.spec import ConnectorSpec
from readyagents.errors import ToolError
from readyagents.paths import resolve_within


class FileIngestConnector:
    spec = ConnectorSpec(
        name="ingest",
        version="1.0.0",
        description="Read a workspace file or CSV into JSON rows.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "format": {"type": "string", "enum": ["text", "csv"]},
            },
            "required": ["path"],
        },
        output_schema={"type": "object"},
        destinations=("local",),
        determinism="recomputed",
        idempotent=True,
        side_effects="read",
    )

    def call(self, args: Mapping[str, Any], ctx: ConnectorContext) -> Any:
        rel = str(args.get("path") or "").strip()
        if not rel:
            raise ToolError("ingest requires path")
        path = resolve_within(rel, ctx.workspace)
        if not path.is_file():
            raise ToolError(f"ingest file not found: {rel}")
        raw = path.read_bytes()
        ctx.cap_body(raw, what="file")
        text = raw.decode("utf-8", errors="replace")
        fmt = str(args.get("format") or _guess(rel)).lower()
        if fmt == "csv":
            reader = csv.DictReader(io.StringIO(text))
            rows = list(reader)
            return {"format": "csv", "rows": rows, "count": len(rows)}
        return {"format": "text", "content": text, "bytes": len(raw)}


def _guess(name: str) -> str:
    return "csv" if name.lower().endswith(".csv") else "text"
