"""Every keyless shipped example runs to completion through its approval gates.

``--dry-run`` only proves a workflow parses and dispatches. This module runs
the shipped files from their real start, injecting approval with ``--approve``
(and ``readyagents decide`` when a gate needs a second actor). Anything that
is not a workflow, or that needs a key, network, browser UI, or input other
than approvals, is named in ``SKIP`` with a reason. A new shipped file that
is in neither set fails this module.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.examples import list_examples
from readyagents.workflow.runner import load_workflow

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
runner = CliRunner()

# Actor id is also treated as a held role, so this satisfies
# feedback_gate's approver_roles: [editor]. Enterprise gates ignore a
# nameless --approve.
_ACTOR = "editor"
_MORE_ACTORS = ("bob", "carol", "dave")
_PAUSE_NODE = re.compile(r"Approval required at node '([^']+)'")
_PAUSE_RUN = re.compile(r"resume ([0-9a-f]{16,})")
_STRIP_ENV = (
    "TYPESAFE_API_KEY",
    "READYAGENTS_TYPESAFE_API_KEY",
    "OPENAI_API_KEY",
    "READYAGENTS_OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "READYAGENTS_ANTHROPIC_API_KEY",
    "READYAGENTS_POLICY",
    "READYAGENTS_ACTOR",
)

# Sibling examples/readyagents.policy.yaml gates write_file on tainted input.
# These nodes are not type: approval; --approve is still the shipped way through.
_POLICY_APPROVES: dict[str, tuple[str, ...]] = {
    "gated_write.yaml": ("write",),
    "policy_gated.yaml": ("write",),
}

# Reviewable omissions. Every other shipped example is executed below.
SKIP: dict[str, str] = {
    "a2a_delegate.yaml": "live run fetches a remote Agent Card (network)",
    "agent_tools.yaml": "type: agent needs an API key",
    "batch_echo.yaml": "readyagents run requires input n; the shipped entry point is readyagents batch",
    "batch_rows.csv": "row data, not a workflow",
    "batch_rows.jsonl": "row data, not a workflow",
    "bench/cassette.json": "cassette fixture, not a workflow",
    "bench/suite.yaml": "bench suite, not a workflow",
    "browser_statement.yaml": "type: browser needs the optional browser driver",
    "code_review.yaml": "type: agent needs an API key",
    "connector_demo.yaml": "needs --pack examples/packs/connector_pack.py, not an approval",
    "connectors/fixtures/message/send.json": "connector fixture, not a workflow",
    "connectors/fixtures/support/create_ticket.json": "connector fixture, not a workflow",
    "connectors/fixtures/support/list_tickets.json": "connector fixture, not a workflow",
    "connectors/support.yaml": "connector config, not a workflow",
    "converse_order.yaml": "needs a converse reply, not an approval",
    "document_pages.yaml": "needs invoice.pdf; documented as validate-only",
    "env/readyagents.env.yaml": "environment pin, not a workflow",
    "eval/fail.yaml": "eval suite (intentional failure), not a workflow run",
    "eval/pass.yaml": "eval suite, not a workflow run",
    "import/n8n_demo.json": "n8n import, not a workflow",
    "knowledge_ingest.yaml": "ingest source docs is not in the workflow workspace; validate-only",
    "mcp_http_client.py": "client script, not a workflow",
    "mcp_tasks_client.py": "client script, not a workflow",
    "optimize/classify.yaml": "type: agent needs an API key",
    "optimize/suite.yaml": "optimize suite, not a workflow",
    "packs/browser_pack.py": "tool pack, not a workflow",
    "packs/connector_pack.py": "tool pack, not a workflow",
    "packs/hitl_gate.py": "tool pack, not a workflow",
    "packs/voice_pack.py": "tool pack, not a workflow",
    "readyagents.policy.yaml": "firewall policy, not a workflow",
    "research_brief.yaml": "type: agent needs an API key",
    "sample_code.py": "sample source, not a workflow",
    "skill_style.yaml": "skill house-writing-style is not installed",
    "skills/house-writing-style/SKILL.md": "skill package file, not a workflow",
    "skills/house-writing-style/scripts/apply.py": "skill script, not a workflow",
    "sqlite_run_store.md": "documentation, not a workflow",
    "support_triage.yaml": "type: agent needs an API key",
    "table_pipeline.yaml": "needs exports.csv; documented as validate-only",
    "trigger_support.yaml": "trigger contract; run needs event input text",
    "wait_inbox.yaml": "type: wait parks until readyagents wake, not an approval",
}


def _walk(node: Any):
    yield node
    for branch in getattr(node, "branches", None) or []:
        yield from _walk(branch)
    body = getattr(node, "body", None)
    if body is not None:
        yield from _walk(body)
    for member in getattr(node, "members", None) or []:
        if hasattr(member, "type"):
            yield from _walk(member)


def _approval_nodes(spec: Any) -> list[Any]:
    found: list[Any] = []
    for node in spec.nodes:
        for item in _walk(node):
            if str(getattr(item, "type", "")).lower() == "approval":
                found.append(item)
    return found


def _blob(result: Any) -> str:
    return f"{result.stdout}\n{result.stderr or ''}"


def _prepare(monkeypatch: pytest.MonkeyPatch, home: Path, workspace: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("READYAGENTS_HOME", str(home))
    monkeypatch.setenv("READYAGENTS_WORKSPACE", str(workspace))
    for key in _STRIP_ENV:
        monkeypatch.delenv(key, raising=False)
    clear_settings_cache()


def test_keyless_skip_list_names_only_shipped_files() -> None:
    shipped = set(list_examples())
    unknown = sorted(set(SKIP) - shipped)
    assert unknown == [], f"skip list names files that are not shipped: {unknown}"
    assert "decide_triage.yaml" not in SKIP
    assert all(reason.strip() for reason in SKIP.values())


@pytest.mark.parametrize("rel", list_examples())
def test_keyless_shipped_example_completes(rel: str, tmp_path: Path, monkeypatch) -> None:
    reason = SKIP.get(rel)
    if reason is not None:
        pytest.skip(reason)

    path = EXAMPLES / rel
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    workspace.mkdir(parents=True, exist_ok=True)
    if rel == "connector_rest.yaml":
        # rest resolves connector_config and fixtures against the workspace.
        shutil.copytree(EXAMPLES / "connectors", workspace / "connectors")
    _prepare(monkeypatch, home, workspace)

    spec = load_workflow(path)
    approvals = _approval_nodes(spec)
    needed = 1
    approve: list[str] = []
    for node in approvals:
        approve.append(node.id)
        required = int(node.approvals_required or 1)
        if required > needed:
            needed = required
    for node_id in _POLICY_APPROVES.get(rel, ()):
        if node_id not in approve:
            approve.append(node_id)

    args = ["run", str(path), "--actor", _ACTOR]
    if needed == 1:
        args.append("--no-persist")
    for node_id in approve:
        args.extend(["--approve", node_id])
    result = runner.invoke(app, args)
    votes = 1
    while result.exit_code == 2 and votes < needed:
        blob = _blob(result)
        node_match = _PAUSE_NODE.search(blob)
        run_match = _PAUSE_RUN.search(blob)
        assert node_match and run_match, blob
        node_id, run_id = node_match.group(1), run_match.group(1)
        clear_settings_cache()
        result = runner.invoke(
            app,
            [
                "decide",
                run_id,
                "--node",
                node_id,
                "--decision",
                "approve",
                "--actor",
                _MORE_ACTORS[votes - 1],
            ],
        )
        votes += 1

    blob = _blob(result)
    assert result.exit_code == 0, blob
    assert "succeeded" in blob
    assert "Missing template variable" not in blob
    assert "--dry-run" not in args
