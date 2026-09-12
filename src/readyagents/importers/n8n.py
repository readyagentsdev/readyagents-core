"""n8n workflow JSON → intermediate graph. Parse only."""

from __future__ import annotations

import json
import re
from typing import Any

from readyagents.errors import ImportBoundNodes, ImportRefused
from readyagents.importers.bounds import check_depth, check_nodes, check_size
from readyagents.importers.ir import IntermediateEdge, IntermediateGraph, IntermediateNode
from readyagents.importers.secrets import find_secrets, strip_secrets
from readyagents.importers.slug import slug

_KNOWN_VERSIONS = {"1", "n8n"}


def parse_n8n(text: str, *, filename: str = "export.json") -> IntermediateGraph:
    check_size(text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as extra:
        raise ImportRefused(f"n8n export is not JSON: {extra}", reason="malformed") from extra
    if not isinstance(data, dict):
        raise ImportRefused("n8n export must be a JSON object", reason="malformed")
    check_depth(data)
    version = data.get("schemaVersion") or data.get("version") or "n8n"
    token = str(version)
    if token not in _KNOWN_VERSIONS and not str(token).replace(".", "", 1).isdigit():
        raise ImportRefused(f"unknown n8n schema version {version!r}", reason="version")
    nodes_raw = data.get("nodes")
    if not isinstance(nodes_raw, list):
        raise ImportRefused("n8n export needs a nodes array", reason="malformed")
    check_nodes(len(nodes_raw))
    secrets = find_secrets(data)
    warnings: list[str] = []
    if secrets:
        warnings.append(
            "source export contains credential-like values; they were not written. "
            "The export file itself is a secret."
        )
        data = strip_secrets(data)
        nodes_raw = data.get("nodes") or []
    names: dict[str, str] = {}
    nodes: list[IntermediateNode] = []
    cred_names: list[str] = []
    for raw in nodes_raw:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("type") or "")
        if kind.endswith("stickyNote"):
            continue
        title = str(raw.get("name") or raw.get("id") or kind or "node")
        ident = slug(title, prefix="n8n")
        names[str(raw.get("name") or "")] = ident
        names[str(raw.get("id") or "")] = ident
        params = raw.get("parameters") if isinstance(raw.get("parameters"), dict) else {}
        for cred in _cred_names(raw):
            cred_names.append(cred)
        nodes.append(
            IntermediateNode(
                id=ident,
                kind=kind,
                title=title,
                params=dict(params),
                structural=_structural(kind),
            )
        )
    if not nodes:
        raise ImportRefused("n8n export has no importable nodes", reason="malformed")
    if len(nodes) > 400:
        raise ImportBoundNodes("n8n export exceeds node bound")
    raw_edges: list[IntermediateEdge] = []
    connections = data.get("connections") if isinstance(data.get("connections"), dict) else {}
    for src_name, spec in connections.items():
        src = names.get(str(src_name))
        if not src or not isinstance(spec, dict):
            continue
        main = spec.get("main") if isinstance(spec.get("main"), list) else []
        for index, bundle in enumerate(main):
            kind = "then" if index == 0 else "else" if index == 1 else "next"
            if not isinstance(bundle, list):
                continue
            for hop in bundle:
                if not isinstance(hop, dict):
                    continue
                dest_name = str(hop.get("node") or "")
                dest = names.get(dest_name)
                if dest:
                    raw_edges.append(
                        IntermediateEdge(src, dest, kind=kind if index < 2 else "next")
                    )
        error = spec.get("error") if isinstance(spec.get("error"), list) else []
        for bundle in error:
            if not isinstance(bundle, list):
                continue
            for hop in bundle:
                if isinstance(hop, dict) and names.get(str(hop.get("node") or "")):
                    raw_edges.append(
                        IntermediateEdge(src, names[str(hop.get("node"))], kind="error")
                    )
    start = nodes[0].id
    for item in nodes:
        if "trigger" in item.kind.lower() or item.kind.endswith("manualTrigger"):
            start = item.id
            break
    edges, skipped = drop_cycles(raw_edges)
    if skipped:
        nodes.append(
            IntermediateNode(
                id="cycle_broken",
                kind="n8n-nodes-base.splitInBatches",
                title="cycle",
                structural="loop",
            )
        )
    del filename
    return IntermediateGraph(
        source="n8n",
        name=_name(data),
        version=token,
        nodes=nodes,
        edges=edges,
        start=start,
        credential_names=sorted(set(cred_names)),
        warnings=warnings,
    )


def _name(data: dict[str, Any]) -> str:
    raw = str(data.get("name") or "n8n-import")
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-") or "n8n-import"


def _cred_names(raw: dict[str, Any]) -> list[str]:
    creds = raw.get("credentials")
    if not isinstance(creds, dict):
        return []
    names: list[str] = []
    for key, item in creds.items():
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
        else:
            names.append(str(key))
    return names


def _structural(kind: str) -> str | None:
    token = kind.lower()
    if token.endswith(".if") or token.endswith(".switch"):
        return "branch"
    if "splitinbatches" in token or "loop" in token:
        return "loop"
    if token.endswith(".merge"):
        return "parallel"
    if "executeworkflow" in token:
        return "subworkflow"
    if "error" in token:
        return "error"
    if "formtrigger" in token or "wait" in token:
        return "human"
    return None


def drop_cycles(
    edges: list[IntermediateEdge],
) -> tuple[list[IntermediateEdge], list[IntermediateEdge]]:
    kept: list[IntermediateEdge] = []
    skipped: list[IntermediateEdge] = []
    adj: dict[str, list[str]] = {}
    for edge in edges:
        if _reaches(adj, edge.target, edge.source):
            skipped.append(edge)
            continue
        adj.setdefault(edge.source, []).append(edge.target)
        kept.append(edge)
    return kept, skipped


def _reaches(adj: dict[str, list[str]], start: str, goal: str) -> bool:
    seen: set[str] = set()
    stack = [start]
    while stack:
        cur = stack.pop()
        if cur == goal:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(adj.get(cur) or [])
    return False
