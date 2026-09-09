"""Deterministic Mermaid export. Executes nothing. YAML text is untrusted."""

from __future__ import annotations

import re

from readyagents.workflow.schema import NodeSpec, WorkflowSpec

_MAX_LABEL = 80
_UNSAFE = re.compile(r"[<>]|https?://|javascript:|%%|click\s", re.IGNORECASE)


def render_mermaid(workflow: WorkflowSpec, *, direction: str = "LR") -> str:
    """Render a workflow's routing as Mermaid. No include expansion, no packs."""
    heading = "TD" if str(direction).upper() in {"TD", "TB"} else "LR"
    nodes = _flatten(list(workflow.nodes))
    index = {node.id: f"n{i}" for i, node in enumerate(nodes)}
    lines = [f"flowchart {heading}"]
    for i, node in enumerate(nodes):
        label = _label(node)
        shape = _shape(node, index[node.id], label)
        lines.append(f"  {shape}")
        _ = i
    for node in nodes:
        src = index[node.id]
        for dest in _successors(node):
            if dest in index:
                lines.append(f"  {src} --> {index[dest]}")
        for branch in list(node.branches or []):
            if branch.id in index:
                lines.append(f"  {src} --> {index[branch.id]}")
        body = node.body
        if body is not None and body.id in index:
            lines.append(f"  {src} --> {index[body.id]}")
    if workflow.edges:
        for edge in workflow.edges:
            frm = getattr(edge, "from_", None) or getattr(edge, "source", None)
            to = getattr(edge, "to", None) or getattr(edge, "target", None)
            if frm in index and to in index:
                lines.append(f"  {index[frm]} --> {index[to]}")
    return "\n".join(lines) + "\n"


def _label(node: NodeSpec) -> str:
    raw = node.description or node.id
    text = " ".join(str(raw).split())
    text = _UNSAFE.sub("", text)
    text = text.replace('"', "'").replace("[", "(").replace("]", ")")
    text = text.replace("{", "(").replace("}", ")")
    if len(text) > _MAX_LABEL:
        text = text[: _MAX_LABEL - 1] + "…"
    return text or node.id


def _shape(node: NodeSpec, ident: str, label: str) -> str:
    kind = str(node.type)
    quoted = f'{ident}["{label}"]'
    if kind == "approval":
        return f'{ident}{{"{label}"}}'
    if kind == "condition":
        return f'{ident}{{"{label}"}}'
    if kind == "parallel":
        return f'{ident}[["{label}"]]'
    _ = quoted
    return f'{ident}["{kind}: {label}"]'


def _flatten(nodes: list[NodeSpec]) -> list[NodeSpec]:
    out: list[NodeSpec] = []
    seen: set[str] = set()
    for node in nodes:
        if node.id in seen:
            continue
        seen.add(node.id)
        out.append(node)
        out.extend(_flatten(list(node.branches or [])))
        if node.body is not None:
            out.extend(_flatten([node.body]))
    return out


def _successors(node: NodeSpec) -> list[str]:
    found: list[str] = []
    for attr in ("next", "then", "else_"):
        value = getattr(node, attr, None)
        if isinstance(value, str) and value and value not in found:
            found.append(value)
    return found
