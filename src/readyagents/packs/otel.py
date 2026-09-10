"""Optional OpenTelemetry pack. Disabled and content-free by default.

Importing this module starts no collector, no exporter, and no network.
Enable with READYAGENTS_OTEL=1. Requires the optional ``otel`` extra only
when you want the OpenTelemetry SDK; the extra is not part of ``all``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from typing import Any

from readyagents.packs.protocol import BasePack

_ENABLED_ENV = "READYAGENTS_OTEL"
_TRUTH = frozenset({"1", "true", "yes", "on"})
_SECRETISH = re.compile(r"(?i)(sk-[a-z0-9]|api[_-]?key\s*=|bearer\s+[a-z0-9]|-----begin )")
_BLOCKED_ATTR_KEYS = frozenset(
    {
        "prompt",
        "input",
        "output",
        "arguments",
        "args",
        "tool_arguments",
        "secret",
        "secrets",
        "api_key",
        "apikey",
        "authorization",
        "token",
        "password",
        "completion",
        "messages",
        "content",
        "system",
        "text",
        "result",
        "tool_args",
    }
)


class MemorySpanExporter:
    """In-process span sink. Test hook. No network."""

    def __init__(self) -> None:
        self._spans: list[Any] = []

    def export(self, spans: Sequence[Any]) -> int:
        self._spans.extend(list(spans))
        return 0

    def shutdown(self, timeout_millis: int = 0) -> None:
        _ = timeout_millis

    def force_flush(self, timeout_millis: int = 0) -> bool:
        _ = timeout_millis
        return True

    def get_finished_spans(self) -> list[Any]:
        return list(self._spans)


class OtelObserver:
    """Content-free observer. Lazy-imports OpenTelemetry only when constructed."""

    def __init__(self, exporter: Any | None = None) -> None:
        self.exporter = exporter if exporter is not None else MemorySpanExporter()
        self._provider: Any = None
        self._tracer: Any = None
        self._init_sdk()

    def on_event(self, event: Any) -> None:
        attrs = span_attributes(event)
        record = {"name": str(getattr(event, "name", "event")), "attributes": attrs}
        exporter = self.exporter
        if exporter is not None and hasattr(exporter, "export"):
            try:
                exporter.export([record])
            except Exception:  # noqa: BLE001
                pass
        if self._tracer is None:
            return
        span = self._tracer.start_span(record["name"])
        try:
            for key, value in attrs.items():
                span.set_attribute(key, value)
        finally:
            span.end()

    def shutdown(self) -> None:
        provider = self._provider
        if provider is not None and hasattr(provider, "shutdown"):
            provider.shutdown()
        exporter = self.exporter
        if exporter is not None and hasattr(exporter, "shutdown"):
            exporter.shutdown()

    def _init_sdk(self) -> None:
        try:
            from opentelemetry.sdk.trace import TracerProvider
        except ImportError:
            return
        provider = TracerProvider()
        exporter = self.exporter
        if exporter is not None and not isinstance(exporter, MemorySpanExporter):
            try:
                from opentelemetry.sdk.trace.export import SimpleSpanProcessor

                provider.add_span_processor(SimpleSpanProcessor(exporter))
            except Exception:  # noqa: BLE001
                pass
        self._provider = provider
        self._tracer = provider.get_tracer("readyagents.otel")


class OtelPack(BasePack):
    name = "otel"
    version = "0.0.1"

    def __init__(self, exporter: Any | None = None) -> None:
        self._exporter = exporter

    @classmethod
    def memory_exporter(cls) -> MemorySpanExporter:
        return MemorySpanExporter()

    def register_observers(self) -> list[Any]:
        if not otel_enabled():
            return []
        return [OtelObserver(exporter=self._exporter)]


def otel_enabled() -> bool:
    raw = (os.environ.get(_ENABLED_ENV) or "").strip().lower()
    return raw in _TRUTH


def span_attributes(event: Any) -> dict[str, Any]:
    """Allowlisted usage/model/node/status/run/cost. Never prompt or secrets."""
    attrs: dict[str, Any] = {}
    run_id = getattr(event, "run_id", None)
    if run_id:
        attrs["readyagents.run_id"] = str(run_id)
    workflow = getattr(event, "workflow", None)
    if workflow:
        attrs["readyagents.workflow"] = str(workflow)
    node_id = getattr(event, "node_id", None)
    if node_id:
        attrs["readyagents.node_id"] = str(node_id)
    node_type = getattr(event, "node_type", None)
    if node_type:
        attrs["readyagents.node_type"] = str(node_type)
    status = getattr(event, "status", None)
    if status:
        attrs["readyagents.status"] = str(status)
    duration = getattr(event, "duration_ms", None)
    if duration is not None:
        attrs["readyagents.duration_ms"] = int(duration)
    usage = getattr(event, "usage", None) or {}
    if isinstance(usage, Mapping):
        if "prompt_tokens" in usage:
            attrs["gen_ai.usage.input_tokens"] = int(usage["prompt_tokens"])
        if "completion_tokens" in usage:
            attrs["gen_ai.usage.output_tokens"] = int(usage["completion_tokens"])
        if "total_tokens" in usage:
            attrs["readyagents.total_tokens"] = int(usage["total_tokens"])
        if "cost_micros" in usage:
            attrs["readyagents.cost_micros"] = int(usage["cost_micros"])
    incoming = getattr(event, "attributes", None) or {}
    if isinstance(incoming, Mapping):
        model = incoming.get("model") or incoming.get("gen_ai.request.model")
        if isinstance(model, str) and model and not _SECRETISH.search(model):
            attrs["gen_ai.request.model"] = model
    return {key: value for key, value in attrs.items() if _keep_attr(key, value)}


def _keep_attr(key: str, value: Any) -> bool:
    lowered = key.strip().lower()
    if lowered in _BLOCKED_ATTR_KEYS:
        return False
    if isinstance(value, str) and _SECRETISH.search(value):
        return False
    return True


def get_pack() -> OtelPack:
    return OtelPack()
