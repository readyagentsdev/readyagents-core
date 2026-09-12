"""Neutral intermediate graph. A fifth source is a parser plus a table."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SOURCES = ("n8n", "langgraph", "crewai", "trigger")

FIDELITY = ("translated", "approximated", "unsupported")


@dataclass
class IntermediateNode:
    id: str
    kind: str
    title: str
    params: dict[str, Any] = field(default_factory=dict)
    structural: str | None = None  # branch|loop|parallel|subworkflow|error|human


@dataclass
class IntermediateEdge:
    source: str
    target: str
    kind: str = "next"  # next|then|else|error


@dataclass
class IntermediateGraph:
    source: str
    name: str
    version: str | None
    nodes: list[IntermediateNode] = field(default_factory=list)
    edges: list[IntermediateEdge] = field(default_factory=list)
    start: str | None = None
    credential_names: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
