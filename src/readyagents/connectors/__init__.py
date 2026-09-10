"""Typed connector layer above the tool registry.

Catalog is small by design. Governance (policy, audit, replay) is the
differentiator — not legal certification. In-process harness is not an OS sandbox.
"""

from readyagents.connectors.registry import (
    Connector,
    get_connector,
    installed_specs,
    spec_for,
)
from readyagents.connectors.spec import AuthSpec, ConnectorSpec, RateLimitSpec

__all__ = [
    "AuthSpec",
    "Connector",
    "ConnectorSpec",
    "RateLimitSpec",
    "get_connector",
    "installed_specs",
    "spec_for",
]
