"""Register connectors as ordinary tools. Existing packs need not implement this."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from readyagents.connectors.context import (
    ConnectorContext,
    bind_context,
    current_context,
    reset_context,
)
from readyagents.connectors.spec import Connector, ConnectorSpec
from readyagents.tools import FunctionTool, Tool

_CONNECTORS: dict[str, Connector] = {}


def register(connector: Connector) -> None:
    _CONNECTORS[connector.spec.name] = connector


def spec_for(name: str) -> ConnectorSpec | None:
    item = _CONNECTORS.get(name)
    return None if item is None else item.spec


def get_connector(name: str) -> Connector:
    from readyagents.errors import ToolError

    if name not in _CONNECTORS:
        known = ", ".join(sorted(_CONNECTORS)) or "(none)"
        raise ToolError(f"Unknown connector '{name}'. Installed: {known}")
    return _CONNECTORS[name]


def installed_specs() -> list[ConnectorSpec]:
    return [item.spec for item in _CONNECTORS.values()]


def _call_registered(name: str, **kwargs: Any) -> Any:
    connector = get_connector(name)
    ctx = current_context()
    return connector.call(kwargs, ctx)


def as_tool(connector: Connector) -> FunctionTool:
    spec = connector.spec

    def handler(**kwargs: Any) -> Any:
        return _call_registered(spec.name, **kwargs)

    return FunctionTool(
        name=spec.name,
        description=spec.description,
        handler=handler,
        schema=dict(spec.input_schema),
        determinism=spec.determinism,
    )


def first_party_connectors(*, workspace: Path | None = None) -> list[Connector]:
    del workspace
    from readyagents.connectors.ingest import FileIngestConnector
    from readyagents.connectors.message import MessageConnector
    from readyagents.connectors.rest import RestConnector
    from readyagents.connectors.sql import SqlConnector
    from readyagents.connectors.storage import ObjectStorageConnector

    return [
        RestConnector(),
        SqlConnector(),
        ObjectStorageConnector(),
        MessageConnector(),
        FileIngestConnector(),
    ]


def connector_tools(*, workspace: Any = None) -> list[Tool]:
    root = Path(workspace) if workspace is not None else None
    tools: list[Tool] = []
    for connector in first_party_connectors(workspace=root):
        register(connector)
        tools.append(as_tool(connector))
    return tools


def wrap_connector_runner(
    runner: Callable[[], Any],
    ctx: ConnectorContext,
) -> Callable[[], Any]:
    def _run() -> Any:
        token = bind_context(ctx)
        try:
            return runner()
        finally:
            reset_context(token)

    return _run


def call_is_write(spec: ConnectorSpec, args: Mapping[str, Any] | None) -> bool:
    if spec.side_effects == "write":
        return True
    if spec.side_effects == "none":
        return False
    method = str((args or {}).get("method") or (args or {}).get("operation") or "").upper()
    if method in {"POST", "PUT", "PATCH", "DELETE", "SEND", "PUT_OBJECT"}:
        return True
    operation = str((args or {}).get("op") or (args or {}).get("operation") or "").lower()
    if operation in {"put", "send", "create", "write", "delete", "post"}:
        return True
    return False
