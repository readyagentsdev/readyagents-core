"""Scoped local memory. Untrusted. No ambient store. Not a quality claim."""

from readyagents.memory.protocol import (
    MemoryHit,
    MemoryRecord,
    MemoryStore,
    open_memory_store,
)
from readyagents.memory.scope import parse_scope, validate_scope

__all__ = [
    "MemoryHit",
    "MemoryRecord",
    "MemoryStore",
    "open_memory_store",
    "parse_scope",
    "validate_scope",
]
