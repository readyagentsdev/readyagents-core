"""Export a workflow as an open-format skill folder. No secrets, no local paths."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from readyagents.errors import SkillRefused
from readyagents.skills.parse import parse_skill_md
from readyagents.workflow.runner import load_workflow
from readyagents.workflow.schema import WorkflowSpec

_SECRET_KEYS = re.compile(
    r"(api[_-]?key|secret|token|password|private[_-]?key|openai|anthropic)",
    re.I,
)
_ABS_PATH = re.compile(r"(^|[\s\"'=])(/Users/|/home/|[A-Za-z]:\\)")
_IDENTITY = re.compile(r"(operator|actor@|READYAGENTS_ACTOR)", re.I)


def export_workflow(path: Path | str, dest: Path | str) -> Path:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise SkillRefused(f"workflow not found: {source}", reason="missing")
    workflow = load_workflow(source)
    name = _skill_name(workflow.name)
    out = Path(dest).expanduser()
    folder = out / name
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, folder / "workflow.yaml")
    (folder / "scripts").mkdir(exist_ok=True)
    (folder / "scripts" / "run.sh").write_text(_run_script(), encoding="utf-8", newline="\n")
    skill_md = _skill_md(workflow, name)
    (folder / "SKILL.md").write_text(skill_md, encoding="utf-8", newline="\n")
    (folder / "README.md").write_text(_readme(workflow, name), encoding="utf-8", newline="\n")
    _assert_clean(folder)
    parse_skill_md((folder / "SKILL.md").read_text(encoding="utf-8"), directory_name=name)
    return folder


def _skill_name(workflow_name: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "-", workflow_name.lower()).strip("-")
    token = re.sub(r"-{2,}", "-", token)
    return (token or "workflow")[:64]


def _skill_md(workflow: WorkflowSpec, name: str) -> str:
    required = list(workflow.required_inputs or [])
    gates = [n.id for n in workflow.nodes if str(n.type) == "approval"]
    budget = workflow.budget
    bits = [
        f"Run the ReadyAgents workflow `{workflow.name}` via `scripts/run.sh`.",
        "Governance: local one-shot engine, audit log, policy-filtered tools.",
    ]
    if budget and (budget.max_cost_usd is not None or budget.max_tokens is not None):
        bits.append("This workflow declares a token/cost budget ceiling.")
    if gates:
        bits.append(f"Approval gates: {', '.join(gates)}.")
    if required:
        bits.append("Required inputs: " + ", ".join(required) + ".")
    description = " ".join(bits)
    if len(description) > 1024:
        description = description[:1021] + "..."
    body = "\n".join(
        [
            f"# {workflow.name}",
            "",
            (workflow.description or "").strip() or "Exported ReadyAgents workflow.",
            "",
            "## Invoke",
            "",
            "```sh",
            "scripts/run.sh",
            "```",
            "",
            "The script shells to `readyagents run workflow.yaml` in this folder.",
            "Approvals, budgets, and the audit log still apply.",
            "",
        ]
    )
    front = [
        "---",
        f"name: {name}",
        f"description: {json.dumps(description)}",
        "license: Apache-2.0",
        "metadata:",
        "  exported-from: readyagents-workflow",
        "---",
        "",
    ]
    return "\n".join(front) + body


def _run_script() -> str:
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        'DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\n'
        'exec readyagents run "$DIR/workflow.yaml" "$@"\n'
    )


def _readme(workflow: WorkflowSpec, name: str) -> str:
    return (
        f"# {name}\n\n"
        f"Exported from ReadyAgents workflow `{workflow.name}`.\n"
        "This skill folder is the open Agent Skills format. "
        "Running it still answers to the workflow's policy, budget, and audit log.\n"
    )


def _assert_clean(folder: Path) -> None:
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if _SECRET_KEYS.search(text) and path.suffix in {".yaml", ".yml", ".md", ".sh", ".env"}:
            if any(k in text.lower() for k in ("api_key:", "sk-", "secret:")):
                raise SkillRefused("export would embed a secret", reason="secret")
        if _ABS_PATH.search(text):
            raise SkillRefused("export would embed an absolute local path", reason="path")
        if _IDENTITY.search(text) and "READYAGENTS_ACTOR=" in text:
            raise SkillRefused("export would embed an operator identity", reason="identity")
