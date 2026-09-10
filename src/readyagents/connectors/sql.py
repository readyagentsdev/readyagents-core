"""Read-only SQL (sqlite). Destination is local. No vendor driver."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from typing import Any

from readyagents.connectors.context import ConnectorContext
from readyagents.connectors.spec import ConnectorSpec
from readyagents.errors import ToolError
from readyagents.paths import resolve_within

_WRITE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|pragma|vacuum)\b",
    re.IGNORECASE,
)


class SqlConnector:
    spec = ConnectorSpec(
        name="sql",
        version="1.0.0",
        description="Read-only SQLite query confined to the workspace.",
        input_schema={
            "type": "object",
            "properties": {
                "database": {"type": "string"},
                "query": {"type": "string"},
            },
            "required": ["database", "query"],
        },
        output_schema={"type": "object"},
        destinations=("local",),
        determinism="recomputed",
        idempotent=True,
        side_effects="read",
    )

    def call(self, args: Mapping[str, Any], ctx: ConnectorContext) -> Any:
        database = str(args.get("database") or "").strip()
        query = str(args.get("query") or "").strip()
        if not database or not query:
            raise ToolError("sql requires database and query")
        if _WRITE.search(query.split(";", 1)[0]):
            raise ToolError("sql connector is read-only")
        path = resolve_within(database, ctx.workspace)
        if not path.is_file():
            raise ToolError(f"sql database not found: {database}")
        uri = path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(query)
            rows = [dict(row) for row in cur.fetchmany(500)]
            return {"rows": rows, "count": len(rows)}
        except sqlite3.Error as extra:
            raise ToolError(f"sql query failed: {extra}") from extra
        finally:
            conn.close()
