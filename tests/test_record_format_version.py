from __future__ import annotations

from pathlib import Path

from readyagents.workflow.state import RunState


def test_loads_0_9_era_record() -> None:
    path = Path("tests/fixtures/run_record_v0_9.json")
    data = path.read_text(encoding="utf-8")
    import json

    record = json.loads(data)
    assert "record_version" not in record
    state = RunState.from_record(record)
    assert state.run_id == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert state.status == "succeeded"
    assert state.output_keys["summary"] == "ok"
    exported = state.to_record()
    assert exported["record_version"] == 1
