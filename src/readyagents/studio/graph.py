"""Interactive graph payload from the shipped workflow model. Executes nothing."""

from __future__ import annotations

import html
from typing import Any

from readyagents.workflow.schema import NodeSpec, WorkflowSpec
from readyagents.workflow.source_map import locate_node_field


def workflow_graph(
    spec: WorkflowSpec,
    *,
    source_path: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Nodes, edges, and construct tags for nested parallel / foreach / include."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(node: NodeSpec, *, parent: str | None, relation: str | None) -> None:
        if node.id in seen:
            return
        seen.add(node.id)
        kind = str(node.type)
        loc = None
        if source_path:
            pos = locate_node_field(source_path, node.id, "id", source=source)
            if pos is not None:
                loc = {"line": pos.line, "column": pos.column, "file": str(pos.path)}
        row: dict[str, Any] = {
            "id": node.id,
            "type": kind,
            "label": display_label(node.description or node.id),
            "description": display_label(node.description or ""),
            "parent": parent,
            "relation": relation,
            "yaml": loc,
        }
        if kind == "include":
            row["include_path"] = node.path
        if kind == "foreach":
            row["items"] = node.items
        if kind == "parallel":
            row["branch_ids"] = [b.id for b in (node.branches or [])]
        nodes.append(row)
        if node.next:
            edges.append({"from": node.id, "to": node.next, "kind": "next"})
        if node.then:
            edges.append({"from": node.id, "to": node.then, "kind": "then"})
        if node.else_:
            edges.append({"from": node.id, "to": node.else_, "kind": "else"})
        for branch in node.branches or []:
            edges.append({"from": node.id, "to": branch.id, "kind": "branch"})
            walk(branch, parent=node.id, relation="branch")
        if node.body is not None:
            edges.append({"from": node.id, "to": node.body.id, "kind": "body"})
            walk(node.body, parent=node.id, relation="body")

    for node in spec.nodes:
        walk(node, parent=None, relation=None)
    if spec.edges:
        for edge in spec.edges:
            frm = getattr(edge, "from_", None)
            to = getattr(edge, "to", None)
            if frm and to:
                edges.append({"from": frm, "to": to, "kind": "edge"})
    constructs = sorted({row["type"] for row in nodes})
    return {
        "name": spec.name,
        "start": spec.start,
        "nodes": nodes,
        "edges": edges,
        "constructs": constructs,
    }


def display_label(raw: Any) -> str:
    """Escape untrusted workflow text so a canvas cannot execute it."""
    text = " ".join(str(raw or "").split())
    return html.escape(text, quote=True)


def node_form_fields(node: NodeSpec) -> dict[str, Any]:
    """Shipped JSON-Schema field names for this node's type."""
    from readyagents.workflow.jsonschema import NODE_TYPE_FIELDS
    from readyagents.workflow.schema import NodeType

    kind = str(node.type)
    try:
        enum = NodeType(kind)
        fields = list(NODE_TYPE_FIELDS.get(enum) or ())
    except ValueError:
        fields = []
    common = ["id", "type", "description", "next", "output_key", "timeout_seconds"]
    values: dict[str, Any] = {}
    for name in common + fields:
        attr = "else_" if name == "else" else ("call_inputs" if name == "inputs" else name)
        if hasattr(node, attr):
            values[name] = getattr(node, attr)
    return {"id": node.id, "type": kind, "fields": common + fields, "values": values}
