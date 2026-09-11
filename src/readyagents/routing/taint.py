"""Fail-closed taint resolution for model routing. Indeterminate is untrusted."""

from __future__ import annotations

from typing import Literal

from readyagents.firewall.taint import (
    UNTRUSTED,
    from_mapping,
    template_paths,
)
from readyagents.workflow.schema import NodeSpec
from readyagents.workflow.state import RunState

RoutingTaint = Literal["trusted", "untrusted", "indeterminate"]

_SKIP_ROOTS = frozenset({"run_id", "true", "false", "none"})
_NAMESPACE_ROOTS = frozenset({"outputs", "inputs"})


def resolve_routing_taint(state: RunState | None, node: NodeSpec) -> RoutingTaint:
    """Classify prompt/system (and memory-derived) interpolation for residency rules.

    Missing provenance on an interpolated key is **indeterminate**, never trusted.
    ``{{outputs.<id>}}`` / ``{{inputs.<key>}}`` resolve the nested key, not the
    namespace root. Memory-derived values are untrusted. Callers treat
    indeterminate as untrusted.
    """
    blob = f"{getattr(node, 'prompt', None) or ''}\n{getattr(node, 'system', None) or ''}"
    paths = template_paths(blob)
    if not paths:
        return "trusted"
    if state is None:
        return "indeterminate"
    found_untrusted = False
    found_indeterminate = False
    known = set(state.inputs) | set(state.node_outputs) | set(state.output_keys)
    known.update(state.provenance or {})
    mapping = state.mapping()
    for path in paths:
        keys = _provenance_keys(path)
        if keys is None:
            continue
        if not keys:
            found_indeterminate = True
            continue
        for key in keys:
            raw = (state.provenance or {}).get(key)
            parsed = from_mapping(raw) if raw is not None else None
            if parsed is None:
                if key in state.inputs:
                    continue
                if key in known or key in mapping:
                    found_indeterminate = True
                    continue
                found_indeterminate = True
                continue
            if parsed.trust == UNTRUSTED:
                found_untrusted = True
            if (parsed.source or "").startswith("memory"):
                found_untrusted = True
    if found_untrusted:
        return "untrusted"
    if found_indeterminate:
        return "indeterminate"
    return "trusted"


def _provenance_keys(path: str) -> list[str] | None:
    """Return provenance keys for a dotted interpolation, or None to skip.

    An empty list means the path cannot be resolved (fail closed).
    """
    parts = [part for part in (path or "").split(".") if part]
    if not parts:
        return None
    root = parts[0]
    if root in _SKIP_ROOTS:
        return None
    if root in _NAMESPACE_ROOTS:
        if len(parts) < 2:
            return []
        return [parts[1]]
    return [root]


def taint_blocks_hosted(taint: str) -> bool:
    return taint in {"untrusted", "indeterminate"}


def effective_untrusted(taint: str) -> bool:
    return taint_blocks_hosted(taint)
