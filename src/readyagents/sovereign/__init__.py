"""Sovereign mode: in-process socket-boundary egress guard and attestation.

This is not an OS sandbox. DNS resolution can leak. MCP stdio subprocesses
are network-uncontrolled.
"""

from readyagents.sovereign.egress import (
    EgressGuard,
    install_guard,
    is_loopback_url,
    set_node,
)

__all__ = ["EgressGuard", "install_guard", "is_loopback_url", "set_node"]
