"""policy check / policy explain."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from readyagents.firewall.enforce import ToolRequest, evaluate
from readyagents.firewall.policy_file import Policy, load_policy
from readyagents.workflow.schema import WorkflowSpec
from readyagents.workflow.state import RunState


def check_policy(path: Path | str) -> Policy:
    return load_policy(path)


def explain_workflow(workflow: WorkflowSpec, policy: Policy | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    dummy = RunState.start(workflow.name, {})
    for node in workflow.nodes:
        if str(node.type) != "tool":
            continue
        name = node.tool or ""
        decision = evaluate(
            ToolRequest(name=name, arguments=dict(node.arguments or {}), node_id=node.id),
            dummy,
            policy,
        )
        rows.append(
            {
                "node": node.id,
                "tool": name,
                "action": decision.action,
                "rule": decision.rule,
                "reason": decision.reason,
            }
        )
    return rows
