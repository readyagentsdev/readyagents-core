"""Additive provenance for values entering run state."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from readyagents.workflow.state import RunState
from readyagents.workflow.templates import _VAR

TRUSTED = "trusted"
UNTRUSTED = "untrusted"


@dataclass(frozen=True)
class Provenance:
    trust: str
    source: str
    node_id: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return {k: v for k, v in data.items() if v is not None}


def trusted(*, source: str, node_id: str | None = None, detail: str | None = None) -> Provenance:
    return Provenance(trust=TRUSTED, source=source, node_id=node_id, detail=detail)


def untrusted(*, source: str, node_id: str | None = None, detail: str | None = None) -> Provenance:
    return Provenance(trust=UNTRUSTED, source=source, node_id=node_id, detail=detail)


def from_mapping(raw: Any) -> Provenance | None:
    if not isinstance(raw, dict):
        return None
    trust = str(raw.get("trust") or TRUSTED)
    source = str(raw.get("source") or "literal")
    node_id = raw.get("node_id")
    detail = raw.get("detail")
    return Provenance(
        trust=UNTRUSTED if trust == UNTRUSTED else TRUSTED,
        source=source,
        node_id=str(node_id) if node_id else None,
        detail=str(detail) if detail else None,
    )


def merge(parts: list[Provenance], *, node_id: str | None = None) -> Provenance:
    if not parts:
        return trusted(source="literal", node_id=node_id)
    if any(p.trust == UNTRUSTED for p in parts):
        src = next(p.source for p in parts if p.trust == UNTRUSTED)
        return untrusted(source=src, node_id=node_id)
    return trusted(source=parts[0].source, node_id=node_id)


_ROOT = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*)")


def template_roots(template: str) -> list[str]:
    names: list[str] = []
    for match in _VAR.finditer(template or ""):
        path = match.group(1) or ""
        root = path.split(".", 1)[0]
        if root and root not in names:
            names.append(root)
    return names


def template_paths(template: str) -> list[str]:
    """Full dotted interpolation paths (``outputs.seed``, not just ``outputs``)."""
    paths: list[str] = []
    for match in _VAR.finditer(template or ""):
        path = (match.group(1) or "").strip()
        if path and path not in paths:
            paths.append(path)
    return paths


def walk_strings(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(walk_strings(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(walk_strings(item))
    return found


def provenance_of(state: RunState, key: str) -> Provenance:
    raw = (state.provenance or {}).get(key)
    parsed = from_mapping(raw)
    if parsed is not None:
        return parsed
    if key in state.inputs:
        return trusted(source="input")
    return trusted(source="literal")


def arguments_tainted(state: RunState, arguments: Any) -> bool:
    for text in walk_strings(arguments):
        for root in template_roots(text):
            if provenance_of(state, root).trust == UNTRUSTED:
                return True
            if root in state.output_keys:
                if provenance_of(state, root).trust == UNTRUSTED:
                    return True
    return False


def prompt_tainted(state: RunState, prompt: str, system: str | None = None) -> bool:
    """True when an agent prompt/system interpolates untrusted state."""
    blob = f"{prompt or ''}\n{system or ''}"
    return provenance_for_template(state, blob, node_id=None).trust == UNTRUSTED


def seed_foreach_item_provenance(
    parent: RunState,
    child: RunState,
    *,
    items_expr: str,
    node_id: str,
) -> None:
    """Copy parent taint onto a foreach child; untrust item/index from untrusted items."""
    child.provenance = dict(parent.provenance)
    roots = template_roots(items_expr)
    if not roots:
        root = (items_expr or "").split(".", 1)[0].strip()
        if root:
            roots = [root]
    source_untrusted = any(provenance_of(parent, root).trust == UNTRUSTED for root in roots)
    if source_untrusted:
        item_prov = untrusted(source="foreach", node_id=node_id, detail="item")
        index_prov = untrusted(source="foreach", node_id=node_id, detail="index")
    else:
        item_prov = trusted(source="foreach", node_id=node_id, detail="item")
        index_prov = trusted(source="foreach", node_id=node_id, detail="index")
    set_provenance(child, "item", item_prov)
    set_provenance(child, "index", index_prov)


def provenance_for_template(state: RunState, template: str, *, node_id: str | None) -> Provenance:
    parts = [provenance_of(state, root) for root in template_roots(template)]
    return merge(parts, node_id=node_id)


def set_provenance(state: RunState, key: str, prov: Provenance) -> None:
    if not key:
        return
    state.provenance[key] = prov.as_dict()


def seed_input_provenance(state: RunState) -> None:
    for key in state.inputs:
        state.provenance.setdefault(key, trusted(source="input").as_dict())


def note_output(state: RunState, node_id: str, output_key: str | None, prov: Provenance) -> None:
    set_provenance(state, node_id, prov)
    if output_key:
        set_provenance(state, output_key, prov)


def note_node_output(state: RunState, node: Any, output: Any) -> None:
    """Record provenance for a finished node. Additive; never strips taint."""
    kind = str(getattr(node, "type", "") or "")
    node_id = str(getattr(node, "id", "") or "")
    output_key = getattr(node, "output_key", None)
    if kind == "tool":
        tool = str(getattr(node, "tool", "") or "tool")
        source = f"mcp:{tool.split('.', 1)[0]}" if "." in tool else f"tool:{tool}"
        if tool == "http_get":
            source = "http"
        elif tool in {"read_file", "list_dir", "write_file"}:
            source = "file"
        else:
            try:
                from readyagents.connectors.registry import spec_for

                if spec_for(tool) is not None:
                    source = f"connector:{tool}"
            except Exception:  # noqa: BLE001
                pass
        prov = untrusted(source=source, node_id=node_id)
    elif kind == "agent":
        prompt = str(getattr(node, "prompt", "") or "")
        system = str(getattr(node, "system", "") or "")
        prov = provenance_for_template(state, prompt + " " + system, node_id=node_id)
        if getattr(node, "media", None):
            prov = untrusted(source="media", node_id=node_id)
        elif prov.trust == UNTRUSTED:
            prov = untrusted(source="model", node_id=node_id)
        else:
            prov = trusted(source="model", node_id=node_id)
    elif kind == "transform":
        template = getattr(node, "template", None)
        if isinstance(template, str):
            prov = provenance_for_template(state, template, node_id=node_id)
        else:
            source = getattr(node, "source", None)
            if isinstance(source, str):
                prov = provenance_of(state, source.split(".", 1)[0])
            else:
                prev = state.results[-2].node_id if len(state.results) >= 2 else ""
                prov = (
                    provenance_of(state, prev)
                    if prev
                    else trusted(source="literal", node_id=node_id)
                )
            if prov.node_id != node_id:
                prov = Provenance(
                    trust=prov.trust, source=prov.source, node_id=node_id, detail=prov.detail
                )
    elif kind == "condition":
        when = str(getattr(node, "when", "") or "")
        skip = {"and", "or", "not", "true", "false", "in", "eq", "ne"}
        roots = [tok for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", when) if tok not in skip]
        prov = merge([provenance_of(state, root) for root in roots], node_id=node_id)
    elif kind == "approval":
        prov = trusted(source="input", node_id=node_id)
    elif kind == "foreach":
        body = getattr(node, "body", None)
        body_type = str(getattr(body, "type", "") or "")
        if body_type in {"tool", "agent", "a2a", "memory"}:
            tool = str(getattr(body, "tool", "") or body_type)
            prov = untrusted(source=f"tool:{tool}", node_id=node_id)
        else:
            items = str(getattr(node, "items", "") or "")
            root = items.split(".", 1)[0]
            from_items = provenance_of(state, root) if root else trusted(source="literal")
            template = str(getattr(body, "template", "") or "")
            from_body = provenance_for_template(state, template, node_id=node_id)
            prov = merge([from_items, from_body], node_id=node_id)
    elif kind == "parallel":
        branches = list(getattr(node, "branches", None) or [])
        if any(
            str(getattr(branch, "type", "")) in {"tool", "agent", "a2a", "memory"}
            for branch in branches
        ):
            prov = untrusted(source="tool:parallel", node_id=node_id)
        else:
            prov = trusted(source="literal", node_id=node_id)
    elif kind == "a2a":
        prov = untrusted(source="a2a", node_id=node_id)
    elif kind == "memory":
        prov = untrusted(source="memory", node_id=node_id)
    elif kind == "ingest":
        prov = untrusted(source="knowledge", node_id=node_id)
    elif kind in {"table", "classify"}:
        prov = untrusted(source="table", node_id=node_id)
    elif kind == "wait":
        prov = untrusted(source="event", node_id=node_id)
    elif kind == "skill":
        prov = untrusted(source="skill", node_id=node_id)
    elif kind == "browser":
        url = "browser"
        if isinstance(output, dict):
            url = str(output.get("url") or output.get("source") or "browser")
        prov = untrusted(source=url, node_id=node_id)
    elif kind in {"document", "transcribe"}:
        prov = untrusted(source="media", node_id=node_id)
    elif kind == "include":
        flagged = (state.metadata.get("_child_untrusted") or {}).get(node_id)
        if flagged:
            prov = untrusted(source="include", node_id=node_id)
        else:
            prov = trusted(source="literal", node_id=node_id)
    else:
        prov = trusted(source="literal", node_id=node_id)
        for raw in (state.provenance or {}).values():
            parsed = from_mapping(raw)
            if parsed and parsed.trust == UNTRUSTED:
                prov = untrusted(source=parsed.source, node_id=node_id)
                break
    note_output(state, node_id, output_key, prov)
    _ = output
