from __future__ import annotations

from pathlib import Path

import pytest

from readyagents.errors import CassetteError, PathError
from readyagents.llm.base import CompletionResult, Message
from readyagents.policy import Redactor
from readyagents.replay.cassette import Cassette
from readyagents.replay.freeze import freeze_run
from readyagents.replay.record import contains_secret, record_llm_result
from readyagents.testing.helpers import ScriptedLLM
from readyagents.workflow.runner import run_workflow_file


def test_secret_in_prompt_is_blocked(tmp_path: Path) -> None:
    tape = Cassette.new(run_id="r", workflow="w")
    secret = "sk-live-super-secret-value"
    messages = [Message(role="user", content=f"token {secret}")]
    result = CompletionResult(text="ok", model="m")
    record_llm_result(
        tape,
        node_id="a",
        model="m",
        messages=messages,
        tools=None,
        result=result,
        secrets=[secret],
    )
    blob = str(tape.to_document())
    assert secret not in blob
    assert tape.blocked_nodes == {"a"}


def test_redactor_strips_pii_before_write() -> None:
    tape = Cassette.new(run_id="r", workflow="w")
    redactor = Redactor(literals=["Alice Example"])
    record_llm_result(
        tape,
        node_id="a",
        model="m",
        messages=[Message(role="user", content="hello")],
        tools=None,
        result=CompletionResult(text="meet Alice Example tomorrow", model="m"),
        redactor=redactor,
        secrets=[],
    )
    entry = next(iter(tape.entries.values()))
    assert "Alice Example" not in str(entry.get("text"))


def test_freeze_rechecks_redaction(tmp_path: Path, tmp_settings) -> None:
    workflow = tmp_path / "s.yaml"
    workflow.write_text(
        "name: sec\n"
        "nodes:\n"
        "  - id: t\n"
        "    type: transform\n"
        "    template: ok\n"
        "    output_key: summary\n",
        encoding="utf-8",
    )
    state = run_workflow_file(workflow, settings=tmp_settings, persist=True, record=True)
    cassette = Cassette.load(state.metadata["cassette"])
    # Inject a secret into the on-disk cassette as an attacker would.
    for row in cassette.entries.values():
        row["text"] = "leak sk-live-super-secret-value"
    dest = tmp_path / "out"
    freeze_run(
        state,
        cassette,
        out_dir=dest,
        workspace=tmp_path,
        secrets=["sk-live-super-secret-value"],
        allow_unsealed=True,
    )
    frozen = (dest / "cassette.json").read_text(encoding="utf-8")
    assert "sk-live-super-secret-value" not in frozen


def test_freeze_out_traversal_refused(tmp_path: Path, tmp_settings) -> None:
    workflow = tmp_path / "s.yaml"
    workflow.write_text(
        "name: sec\n"
        "nodes:\n"
        "  - id: t\n"
        "    type: transform\n"
        "    template: ok\n"
        "    output_key: summary\n",
        encoding="utf-8",
    )
    state = run_workflow_file(workflow, settings=tmp_settings, persist=True, record=True)
    cassette = Cassette.load(state.metadata["cassette"])
    with pytest.raises((PathError, CassetteError)):
        freeze_run(
            state,
            cassette,
            out_dir=tmp_path.parent / "escape",
            workspace=tmp_path,
            allow_unsealed=True,
        )


def test_tampered_cassette_is_still_labelled_replay(tmp_path: Path, tmp_settings) -> None:
    path = tmp_path / "agent.yaml"
    path.write_text(
        "name: agent-one\n"
        "nodes:\n"
        "  - id: a\n"
        "    type: agent\n"
        "    prompt: hi\n"
        "    model: mock:m\n"
        "    output_key: t\n",
        encoding="utf-8",
    )
    inner = ScriptedLLM()
    inner.enqueue("original", model="m")
    recorded = run_workflow_file(path, settings=tmp_settings, persist=True, record=True, llm=inner)
    cassette = Cassette.load(recorded.metadata["cassette"])
    for row in cassette.entries.values():
        row["text"] = "injected"
    cassette.save(Path(recorded.metadata["cassette"]))
    from readyagents.workflow.runner import replay_run

    replayed = replay_run(recorded.run_id, settings=tmp_settings, persist=True, offline=True)
    assert replayed.metadata.get("replay") is True
    assert replayed.output_keys["t"] == "injected"


def test_contains_secret_helper() -> None:
    assert contains_secret("bearer sk-abcdefghijk", ["sk-abcdefghijk"])
    assert not contains_secret("hello", ["sk-abcdefghijk"])
