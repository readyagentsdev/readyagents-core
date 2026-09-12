"""LangGraph declarative subset → IR. AST only; never import the module."""

from __future__ import annotations

import ast

from readyagents.errors import ImportRefused
from readyagents.importers.ast_safe import const_str, parse_python
from readyagents.importers.bounds import check_nodes, check_size
from readyagents.importers.ir import IntermediateEdge, IntermediateGraph, IntermediateNode
from readyagents.importers.n8n import drop_cycles
from readyagents.importers.secrets import find_secrets
from readyagents.importers.slug import slug


def parse_langgraph(text: str, *, filename: str = "graph.py") -> IntermediateGraph:
    check_size(text)
    secrets = find_secrets(text)
    warnings: list[str] = []
    if secrets:
        warnings.append(
            "source export contains credential-like values; they were not written. "
            "The export file itself is a secret."
        )
    tree = parse_python(text, filename=filename)
    nodes_meta: dict[str, IntermediateNode] = {}
    edges: list[IntermediateEdge] = []
    start: str | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _attr(node.func)
        if name.endswith("add_node") or name == "add_node":
            ident = const_str(node.args[0] if node.args else None)
            if ident is None:
                raise ImportRefused(
                    "add_node() requires a string literal name; non-static names are refused",
                    reason="ast",
                )
            nid = slug(ident, prefix="lg")
            kind = "interrupt" if ident.lower() in {"human", "interrupt"} else "node"
            nodes_meta[nid] = IntermediateNode(
                id=nid, kind=kind, title=ident, structural=_struct(kind)
            )
        elif name.endswith("add_edge") or name == "add_edge":
            if len(node.args) < 2:
                raise ImportRefused("add_edge() needs two arguments", reason="ast")
            src_s, dst_s = const_str(node.args[0]), const_str(node.args[1])
            if src_s is None or dst_s is None:
                raise ImportRefused(
                    "add_edge() endpoints must be string literals or START/END",
                    reason="ast",
                )
            if src_s == "START":
                if dst_s != "END":
                    start = slug(dst_s, prefix="lg")
                continue
            if dst_s == "END":
                continue
            edges.append(
                IntermediateEdge(slug(src_s, prefix="lg"), slug(dst_s, prefix="lg"), kind="next")
            )
        elif name.endswith("add_conditional_edges") or name == "add_conditional_edges":
            src_s = const_str(node.args[0] if node.args else None)
            if src_s is None:
                raise ImportRefused(
                    "add_conditional_edges() source must be a string literal",
                    reason="ast",
                )
            cond_id = slug(src_s + "_route", prefix="lg")
            nodes_meta[cond_id] = IntermediateNode(
                id=cond_id, kind="condition", title=f"{src_s} router", structural="branch"
            )
            src = slug(src_s, prefix="lg")
            edges.append(IntermediateEdge(src, cond_id, kind="next"))
            mapping = _dict_map(node)
            keys = list(mapping)
            if keys:
                then = mapping[keys[0]]
                if then and then != "END":
                    edges.append(IntermediateEdge(cond_id, slug(then, prefix="lg"), kind="then"))
            if len(keys) > 1:
                els = mapping[keys[1]]
                if els and els != "END":
                    edges.append(IntermediateEdge(cond_id, slug(els, prefix="lg"), kind="else"))
        elif name.endswith("add_subgraph") or name == "add_subgraph":
            ident = const_str(node.args[0] if node.args else None) or "subgraph"
            nid = slug(ident, prefix="lg")
            nodes_meta[nid] = IntermediateNode(
                id=nid, kind="subgraph", title=ident, structural="subworkflow"
            )
    if not nodes_meta:
        raise ImportRefused("LangGraph source has no add_node() calls", reason="malformed")
    check_nodes(len(nodes_meta))
    nodes = list(nodes_meta.values())
    if start is None:
        start = nodes[0].id
    kept, skipped = drop_cycles(edges)
    if skipped:
        nodes.append(
            IntermediateNode(id="cycle_broken", kind="loop", title="cycle", structural="loop")
        )
    return IntermediateGraph(
        source="langgraph",
        name="langgraph-import",
        version="1",
        nodes=nodes,
        edges=kept,
        start=start,
        warnings=warnings,
    )


def _attr(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _struct(kind: str) -> str | None:
    return {
        "condition": "branch",
        "loop": "loop",
        "parallel": "parallel",
        "subgraph": "subworkflow",
        "error": "error",
        "interrupt": "human",
    }.get(kind)


def _dict_map(call: ast.Call) -> dict[str, str]:
    if len(call.args) >= 3 and isinstance(call.args[2], ast.Dict):
        out: dict[str, str] = {}
        for key, val in zip(call.args[2].keys, call.args[2].values, strict=False):
            ks, vs = const_str(key), const_str(val)
            if ks is None or vs is None:
                raise ImportRefused(
                    "conditional edge maps must use string literals",
                    reason="ast",
                )
            out[ks] = vs
        return out
    for kw in call.keywords:
        if kw.arg in {"path_map", "then"} and isinstance(kw.value, ast.Dict):
            fake = ast.Call(func=call.func, args=[*call.args, kw.value], keywords=[])
            return _dict_map(fake)
    return {}
