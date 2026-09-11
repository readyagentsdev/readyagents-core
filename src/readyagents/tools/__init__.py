"""Common Tool protocol wrapping builtin and MCP tools."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from readyagents.errors import ToolError


class Tool(Protocol):
    name: str
    description: str
    schema: dict[str, Any]

    def run(self, **kwargs: Any) -> Any: ...


@dataclass
class FunctionTool:
    name: str
    description: str
    handler: Callable[..., Any]
    schema: dict[str, Any] = field(default_factory=dict)
    determinism: str | None = None

    def run(self, **kwargs: Any) -> Any:
        try:
            return self.handler(**kwargs)
        except TypeError as exc:
            raise ToolError(f"Tool '{self.name}' got invalid arguments: {exc}") from exc


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            known = ", ".join(sorted(self._tools)) or "(none)"
            raise ToolError(f"Unknown tool '{name}'. Available: {known}")
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def as_dict(self) -> Mapping[str, Tool]:
        return dict(self._tools)

    def merge(self, other: Mapping[str, Tool] | ToolRegistry) -> None:
        items = other.as_dict() if isinstance(other, ToolRegistry) else other
        for tool in items.values():
            if tool.name in self._tools:
                continue
            self.register(tool)


def default_registry(*, allow_http: bool, workspace: Any) -> ToolRegistry:
    from readyagents.mcp.builtin import builtin_tools

    registry = ToolRegistry()
    for tool in builtin_tools(allow_http=allow_http, workspace=workspace):
        registry.register(tool)
    from readyagents.connectors.registry import connector_tools

    for tool in connector_tools(workspace=workspace):
        if tool.name not in registry.as_dict():
            registry.register(tool)
    from readyagents.media.builtins import media_tools

    for tool in media_tools(workspace=workspace):
        if tool.name not in registry.as_dict():
            registry.register(tool)
    return registry
