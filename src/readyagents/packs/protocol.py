"""Pack protocol — commercial / extra capability layers sit on top of core."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from readyagents.tools import Tool


@runtime_checkable
class NodeHandler(Protocol):
    """Optional custom node type: `execute(node, state, context) -> output`."""

    type_name: str

    def execute(self, node: Any, state: Any, context: Any) -> Any: ...


@runtime_checkable
class Pack(Protocol):
    """A ReadyAgents pack discovered via entry points `readyagents.packs`."""

    name: str
    version: str

    def register_nodes(self) -> Mapping[str, NodeHandler]: ...

    def register_tools(self) -> Sequence[Tool]: ...

    def register_workflows(self) -> Sequence[Any]: ...


class BasePack:
    """Convenient base class for packs (not required)."""

    name = "unnamed"
    version = "0.0.0"

    def register_nodes(self) -> Mapping[str, NodeHandler]:
        return {}

    def register_tools(self) -> Sequence[Tool]:
        return []

    def register_workflows(self) -> Sequence[Any]:
        return []

    def register_secrets(self) -> Sequence[Any]:
        """Optional secrets-manager backends (Vault/AWS live in packs, not core)."""
        return []

    def register_authorizers(self) -> Sequence[Any]:
        """Optional RBAC hooks. Default in core is allow-all."""
        return []

    def register_tool_seals(self) -> Mapping[str, str]:
        """Optional cassette classification: recomputed / sealable / unsealable."""
        return {}
