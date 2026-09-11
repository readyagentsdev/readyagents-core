"""Adversarial health suite. Drive shipped APIs only; fail closed. No skip/xfail."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from readyagents.errors import ApprovalRequired
from readyagents.health.explain import write_explain_bundle
from readyagents.health.fingerprint import fingerprint
from readyagents.health.layout import HARD_MAX_RUNS
from readyagents.health.query import query_health
from readyagents.run_store import open_run_store
from readyagents.testing.helpers import ScriptedLLM
from readyagents.workflow.runner import run_workflow_file
from readyagents.workflow.schema import WorkflowSpec
from readyagents.workflow.state import NodeResult, RunState

_SECRET = "sk-abcdefghijksecret"
_GHP = "ghp_adversarialtokenvalue99"
_PATH = "/etc/passwd"
_EMAIL = "customer@example.com"


def _save(
    settings,
    *,
    workflow: str,
    node: str,
    error: str,
    cost: int = 1,
    run_id: str,
) -> None:
    state = RunState.start(workflow, {"draft": "x"}, run_id=run_id)
    state.results.append(
        NodeResult(node_id=node, type="agent", status="error", error=error, attempts=1)
    )
    state.errors.append(error)
    state.usage["cost_micros"] = cost
    state.finish("failed")
    store = open_run_store(settings)
    try:
        store.save(state)
    finally:
        store.close()


def test_fingerprint_and_explain_drop_secrets_and_paths(tmp_settings) -> None:
    message = f"{_SECRET} {_GHP} {_PATH} {_EMAIL} truncated max_tokens"
    fp = fingerprint(message, node_id="draft", secrets=[_SECRET, _GHP])
    blob = json.dumps(fp.as_dict())
    assert _SECRET not in blob
    assert _GHP not in blob
    assert _PATH not in blob
    assert "/etc/" not in blob
    assert _EMAIL not in blob
    _save(
        tmp_settings,
        workflow="adv",
        node="draft",
        error=message,
        cost=3,
        run_id="a" * 32,
    )
    store = open_run_store(tmp_settings)
    try:
        dest = write_explain_bundle(
            fp.id,
            store,
            out_dir=tmp_settings.workspace_path() / "adv-explain",
            workspace=tmp_settings.workspace_path(),
            settings=tmp_settings,
            secrets=[_SECRET, _GHP],
            confirm=True,
        )
    finally:
        store.close()
    tree = ""
    for path in dest.rglob("*"):
        if path.is_file():
            tree += path.read_text(encoding="utf-8", errors="replace")
    assert _SECRET not in tree
    assert _GHP not in tree
    assert _PATH not in tree
    assert _EMAIL not in tree


def test_induced_quarantine_gates_never_skips(tmp_settings) -> None:
    with pytest.raises(Exception, match="gate"):
        WorkflowSpec.model_validate(
            {
                "name": "skip-open",
                "nodes": [
                    {
                        "id": "draft",
                        "type": "transform",
                        "template": "x",
                        "output_key": "s",
                        "recovery": {
                            "health": {
                                "min_success_rate": 0.99,
                                "window": 3,
                                "below": "skip",
                            }
                        },
                    }
                ],
            }
        )
    for i in range(6):
        _save(
            tmp_settings,
            workflow="ind",
            node="draft",
            error="429 rate limit",
            run_id=f"{i:032x}",
        )
    flow = tmp_settings.workspace_path() / "ind.yaml"
    flow.write_text(
        "name: ind\n"
        "default_model: mock:x\n"
        "start: draft\n"
        "nodes:\n"
        "  - id: draft\n"
        "    type: agent\n"
        "    prompt: hi\n"
        "    recovery:\n"
        "      health: {min_success_rate: 0.9, window: 5, below: gate}\n"
        "  - id: done\n"
        "    type: transform\n"
        "    template: skipped-would-be-bad\n"
        "    output_key: summary\n",
        encoding="utf-8",
        newline="\n",
    )
    llm = ScriptedLLM()
    llm.enqueue(text="must-not-run")
    with pytest.raises(ApprovalRequired):
        run_workflow_file(flow, llm=llm, settings=tmp_settings, persist=True)
    assert not llm.calls


def test_untrusted_threshold_string_refused() -> None:
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate(
            {
                "name": "tmpl",
                "nodes": [
                    {
                        "id": "a",
                        "type": "transform",
                        "template": "x",
                        "output_key": "s",
                        "recovery": {
                            "health": {
                                "min_success_rate": "{{ rate }}",
                                "window": 5,
                                "below": "gate",
                            }
                        },
                    }
                ],
            }
        )


def test_health_query_does_not_scan_unbounded(tmp_settings) -> None:
    for i in range(24):
        _save(
            tmp_settings,
            workflow="dos",
            node="n",
            error="429 rate limit",
            run_id=f"{i + 100:032x}",
        )
    store = open_run_store(tmp_settings)
    try:
        report = query_health(store, workflow="dos", limit=5)
        huge = query_health(store, workflow="dos", limit=HARD_MAX_RUNS * 10)
    finally:
        store.close()
    assert report.scanned <= 5
    assert report.truncated is True
    assert huge.scanned <= HARD_MAX_RUNS
