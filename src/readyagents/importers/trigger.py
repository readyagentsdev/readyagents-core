"""Generic trigger-action JSON (Zapier-shaped) → IR."""

from __future__ import annotations

import json

from readyagents.errors import ImportRefused
from readyagents.importers.bounds import check_depth, check_nodes, check_size
from readyagents.importers.ir import IntermediateEdge, IntermediateGraph, IntermediateNode
from readyagents.importers.n8n import drop_cycles
from readyagents.importers.secrets import find_secrets, strip_secrets
from readyagents.importers.slug import slug

_STRUCT = {
    "filter": "branch",
    "paths": "branch",
    "loop": "loop",
    "fork": "parallel",
    "subzap": "subworkflow",
    "error": "error",
    "delay_until_approved": "human",
}


def parse_trigger(text: str, *, filename: str = "zap.json") -> IntermediateGraph:
    del filename
    check_size(text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as extra:
        raise ImportRefused(
            f"trigger-action export is not JSON: {extra}", reason="malformed"
        ) from extra
    if not isinstance(data, dict):
        raise ImportRefused("trigger-action export must be a JSON object", reason="malformed")
    check_depth(data)
    version = str(data.get("version") or "1")
    if version != "1":
        raise ImportRefused(f"unknown trigger-action schema version {version!r}", reason="version")
    warnings: list[str] = []
    if find_secrets(data):
        warnings.append(
            "source export contains credential-like values; they were not written. "
            "The export file itself is a secret."
        )
        data = strip_secrets(data)
    nodes: list[IntermediateNode] = []
    edges: list[IntermediateEdge] = []
    trigger = data.get("trigger") if isinstance(data.get("trigger"), dict) else {}
    trig_kind = str(trigger.get("type") or trigger.get("app") or "webhook")
    trig_id = slug(str(trigger.get("id") or "trigger"), prefix="tr")
    nodes.append(
        IntermediateNode(
            id=trig_id,
            kind=trig_kind,
            title=str(trigger.get("event") or trig_kind),
            params=dict(trigger.get("params") or {}),
        )
    )
    prev = trig_id
    steps = data.get("steps") if isinstance(data.get("steps"), list) else []
    check_nodes(len(steps) + 1)
    for raw in steps:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("type") or raw.get("app") or "action")
        ident = slug(str(raw.get("id") or kind), prefix="st")
        nodes.append(
            IntermediateNode(
                id=ident,
                kind=kind,
                title=str(raw.get("event") or raw.get("id") or kind),
                params=dict(raw.get("params") or {}),
                structural=_STRUCT.get(kind),
            )
        )
        edges.append(IntermediateEdge(prev, ident, kind="then" if kind == "filter" else "next"))
        prev = ident
    if not nodes:
        raise ImportRefused("trigger-action export has no trigger or steps", reason="malformed")
    return IntermediateGraph(
        source="trigger",
        name=str(data.get("name") or "trigger-import"),
        version=version,
        nodes=nodes,
        edges=drop_cycles(edges)[0],
        start=trig_id,
        warnings=warnings,
    )
