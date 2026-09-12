"""CrewAI YAML or Python → IR. Python is AST-only."""

from __future__ import annotations

import ast
from typing import Any

import yaml

from readyagents.errors import ImportRefused
from readyagents.importers.ast_safe import const_str, parse_python
from readyagents.importers.bounds import check_depth, check_nodes, check_size
from readyagents.importers.ir import IntermediateEdge, IntermediateGraph, IntermediateNode
from readyagents.importers.n8n import drop_cycles
from readyagents.importers.secrets import find_secrets, strip_secrets
from readyagents.importers.slug import slug


def parse_crewai(text: str, *, filename: str = "crew.yaml") -> IntermediateGraph:
    check_size(text)
    name = str(filename or "").lower()
    stripped = text.lstrip()
    if name.endswith(".json") or stripped.startswith("{"):
        raise ImportRefused("CrewAI importer expects YAML or Python, not JSON", reason="malformed")
    # Honor the file suffix. Do not sniff Agent( — that string appears in YAML copy.
    if name.endswith((".yaml", ".yml")):
        return _parse_yaml(text)
    if name.endswith(".py") or _looks_python(stripped):
        return _parse_python(text, filename=filename)
    return _parse_yaml(text)


def _looks_python(text: str) -> bool:
    first = text.lstrip()
    return first.startswith("from ") or first.startswith("import ")


def _parse_yaml(text: str) -> IntermediateGraph:
    secrets = find_secrets(text)
    warnings: list[str] = []
    if secrets:
        warnings.append(
            "source export contains credential-like values; they were not written. "
            "The export file itself is a secret."
        )
        text = yaml.safe_dump(strip_secrets(yaml.safe_load(text) or {}), sort_keys=False)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as extra:
        raise ImportRefused(f"CrewAI YAML is malformed: {extra}", reason="malformed") from extra
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ImportRefused("CrewAI YAML must be a mapping", reason="malformed")
    check_depth(data)
    version = str(data.get("version") or "1")
    if version != "1":
        raise ImportRefused(f"unknown CrewAI schema version {version!r}", reason="version")
    nodes: list[IntermediateNode] = []
    edges: list[IntermediateEdge] = []
    agents = data.get("agents") if isinstance(data.get("agents"), dict) else {}
    for name, spec in agents.items():
        params = spec if isinstance(spec, dict) else {}
        ident = slug(str(name), prefix="crew")
        nodes.append(
            IntermediateNode(
                id=ident,
                kind="agent",
                title=str(name),
                params=dict(params),
            )
        )
    tasks = data.get("tasks")
    task_items: list[tuple[str, dict[str, Any]]] = []
    if isinstance(tasks, dict):
        task_items = [(str(k), v if isinstance(v, dict) else {}) for k, v in tasks.items()]
    elif isinstance(tasks, list):
        for index, item in enumerate(tasks):
            if isinstance(item, dict):
                task_items.append((str(item.get("name") or f"task_{index}"), item))
    prev: str | None = None
    for name, spec in task_items:
        ident = slug(name, prefix="task")
        kind = "human" if spec.get("human_input") else "task"
        nodes.append(
            IntermediateNode(
                id=ident,
                kind=kind,
                title=name,
                params=dict(spec),
                structural="human" if kind == "human" else None,
            )
        )
        if prev:
            edges.append(IntermediateEdge(prev, ident, kind="next"))
        prev = ident
    process = str(data.get("process") or "sequential").lower()
    if process in {"hierarchical", "async"}:
        nodes.append(
            IntermediateNode(
                id="crew_process",
                kind="crew.hierarchical",
                title="crew",
                structural="parallel",
            )
        )
    check_nodes(len(nodes))
    if not nodes:
        raise ImportRefused("CrewAI source has no agents or tasks", reason="malformed")
    start = nodes[0].id
    if task_items:
        start = slug(task_items[0][0], prefix="task")
    return IntermediateGraph(
        source="crewai",
        name=str(data.get("name") or "crewai-import"),
        version=version,
        nodes=nodes,
        edges=drop_cycles(edges)[0],
        start=start,
        warnings=warnings,
    )


def _parse_python(text: str, *, filename: str) -> IntermediateGraph:
    secrets = find_secrets(text)
    warnings: list[str] = []
    if secrets:
        warnings.append(
            "source export contains credential-like values; they were not written. "
            "The export file itself is a secret."
        )
    tree = parse_python(text, filename=filename)
    nodes: list[IntermediateNode] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        ctor = _ctor(node.func)
        if ctor not in {"Agent", "Task", "Crew"}:
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        if ctor == "Agent":
            role = const_str(kwargs.get("role")) or const_str(node.args[0] if node.args else None)
            if role is None:
                raise ImportRefused("Agent(role=) must be a string literal", reason="ast")
            ident = slug(role, prefix="crew")
            nodes.append(
                IntermediateNode(
                    id=ident,
                    kind="agent",
                    title=role,
                    params={"role": role, "goal": const_str(kwargs.get("goal")) or ""},
                )
            )
        elif ctor == "Task":
            desc = const_str(kwargs.get("description")) or const_str(
                node.args[0] if node.args else None
            )
            if desc is None:
                raise ImportRefused("Task(description=) must be a string literal", reason="ast")
            ident = slug(desc[:24], prefix="task")
            human = kwargs.get("human_input")
            kind = "human" if _truthy(human) else "task"
            nodes.append(
                IntermediateNode(
                    id=ident,
                    kind=kind,
                    title=desc[:48],
                    params={"description": desc},
                    structural="human" if kind == "human" else None,
                )
            )
        elif ctor == "Crew":
            process = const_str(kwargs.get("process")) or "sequential"
            if str(process).lower() in {"hierarchical", "async"}:
                nodes.append(
                    IntermediateNode(
                        id="crew_process",
                        kind="crew.hierarchical",
                        title="crew",
                        structural="parallel",
                    )
                )
    check_nodes(len(nodes))
    if not nodes:
        raise ImportRefused("CrewAI Python has no Agent/Task/Crew calls", reason="malformed")
    edges: list[IntermediateEdge] = []
    tasks = [n for n in nodes if n.kind in {"task", "human"}]
    for left, right in zip(tasks, tasks[1:], strict=False):
        edges.append(IntermediateEdge(left.id, right.id, kind="next"))
    start = tasks[0].id if tasks else nodes[0].id
    return IntermediateGraph(
        source="crewai",
        name="crewai-import",
        version="1",
        nodes=nodes,
        edges=edges,
        start=start,
        warnings=warnings,
    )


def _ctor(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _truthy(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True
