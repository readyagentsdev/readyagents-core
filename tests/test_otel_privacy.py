"""OTel pack: import is a no-op; disabled is empty; enabled is content-free."""

from __future__ import annotations

import importlib
import re
from pathlib import Path

from readyagents.observability import make_event
from readyagents.packs.loader import collect_pack_observers
from readyagents.packs.otel import OtelPack


def _block_network(monkeypatch) -> None:
    def _blocked(*_a, **_k):
        raise AssertionError("network called during otel import/discovery")

    monkeypatch.setattr("socket.create_connection", _blocked)
    monkeypatch.setattr("urllib.request.urlopen", _blocked)


def _span_attrs(span: object) -> dict:
    if isinstance(span, dict):
        raw = span.get("attributes") or {}
        return dict(raw)
    attrs = getattr(span, "attributes", None)
    return dict(attrs or {})


def test_import_and_discovery_do_not_touch_network(monkeypatch) -> None:
    monkeypatch.delenv("READYAGENTS_OTEL", raising=False)
    _block_network(monkeypatch)
    import readyagents.packs.otel as otel

    importlib.reload(otel)
    pack = otel.OtelPack()
    assert pack.register_observers() == []
    collect_pack_observers([pack])


def test_disabled_is_true_noop(monkeypatch) -> None:
    monkeypatch.delenv("READYAGENTS_OTEL", raising=False)
    assert OtelPack().register_observers() == []
    assert collect_pack_observers([OtelPack()]) == []


def test_enabled_spans_are_content_free(monkeypatch) -> None:
    monkeypatch.setenv("READYAGENTS_OTEL", "1")
    _block_network(monkeypatch)
    from readyagents.packs.otel import MemorySpanExporter

    sink = MemorySpanExporter()
    observers = OtelPack(exporter=sink).register_observers()
    assert observers, "enabled pack must register an observer"
    secret = "sk-LEAKEDKEYVALUE99"
    event = make_event(
        "node.finished",
        run_id="run-abc-123",
        workflow="demo",
        node_id="agent1",
        node_type="agent",
        status="ok",
        duration_ms=12,
        usage={
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
            "cost_micros": 1234,
        },
        attributes={
            "model": "openai:gpt-4o-mini",
            "prompt": "the secret prompt text XYZPROMPT",
            "input": "user input blob",
            "output": "model output blob",
            "arguments": {"q": "leak-me-args"},
            "tool_arguments": {"api_key": secret},
            "api_key": secret,
        },
    )
    observers[0].on_event(event)
    observers[0].shutdown()
    spans = sink.get_finished_spans()
    assert spans
    attrs = _span_attrs(spans[0])
    blob = str(spans).lower()
    assert attrs.get("readyagents.run_id") == "run-abc-123"
    assert attrs.get("readyagents.node_id") == "agent1"
    assert attrs.get("readyagents.status") == "ok"
    assert attrs.get("gen_ai.request.model") == "openai:gpt-4o-mini"
    assert attrs.get("gen_ai.usage.input_tokens") == 11
    assert attrs.get("readyagents.cost_micros") == 1234
    for banned in ("prompt", "input", "output", "arguments", "tool_arguments", "api_key"):
        assert banned not in attrs
    for leak in (
        "the secret prompt text",
        "user input blob",
        "model output blob",
        "leak-me-args",
        secret.lower(),
        "xyzprompt",
    ):
        assert leak not in blob


def test_all_extra_excludes_otel() -> None:
    text = Path("pyproject.toml").read_text(encoding="utf-8")
    found = re.search(r"^all = (\[.*?\])", text, flags=re.M | re.S)
    assert found, "all extra missing"
    assert "opentelemetry" not in found.group(1)
    assert "otel" not in found.group(1)
