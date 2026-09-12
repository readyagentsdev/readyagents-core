"""IR + mapping table → ReadyAgents workflow dict and fidelity report."""

from __future__ import annotations

from typing import Any

from readyagents.importers.ir import IntermediateGraph, IntermediateNode
from readyagents.importers.report import FidelityReport, NodeReport
from readyagents.importers.secrets import redact_text, strip_secrets
from readyagents.importers.table import MappingEntry, MappingTable

_STUB = (
    "UNSUPPORTED: {source} '{title}' node ({kind}). {reason} "
    "Nearest: {nearest}. Original parameters are in the fidelity report. "
    "Remove this node to proceed."
)


def emit(graph: IntermediateGraph, table: MappingTable) -> tuple[dict[str, Any], FidelityReport]:
    """Build a workflow mapping and a report. Never silent-drops a source node."""
    name = redact_text(graph.name or "imported")
    report = FidelityReport(source=graph.source, name=name, warnings=list(graph.warnings))
    outgoing: dict[str, list[tuple[str, str]]] = {}
    for edge in graph.edges:
        outgoing.setdefault(edge.source, []).append((edge.kind, edge.target))

    nodes_out: list[dict[str, Any]] = []
    for item in graph.nodes:
        entry = table.lookup(item.kind)
        status = _status(entry, item)
        reason = entry.reason
        if item.structural and status != "unsupported":
            reason = (
                f"{entry.reason} Structural {item.structural} semantics differ "
                "from ReadyAgents; categorised approximated."
            )
            if status == "translated":
                status = "approximated"
        title = redact_text(item.title)
        row = NodeReport(
            id=item.id,
            kind=item.kind,
            title=title,
            status=status,
            reason=reason,
            nearest=entry.nearest,
            structural=item.structural or entry.structural,
        )
        report.nodes.append(row)
        node = _emit_node(graph.source, item, entry, status, outgoing.get(item.id) or [])
        nodes_out.append(node)
        if status == "unsupported":
            report.follow_ups.append(
                f"Replace stub {item.id} ({title}: {item.kind}). {entry.nearest or reason}"
            )
        elif status == "approximated":
            report.follow_ups.append(f"Review approximated node {item.id}: {reason}")

    extra: list[dict[str, Any]] = []
    for node in nodes_out:
        if node.get("type") == "approval" and not (
            node.get("next") or node.get("then") or node.get("else")
        ):
            done_id = f"{node['id']}_done"
            node["next"] = done_id
            extra.append(
                {
                    "id": done_id,
                    "type": "transform",
                    "template": "approved",
                    "output_key": done_id,
                }
            )
    nodes_out.extend(extra)

    start = graph.start or (nodes_out[0]["id"] if nodes_out else "start")
    workflow: dict[str, Any] = {
        "name": name,
        "description": (
            f"Imported from {graph.source}. Structural translation only — test before use."
        ),
        "start": start,
        "nodes": nodes_out,
    }
    if graph.credential_names:
        workflow["description"] += (
            " Declared secret names: " + ", ".join(graph.credential_names) + "."
        )
    return workflow, report


def _status(entry: MappingEntry, item: IntermediateNode) -> str:
    if entry.target is None or entry.fidelity == "unsupported":
        return "unsupported"
    if item.structural or entry.fidelity == "approximated":
        return "approximated"
    return "translated"


def _emit_node(
    source: str,
    item: IntermediateNode,
    entry: MappingEntry,
    status: str,
    edges: list[tuple[str, str]],
) -> dict[str, Any]:
    then = _first(edges, "then")
    else_ = _first(edges, "else")
    nxt = _first(edges, "next") or _first(edges, "error")
    if status == "unsupported" or not entry.target:
        node: dict[str, Any] = {
            "id": item.id,
            "type": "transform",
            "template": _STUB.format(
                source=source,
                title=redact_text(item.title),
                kind=item.kind,
                reason=entry.reason,
                nearest=entry.nearest or "a connector, webhook, or type: code",
            ),
            "output_key": f"{item.id}_unsupported",
        }
        if nxt:
            node["next"] = nxt
        return node
    node = {"id": item.id, "type": entry.target}
    params = dict(entry.params or {})
    safe = strip_secrets(item.params)
    if entry.target == "transform":
        template = str(params.get("template") or item.title or item.id)
        node["template"] = template
        node["output_key"] = str(params.get("output_key") or item.id)
    elif entry.target == "tool":
        node["tool"] = str(params.get("tool") or "now")
        args = params.get("arguments")
        node["arguments"] = dict(args) if isinstance(args, dict) else {}
    elif entry.target == "condition":
        node["when"] = str(params.get("when") or "true")
        if then:
            node["then"] = then
        if else_:
            node["else"] = else_
        if nxt and not then:
            node["then"] = nxt
        if "then" not in node and "else" not in node:
            node["type"] = "transform"
            node["template"] = "branch"
            node["output_key"] = item.id
            node.pop("when", None)
    elif entry.target == "approval":
        node["prompt"] = str(params.get("prompt") or f"Approve imported step {item.title}")
    elif entry.target == "foreach":
        node["items"] = str(params.get("items") or "{{items}}")
        node["body"] = {
            "id": f"{item.id}_body",
            "type": "transform",
            "template": "item",
            "output_key": f"{item.id}_item",
        }
    elif entry.target == "parallel":
        node["branches"] = [
            {
                "id": f"{item.id}_a",
                "type": "transform",
                "template": "branch-a",
                "output_key": f"{item.id}_a",
            },
            {
                "id": f"{item.id}_b",
                "type": "transform",
                "template": "branch-b",
                "output_key": f"{item.id}_b",
            },
        ]
    elif entry.target == "include":
        node["path"] = str(params.get("path") or "imported_sub.yaml")
    elif entry.target == "agent":
        prompt = (
            safe.get("description")
            or safe.get("goal")
            or safe.get("role")
            or params.get("prompt")
            or item.title
        )
        node["prompt"] = redact_text(str(prompt))
        node["output_key"] = str(params.get("output_key") or item.id)
    elif entry.target == "wait":
        node["seconds"] = 0
    if entry.target != "condition" and nxt:
        node["next"] = nxt
    if then and entry.target != "condition":
        node.setdefault("next", then)
    return node


def _first(edges: list[tuple[str, str]], kind: str) -> str | None:
    for edge_kind, target in edges:
        if edge_kind == kind:
            return target
    return None
