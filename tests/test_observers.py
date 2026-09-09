"""Observer seam: old packs load; failures cannot change a run."""

from __future__ import annotations

from pathlib import Path

from readyagents.observability import RunEvent
from readyagents.packs.loader import collect_pack_observers
from readyagents.packs.protocol import BasePack
from readyagents.workflow.runner import run_workflow_file
from readyagents.workflow.state import RunState


class OldPack:
    name = "old"
    version = "0"

    def register_nodes(self):
        return {}

    def register_tools(self):
        return []

    def register_workflows(self):
        return []


class BoomObserver:
    def on_event(self, event: RunEvent) -> None:
        raise RuntimeError("observer boom secret=sk-notrealvalue99")


class BoomPack(BasePack):
    name = "boom"

    def register_observers(self):
        return [BoomObserver()]


class RecordingObserver:
    def __init__(self) -> None:
        self.events: list[str] = []

    def on_event(self, event: RunEvent) -> None:
        self.events.append(event.name)


def test_old_pack_without_register_observers_loads() -> None:
    assert collect_pack_observers([OldPack()]) == []  # type: ignore[list-item]
    assert collect_pack_observers([BasePack()]) == []


def test_observer_exception_does_not_change_run(tmp_path: Path, tmp_settings, caplog) -> None:
    wf = tmp_path / "w.yaml"
    wf.write_text(
        "name: w\nnodes:\n  - id: a\n    type: transform\n    template: 'ok'\n    output_key: summary\n",
        encoding="utf-8",
    )
    pack = BoomPack()
    state = run_workflow_file(wf, settings=tmp_settings, persist=True, extra_packs=[pack])
    assert state.status == "succeeded"
    assert state.output_keys["summary"] == "ok"
    record = RunState.from_record(state.to_record())
    assert record.status == "succeeded"
    text = caplog.text.lower()
    assert "observer" in text
    assert "sk-notrealvalue99" not in text


def test_include_does_not_double_count(tmp_path: Path, tmp_settings) -> None:
    child = tmp_path / "child.yaml"
    child.write_text(
        "name: child\nnodes:\n  - id: c\n    type: tool\n    tool: calc\n"
        "    arguments: {expression: '3'}\n    output_key: inner\n",
        encoding="utf-8",
    )
    wf = tmp_path / "parent.yaml"
    wf.write_text(
        "name: parent\nnodes:\n  - id: inc\n    type: include\n    path: child.yaml\n"
        "    output_key: nested\n",
        encoding="utf-8",
    )
    observer = RecordingObserver()

    class RecPack(BasePack):
        name = "rec"

        def register_observers(self):
            return [observer]

    state = run_workflow_file(wf, settings=tmp_settings, persist=False, extra_packs=[RecPack()])
    assert state.status == "succeeded"
    assert observer.events.count("run.started") == 1
    assert observer.events.count("run.finished") == 1
    assert observer.events.count("node.finished") == 1
