"""Opt-in async wrappers over the synchronous engine.

Frozen pattern
--------------
The synchronous engine (``run_workflow`` / ``run_workflow_file``) is the
implementation. The async path is ``asyncio.to_thread(...)`` over that engine.
There is no second engine.

Why not invert this (async core, sync as ``asyncio.run``)? Nested
``asyncio.run`` deadlocks when a loop is already running. ``to_thread`` from a
running loop is safe; calling ``run_workflow`` from a worker thread is the same
code path as today.

Do not call ``asyncio.run`` from inside ``run_workflow``. Nested wrapping is:
await ``run_workflow_async`` (which uses ``to_thread``) from a running loop, or
``asyncio.run(run_workflow_async(...))`` from a thread that has no loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from readyagents.workflow.engine import run_workflow
from readyagents.workflow.runner import run_workflow_file
from readyagents.workflow.schema import WorkflowSpec
from readyagents.workflow.state import RunState


async def run_workflow_async(
    workflow: WorkflowSpec,
    inputs: Mapping[str, Any],
    ctx: Any,
    *,
    metadata: Mapping[str, Any] | None = None,
    state: RunState | None = None,
    run_id: str | None = None,
) -> RunState:
    """Opt-in async entry. Runs the shipped sync engine in a worker thread."""
    return await asyncio.to_thread(
        run_workflow,
        workflow,
        inputs,
        ctx,
        metadata=metadata,
        state=state,
        run_id=run_id,
    )


async def run_workflow_file_async(
    path: Path | str,
    *,
    inputs: Mapping[str, Any] | None = None,
    extra_packs: Sequence[Any] | None = None,
    **kwargs: Any,
) -> RunState:
    """Opt-in async file runner. Same kwargs as ``run_workflow_file``."""
    return await asyncio.to_thread(
        run_workflow_file,
        path,
        inputs=inputs,
        extra_packs=extra_packs,
        **kwargs,
    )
