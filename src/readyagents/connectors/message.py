"""Webhook-shaped message send. Write-shaped; gated by default."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from readyagents.connectors.context import ConnectorContext
from readyagents.connectors.fixtures import FixtureStore
from readyagents.connectors.spec import AuthSpec, ConnectorSpec
from readyagents.errors import ToolError
from readyagents.paths import resolve_within


class MessageConnector:
    spec = ConnectorSpec(
        name="message",
        version="1.0.0",
        description="Send a webhook-shaped payload to a declared URL.",
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "payload": {"type": "object"},
                "fixtures": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["url", "payload"],
        },
        output_schema={"type": "object"},
        auth=AuthSpec(kind="none"),
        destinations=("example.test",),
        determinism="unsealable",
        idempotent=True,
        idempotency_key="idempotency_key",
        side_effects="write",
    )

    def call(self, args: Mapping[str, Any], ctx: ConnectorContext) -> Any:
        url = str(args.get("url") or "").strip()
        payload = args.get("payload")
        if not url or not isinstance(payload, dict):
            raise ToolError("message requires url and payload object")
        host = (urlparse(url).hostname or "").lower()
        ctx.spec = ConnectorSpec(
            name=self.spec.name,
            version=self.spec.version,
            description=self.spec.description,
            input_schema=self.spec.input_schema,
            output_schema=self.spec.output_schema,
            auth=self.spec.auth,
            destinations=(host,) if host else self.spec.destinations,
            determinism=self.spec.determinism,
            idempotent=self.spec.idempotent,
            idempotency_key=self.spec.idempotency_key,
            side_effects="write",
        )
        fixtures_dir = args.get("fixtures")
        if fixtures_dir:
            ctx.fixtures = FixtureStore(resolve_within(str(fixtures_dir), ctx.workspace))

        def _send() -> dict[str, Any]:
            resp = ctx.http("POST", url, json_body=payload)
            if resp.status >= 400:
                raise ToolError(f"message HTTP {resp.status}")
            return {"sent": True, "status": resp.status}

        key = str(args.get("idempotency_key") or "").strip()
        if key:
            return ctx.idempotent(key, _send)
        return _send()
