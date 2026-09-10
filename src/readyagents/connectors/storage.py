"""Object storage get/put against the workspace. Put is write-shaped."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from readyagents.atomic import atomic_write_text
from readyagents.connectors.context import ConnectorContext
from readyagents.connectors.spec import ConnectorSpec
from readyagents.errors import ToolError
from readyagents.paths import resolve_within


class ObjectStorageConnector:
    spec = ConnectorSpec(
        name="object_storage",
        version="1.0.0",
        description="Get/put objects as files under the workspace.",
        input_schema={
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["get", "put"]},
                "key": {"type": "string"},
                "content": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["op", "key"],
        },
        output_schema={"type": "object"},
        destinations=("local",),
        determinism="recomputed",
        idempotent=True,
        idempotency_key="idempotency_key",
        side_effects="read",
    )

    def call(self, args: Mapping[str, Any], ctx: ConnectorContext) -> Any:
        op = str(args.get("op") or "get").strip().lower()
        key = str(args.get("key") or "").strip()
        if not key:
            raise ToolError("object_storage requires key")
        path = resolve_within(key, ctx.workspace)
        if op == "get":
            if not path.is_file():
                raise ToolError(f"object_storage key not found: {key}")
            data = path.read_bytes()
            ctx.cap_body(data, what="object")
            return {"key": key, "content": data.decode("utf-8", errors="replace")}
        if op != "put":
            raise ToolError("object_storage op must be get or put")

        def _put() -> dict[str, Any]:
            content = str(args.get("content") or "")
            ctx.cap_body(content.encode("utf-8"), what="object")
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, content, encoding="utf-8", newline="\n")
            return {"key": key, "written": True, "bytes": len(content)}

        idem = str(args.get("idempotency_key") or "").strip()
        if idem:
            return ctx.idempotent(idem, _put)
        return _put()
