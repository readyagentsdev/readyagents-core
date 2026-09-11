"""Deterministic case generation from a workflow's own declarations. No model."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from readyagents.firewall.detect import injection_examples
from readyagents.simulate.layout import DEFAULT_CASES, MAX_STRING
from readyagents.workflow.schema import NodeSpec, WorkflowSpec


@dataclass
class SimCase:
    name: str
    inputs: dict[str, Any]
    decisions: dict[str, str] = field(default_factory=dict)
    tag: str = "baseline"


def generate_cases(
    workflow: WorkflowSpec,
    *,
    seed: int = 42,
    cap: int = DEFAULT_CASES,
) -> list[SimCase]:
    """Pure function of (workflow document, seed, cap). Never calls a provider."""
    names = _input_names(workflow)
    defaults = dict(workflow.input_defaults())
    cases: list[SimCase] = []
    cases.append(SimCase(name="baseline-defaults", inputs=dict(defaults), tag="baseline"))
    for name in names:
        for tag, value in _variants_for(name, defaults.get(name)):
            row = dict(defaults)
            row[name] = value
            cases.append(SimCase(name=f"{tag}-{name}", inputs=row, tag=tag))
    cases.extend(_branch_cases(workflow, defaults))
    cases.extend(_approval_cases(workflow, defaults))
    cases.extend(_foreach_cases(workflow, defaults))
    # Stable order then seeded sample if over cap.
    cases.sort(key=lambda item: item.name)
    deduped: list[SimCase] = []
    seen: set[str] = set()
    for item in cases:
        if item.name in seen:
            continue
        seen.add(item.name)
        deduped.append(item)
    if cap > 0 and len(deduped) > cap:
        rng = random.Random(seed)
        picked = rng.sample(deduped, cap)
        picked.sort(key=lambda item: item.name)
        return picked
    return deduped


def generate_from_path(
    path: Path | str,
    *,
    seed: int = 42,
    cap: int = DEFAULT_CASES,
) -> list[SimCase]:
    from readyagents.workflow.runner import load_workflow

    return generate_cases(load_workflow(path), seed=seed, cap=cap)


def _input_names(workflow: WorkflowSpec) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for name in list(workflow.required_inputs) + list(workflow.inputs.keys()):
        token = str(name).strip()
        if not token or token in seen:
            continue
        seen.add(token)
        names.append(token)
    return names


def _variants_for(name: str, default: Any) -> list[tuple[str, Any]]:
    del name
    variants: list[tuple[str, Any]] = [
        ("empty", ""),
        ("maximal", "M" * MAX_STRING),
        ("wrong-type-int", 0),
        ("wrong-type-list", [1]),
        ("unicode", "日本語 café"),
        ("control", "\x00\x1f"),
        ("secret-shaped", "sk-abcdefghijksecret"),
    ]
    for index, example in enumerate(injection_examples()):
        variants.append((f"injection-{index}", example))
    if default is not None and default != "":
        variants.append(("wrong-type-bool", True))
    return variants


def _branch_cases(workflow: WorkflowSpec, defaults: dict[str, Any]) -> list[SimCase]:
    out: list[SimCase] = []
    for node in _walk(workflow.nodes):
        if str(node.type) != "condition" or not node.when:
            continue
        for label, value in _condition_probes(node.when):
            row = dict(defaults)
            key = _first_input_in_expr(node.when, row) or _first_required(row)
            if key is None:
                continue
            row[key] = value
            out.append(
                SimCase(
                    name=f"branch-{node.id}-{label}",
                    inputs=row,
                    tag="branch",
                )
            )
    return out


def _approval_cases(workflow: WorkflowSpec, defaults: dict[str, Any]) -> list[SimCase]:
    out: list[SimCase] = []
    for node in _walk(workflow.nodes):
        if str(node.type) != "approval":
            continue
        for vote in ("approve", "reject"):
            out.append(
                SimCase(
                    name=f"approval-{node.id}-{vote}",
                    inputs=dict(defaults),
                    decisions={node.id: vote},
                    tag="approval",
                )
            )
    return out


def _foreach_cases(workflow: WorkflowSpec, defaults: dict[str, Any]) -> list[SimCase]:
    out: list[SimCase] = []
    for node in _walk(workflow.nodes):
        if str(node.type) != "foreach":
            continue
        key = _foreach_input_name(node, defaults)
        if key is None:
            continue
        out.append(
            SimCase(
                name=f"foreach-{node.id}-empty",
                inputs={**defaults, key: []},
                tag="foreach",
            )
        )
        out.append(
            SimCase(
                name=f"foreach-{node.id}-items",
                inputs={**defaults, key: ["a", "b"]},
                tag="foreach",
            )
        )
    return out


def _foreach_input_name(node: NodeSpec, defaults: dict[str, Any]) -> str | None:
    raw = str(getattr(node, "items", None) or "")
    for key in defaults:
        if key and key in raw:
            return key
    if "items" in defaults:
        return "items"
    return "items" if raw else None


def _condition_probes(expr: str) -> list[tuple[str, Any]]:
    text = expr.strip()
    if "==" in text:
        rhs = text.split("==", 1)[1].strip().strip("'\"")
        other = "no" if rhs == "yes" else "yes"
        return [("then", rhs), ("else", other)]
    return [("truthy", True), ("falsey", False)]


def _first_input_in_expr(expr: str, defaults: dict[str, Any]) -> str | None:
    for key in defaults:
        if key and key in expr:
            return key
    return None


def _first_required(defaults: dict[str, Any]) -> str | None:
    return next(iter(defaults), None)


def _walk(nodes: list[NodeSpec]) -> list[NodeSpec]:
    out: list[NodeSpec] = []
    for node in nodes:
        out.append(node)
        if node.branches:
            out.extend(_walk(list(node.branches)))
        body = getattr(node, "body", None)
        if body is not None:
            out.extend(_walk([body]))
    return out
