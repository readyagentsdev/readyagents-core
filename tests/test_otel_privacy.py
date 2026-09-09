"""OTel pack: import is a no-op; disabled is empty; enabled is content-free."""

from __future__ import annotations

from pathlib import Path

from readyagents.observability import make_event
from readyagents.packs.loader import collect_pack_observers
from readyagents.packs.otel import OtelPack
from readyagents.workflow.runner import run_workflow_file


def test_import_and_discovery_do_not_touch_network(monkeypatch) -> None:
    def _blocked(*_a, **_k):
        raise AssertionError("network called during otel import/discovery")

    monkeypatch.setattr("socket.create_connection", _blocked)
    import importlib

    import readyagents.packs.otel as otel

    importlib.reload(otel)
    pack = otel.OtelPack()
    assert pack.register_observers() == []


def test_disabled_is_true_noop(monkeypatch) -> None:
    monkeypatch.delenv("READYAGENTS_OTEL", raising=False)
    assert OtelPack().register_observers() == []
    assert collect_pack_observers([OtelPack()]) == []


def test_enabled_spans_are_content_free(monkeypatch, tmp_path: Path, tmp_settings) -> None:
    monkeypatch.setenv("READYAGENTS_OTEL", "1")
    from readyagents.packs.otel import MemorySpanExporter, OtelObserver

    sink = MemorySpanExporter()
    observer = OtelObserver(exporter=sink)
    event = make_event(
        "node.finished",
        run_id="r1",
        workflow="w",
        node_id="n",
        node_type="agent",
        status="ok",
        usage={"prompt_tokens": 3, "completion_tokens": 4, "cost_micros": 10},
        attributes={"model": "mock:test", "prompt": "SECRET PROMPT", "output": "SECRET OUT"},
    )
    observer.on_event(event)
    spans = sink.get_finished_spans()
    assert spans
    blob = str(spans).lower()
    assert "secret prompt" not in blob
    assert "secret out" not in blob
    attrs = spans[0]["attributes"]
    assert attrs["gen_ai.usage.input_tokens"] == 3
    assert attrs["gen_ai.usage.output_tokens"] == 4
    assert attrs["readyagents.cost_micros"] == 10
    assert attrs["gen_ai.request.model"] == "mock:test"
    assert "prompt" not in attrs
    observer.shutdown()

    pack = OtelPack()
    observers = pack.register_observers()
    assert observers
    wf = tmp_path / "w.yaml"
    wf.write_text(
        "name: w\nnodes:\n  - id: a\n    type: transform\n    template: 'ok'\n    output_key: summary\n",
        encoding="utf-8",
    )
    state = run_workflow_file(wf, settings=tmp_settings, persist=False, extra_packs=[pack])
    assert state.status == "succeeded"


def test_all_extra_excludes_otel() -> None:
    text = Path("pyproject.toml").read_text(encoding="utf-8")
    match = re_search(text)
    assert "opentelemetry" not in match


def re_search(text: str) -> str:
    import re

    found = re.search(r"^all = (\[.*?\])", text, flags=re.M | re.S)
    assert found, "all extra missing"
    return found.group(1)
