"""Compatibility shim. Implementation lives in ``readyagents.packs.otel``."""

from readyagents.packs.otel import MemorySpanExporter, OtelObserver, span_attributes

__all__ = ["MemorySpanExporter", "OtelObserver", "span_attributes"]
