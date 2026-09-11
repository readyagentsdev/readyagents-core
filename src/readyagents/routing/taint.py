"""Fail-closed taint resolution for model routing. Indeterminate is untrusted."""

from __future__ import annotations

from typing import Literal

from readyagents.firewall.taint import (
    UNTRUSTED,
    from_mapping,
    template_roots,
)
from readyagents.workflow.schema import NodeSpec
from readyagents.workflow.state import RunState

RoutingTaint = Literal["trusted", "untrusted", "indeterminate"]

_SKIP_ROOTS = frozenset({"inputs", "outputs", "run_id", "true", "false", "none"})


def resolve_routing_taint(state: RunState | None, node: NodeSpec) -> RoutingTaint:
    """Classify prompt/system (and memory-derived) interpolation for residency rules.

    Missing provenance on an interpolated root is **indeterminate**, never trusted.
    Memory-derived values are untrusted. Callers treat indeterminate as untrusted.
    """
    blob = f"{getattr(node, 'prompt', None) or ''}\n{getattr(node, 'system', None) or ''}"
    roots = template_roots(blob)
    if not roots:
        return "trusted"
    if state is None:
        return "indeterminate"
    found_untrusted = False
    found_indeterminate = False
    known = set(state.inputs) | set(state.node_outputs) | set(state.output_keys)
    known.update(state.provenance or {})
    mapping = state.mapping()
    for root in roots:
        if root in _SKIP_ROOTS:
            continue
        raw = (state.provenance or {}).get(root)
        parsed = from_mapping(raw) if raw is not None else None
        if parsed is None:
            if root in state.inputs:
                continue
            if root in known or root in mapping:
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


def taint_blocks_hosted(taint: str) -> bool:
    return taint in {"untrusted", "indeterminate"}


def effective_untrusted(taint: str) -> bool:
    return taint_blocks_hosted(taint)
