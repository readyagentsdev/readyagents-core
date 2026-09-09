"""Fork a run from a node checkpoint by replaying retained NodeResult records."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from readyagents.errors import ForkError
from readyagents.workflow.schema import NodeSpec, WorkflowSpec
from readyagents.workflow.state import RunState, utc_now

_FOREACH_META = "_foreach"
_PARALLEL_META = "_parallel"
_INCLUDE_META = "_include"


def reconstruct_after(
    parent: RunState,
    node_id: str,
    *,
    occurrence: int | None = None,
    workflow: WorkflowSpec | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> RunState:
    """Return a new RunState as it existed after ``node_id`` completed."""
    if workflow is not None:
        _refuse_parallel_interior(workflow, node_id)
    matches = [i for i, row in enumerate(parent.results) if row.node_id == node_id]
    if not matches:
        raise ForkError(f"Cannot fork from node '{node_id}': it never ran in {parent.run_id}.")
    if len(matches) > 1 and occurrence is None:
        raise ForkError(
            f"Node '{node_id}' ran {len(matches)} times. Pass --occurrence N "
            f"(0-based) to choose one."
        )
    if occurrence is not None:
        if occurrence < 0 or occurrence >= len(matches):
            raise ForkError(
                f"Node '{node_id}' occurrence {occurrence} is out of range (0..{len(matches) - 1})."
            )
        cut = matches[occurrence]
    else:
        cut = matches[0]
    chosen = parent.results[cut]
    kept = list(parent.results[: cut + 1])
    child = RunState.start(
        parent.workflow_name,
        dict(parent.inputs),
        metadata=_lineage_metadata(parent, node_id, chosen),
        run_id=uuid4().hex,
    )
    if overrides:
        child.inputs.update(dict(overrides))
    for row in kept:
        if row.status != "ok":
            child.record_error(row.node_id, row.type, row.error or "error", attempts=row.attempts)
            continue
        output_key = _output_key_for(parent, row.node_id, row.output)
        child.record(
            row.node_id,
            row.output,
            node_type=row.type,
            output_key=output_key,
            attempts=row.attempts,
            started_at=row.started_at,
            finished_at=row.finished_at,
            usage=row.usage,
            tool_rounds=list(row.tool_rounds),
        )
    child.metadata.update(_truncated_buckets(parent.metadata, {r.node_id for r in kept}))
    child.status = "queued"
    child.pending_node = None
    child.pending = None
    child.finished_at = None
    child.started_at = utc_now()
    return child


def _output_key_for(parent: RunState, node_id: str, output: Any) -> str | None:
    for key, value in parent.output_keys.items():
        if key == node_id or value == output:
            return key
    return None


def _lineage_metadata(parent: RunState, node_id: str, chosen: Any) -> dict[str, Any]:
    meta = {
        key: value
        for key, value in dict(parent.metadata).items()
        if key not in {_FOREACH_META, _PARALLEL_META, _INCLUDE_META}
    }
    meta["forked_from"] = parent.run_id
    meta["forked_at_node"] = node_id
    meta["source"] = parent.metadata.get("source")
    meta["workspace"] = parent.metadata.get("workspace")
    meta["allow_http"] = parent.metadata.get("allow_http")
    return meta


def _truncated_buckets(metadata: Mapping[str, Any], kept_ids: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in (_FOREACH_META, _PARALLEL_META, _INCLUDE_META):
        bucket = metadata.get(key)
        if isinstance(bucket, dict):
            trimmed = {k: v for k, v in bucket.items() if k in kept_ids}
            if trimmed:
                out[key] = trimmed
    return out


def _refuse_parallel_interior(workflow: WorkflowSpec, node_id: str) -> None:
    for node in workflow.nodes:
        if str(node.type) != "parallel":
            continue
        for branch in node.branches or []:
            if _branch_id(branch) == node_id and node.id != node_id:
                raise ForkError(
                    f"Cannot fork from parallel-branch interior '{node_id}'. "
                    f"Fork from the parallel node '{node.id}' instead."
                )


def _branch_id(branch: NodeSpec | Mapping[str, Any]) -> str:
    if isinstance(branch, NodeSpec):
        return branch.id
    return str(branch.get("id") or "")


def fork_run(
    run_id: str,
    from_node: str,
    *,
    occurrence: int | None = None,
    overrides: Mapping[str, Any] | None = None,
    settings: Any = None,
    persist: bool = True,
    offline: bool = False,
    actor: str | None = None,
    authorizer: Any | None = None,
    extra_packs: Any = None,
    decisions: Mapping[str, str] | None = None,
) -> RunState:
    """Mint a new run from a parent checkpoint. Parent record is not rewritten."""
    from readyagents.config import get_settings
    from readyagents.run_store import open_run_store
    from readyagents.workflow.runner import run_workflow_file

    settings = settings or get_settings()
    store = open_run_store(settings)
    try:
        parent = store.get(run_id, allow_prefix=True).state
        parent_path = settings.runs_dir() / f"{parent.run_id}.json"
        parent_before = parent_path.read_bytes() if parent_path.is_file() else b""
        audit_dir = settings.audit_dir()
        audit_file = audit_dir / f"{parent.run_id}.jsonl"
        audit_before = audit_file.read_bytes() if audit_file.is_file() else b""
        workflow = load_workflow_from_state(parent)
        child = reconstruct_after(
            parent,
            from_node,
            occurrence=occurrence,
            workflow=workflow,
            overrides=overrides,
        )
        source = parent.metadata.get("source")
        if not source:
            raise ForkError(f"Run {parent.run_id} has no stored workflow path.")
        cassette_path = parent.metadata.get("cassette")
        state = run_workflow_file(
            source,
            settings=settings,
            persist=persist,
            extra_packs=extra_packs,
            decisions=decisions,
            actor=actor,
            authorizer=authorizer,
            initial_state=child,
            store=store,
            offline=offline,
            cassette_path=cassette_path if offline else None,
            record=False,
        )
        if parent_path.is_file() and parent_path.read_bytes() != parent_before:
            raise ForkError("Fork mutated the parent run record")
        if audit_file.is_file() and audit_file.read_bytes() != audit_before:
            raise ForkError("Fork mutated the parent audit file")
        return state
    finally:
        closer = getattr(store, "close", None)
        if callable(closer):
            closer()


def load_workflow_from_state(state: RunState) -> WorkflowSpec | None:
    source = state.metadata.get("source")
    if not source:
        return None
    path = Path(str(source))
    if not path.is_file():
        return None
    from readyagents.workflow.runner import load_workflow

    return load_workflow(path)
