#!/usr/bin/env python3
"""Keyless client for the ReadyAgents HTTP run API — not MCP JSON-RPC.

This talks to the ReadyAgents **/runs** extension (start, poll, decide, cancel).
It does **not** speak MCP Streamable HTTP (`/mcp`) and is not an MCP JSON-RPC
client. Official MCP Tasks, MRTR, and elicitation are not used here.

Start the foreground server in another process; this script does not start one:

    pip install -e ".[mcp]"
    readyagents mcp serve --transport streamable-http --host 127.0.0.1 --port 8765

Then (token is READYAGENTS_MCP_TOKEN, or printed once to stderr if generated):

    export READYAGENTS_MCP_TOKEN=...
    python examples/mcp_http_client.py
    python examples/mcp_http_client.py --path examples/calc_pipeline.yaml

This file uses stdlib ``urllib`` only. ``pip install httpx`` is optional for
other clients; this example does not import it.

HITL reuses ``examples/approval_gate.yaml``. After GET shows status ``paused``
and ``pending_node`` ``gate``:

    POST {base}/runs/{run_id}/decide
    {"node_id": "gate", "decision": "approve", "actor": "reviewer"}

There is no separate ``async_approval.yaml``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE = "http://127.0.0.1:8765"
DEFAULT_PATH = "examples/calc_pipeline.yaml"
DEFAULT_TOKEN_ENV = "READYAGENTS_MCP_TOKEN"
POLL_INTERVAL_S = 0.25
POLL_TIMEOUT_S = 60.0
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "POST /runs and poll GET until terminal "
            "(ReadyAgents extension, not MCP JSON-RPC)."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE,
        help=f"HTTP origin of the foreground server (default {DEFAULT_BASE}).",
    )
    parser.add_argument(
        "--path",
        default=DEFAULT_PATH,
        help=f"Workflow path relative to the server workspace (default {DEFAULT_PATH}).",
    )
    parser.add_argument(
        "--token-env",
        default=DEFAULT_TOKEN_ENV,
        help=f"Env var holding the bearer token (default {DEFAULT_TOKEN_ENV}).",
    )
    args = parser.parse_args(argv)

    token = os.environ.get(args.token_env, "").strip()
    if not token:
        print(
            f"Set {args.token_env} to the bearer token "
            "(printed once to stderr by `readyagents mcp serve --transport streamable-http` "
            "if generated). This script does not start the server.",
            file=sys.stderr,
        )
        return 1

    created, err = _request(
        "POST",
        _join(args.base_url, "/runs"),
        token,
        body={"path": args.path, "inputs": {}},
    )
    if err:
        return _fail(err)
    run_id = created.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return _fail({"error": "BadResponse", "message": "POST /runs returned no run_id"})
    print(f"run_id: {run_id}")
    print(f"status: {created.get('status', 'queued')}")

    # Approvals reuse examples/approval_gate.yaml (no async_approval.yaml):
    #   POST {base}/runs/{run_id}/decide
    #   {"node_id": "gate", "decision": "approve", "actor": "reviewer"}
    # Then continue polling GET until succeeded / failed / cancelled.

    deadline = time.monotonic() + POLL_TIMEOUT_S
    saw_pause = False
    record: dict[str, Any] = created
    while time.monotonic() < deadline:
        record, err = _request("GET", _join(args.base_url, "/runs", run_id), token)
        if err:
            return _fail(err)
        status = str(record.get("status") or "")
        if status == "paused" and not saw_pause:
            pending = record.get("pending") if isinstance(record.get("pending"), dict) else {}
            print(
                f"paused pending_node={record.get('pending_node')} "
                f"prompt={pending.get('prompt')}",
                file=sys.stderr,
            )
            print(
                "To decide (examples/approval_gate.yaml): POST /runs/{id}/decide "
                '{"node_id": "gate", "decision": "approve", "actor": "reviewer"}',
                file=sys.stderr,
            )
            saw_pause = True
        if status in TERMINAL:
            _print_result(record)
            return 0 if status == "succeeded" else 1
        time.sleep(POLL_INTERVAL_S)

    _print_result(record)
    print(
        f"timed out after {POLL_TIMEOUT_S:.0f}s (last status={record.get('status')!r})",
        file=sys.stderr,
    )
    return 1


def _print_result(record: dict[str, Any]) -> None:
    print(f"status: {record.get('status')}")
    outputs = record.get("outputs") if isinstance(record.get("outputs"), dict) else {}
    summary = outputs.get("summary")
    if summary is not None:
        print(f"summary: {summary}")
    elif outputs:
        print(f"outputs: {json.dumps(outputs, ensure_ascii=False)}")


def _join(base: str, *parts: str) -> str:
    url = base.rstrip("/")
    for part in parts:
        url = f"{url}/{part.lstrip('/')}"
    return url


def _request(
    method: str,
    url: str,
    token: str,
    *,
    body: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = _decode(resp.read())
    except urllib.error.HTTPError as exc:
        payload = _decode(exc.read())
        if not payload:
            payload = {
                "ok": False,
                "error": "HTTPError",
                "message": str(exc.reason or exc),
            }
        payload.setdefault("ok", False)
        payload.setdefault("error", type(exc).__name__)
        payload.setdefault("message", str(exc.reason or exc))
        return {}, payload
    except urllib.error.URLError as exc:
        return {}, {
            "ok": False,
            "error": "URLError",
            "message": (
                f"{exc.reason}. Start the server separately: "
                "readyagents mcp serve --transport streamable-http --host 127.0.0.1 --port 8765"
            ),
        }
    if payload.get("ok") is False:
        return {}, payload
    return payload, None


def _decode(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _fail(err: dict[str, Any]) -> int:
    name = err.get("error") or "Error"
    message = err.get("message") or json.dumps(err, ensure_ascii=False)
    extra = []
    if err.get("run_id"):
        extra.append(f"run_id={err['run_id']}")
    if err.get("request_id"):
        extra.append(f"request_id={err['request_id']}")
    suffix = f" ({', '.join(extra)})" if extra else ""
    print(f"{name}: {message}{suffix}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
