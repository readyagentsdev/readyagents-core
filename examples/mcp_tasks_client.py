#!/usr/bin/env python3
"""Keyless mocked-transport demo of MCP tasks + MRTR approval.

This does **not** open a network socket. It drives ``TaskService`` against an
in-process ``RunCoordinator`` the same way ``tasks/get`` / ``tasks/update``
do on the loopback server. Official MCP Tasks; the deprecated ``/runs``
HTTP door is not used.

    python examples/mcp_tasks_client.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

from readyagents.config import Settings
from readyagents.mcp.run_api import RunCoordinator
from readyagents.mcp.tasks import TaskService


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    workflow = root / "examples" / "approval_gate.yaml"
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            home=Path(tmp) / ".readyagents",
            workspace=root,
            allow_http=False,
            _env_file=(),  # type: ignore[call-arg]
        )
        coord = RunCoordinator(settings=settings, workspace=root)
        service = TaskService(coord)
        try:
            created = service.create_from_workflow(path=str(workflow.relative_to(root)))
            task_id = created["taskId"]
            print(json.dumps({"step": "create", "resultType": created["resultType"], "taskId": task_id}))
            deadline = time.monotonic() + 8
            paused = None
            while time.monotonic() < deadline:
                paused = service.get(task_id)
                if paused.get("status") == "input_required":
                    break
                time.sleep(0.05)
            assert paused is not None
            print(json.dumps({"step": "poll", "status": paused["status"]}))
            key = next(iter(paused.get("inputRequests") or {}))
            print(json.dumps({"step": "input_required", "key": key}))
            ack = service.update(
                task_id,
                input_responses={key: {"action": "accept", "content": {"decision": "approve"}}},
                actor="reviewer",
            )
            print(json.dumps({"step": "update", "resultType": ack.get("resultType")}))
            deadline = time.monotonic() + 8
            final = paused
            while time.monotonic() < deadline:
                final = service.get(task_id)
                if final.get("status") in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.05)
            print(json.dumps({"step": "final", "status": final.get("status")}))
            return 0 if final.get("status") == "completed" else 1
        finally:
            coord.shutdown(timeout=2.0)


if __name__ == "__main__":
    sys.exit(main())
