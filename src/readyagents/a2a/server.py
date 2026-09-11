"""A2A ASGI over the existing run store. Loopback by default. Not started on import."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from readyagents.a2a.card import WELL_KNOWN_ALIAS, WELL_KNOWN_CARD, build_agent_card
from readyagents.a2a.mapping import (
    NOT_FOUND_MESSAGE,
    WIRE_CANCELED,
    WIRE_COMPLETED,
    WIRE_FAILED,
    WIRE_INPUT_REQUIRED,
    artifacts_from_state,
    map_run_to_task_state,
    task_status_object,
    validate_transition,
)
from readyagents.errors import (
    A2AError,
    A2ATransitionError,
    AuthorizationError,
    ConfigError,
    ReadyAgentsError,
    missing_extra_message,
)
from readyagents.mcp.protocol import PROMPT_MAX_CHARS, sanitize_prompt
from readyagents.mcp.tasks import canonical_decision_bytes
from readyagents.workflow.runner import load_workflow

_JSONRPC_INVALID_PARAMS = -32602
_JSONRPC_METHOD_NOT_FOUND = -32601
_JSONRPC_NOT_FOUND = -32001
_JSONRPC_STATE = -32023
_JSONRPC_UNAUTHORIZED = -32010


def compose_a2a_app(
    *,
    workflow_path: Path,
    coordinator: Any,
    token: str | None,
    bind_host: str,
    bind_port: int,
    canonical_url: str | None = None,
    max_body_bytes: int = 1_048_576,
    allow_public_bind: bool = False,
) -> Any:
    """Starlette app + MCP HTTP middleware (auth, rebinding, body limit)."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from readyagents.config import MAX_HTTP_BODY_BYTES
    from readyagents.mcp.http import (
        AuthMiddleware,
        BodyLimitMiddleware,
        CacheControlMiddleware,
        HostOriginMiddleware,
        assert_loopback_host,
    )

    if allow_public_bind:
        raw = str(bind_host).strip()
        if raw.lower() in {"0.0.0.0", "::", "[::]"}:
            raise A2AError(
                "A2A bind 0.0.0.0/:: is refused; pass a specific address with --allow-public-bind"
            )
        bind_host = raw
    else:
        bind_host = assert_loopback_host(bind_host)
    max_body_bytes = min(int(max_body_bytes), MAX_HTTP_BODY_BYTES)
    workflow = load_workflow(workflow_path)
    url = canonical_url or f"http://{bind_host}:{int(bind_port)}"
    card = build_agent_card(workflow, url=url)
    surface = A2ASurface(
        workflow_path=Path(workflow_path),
        coordinator=coordinator,
        card=card,
    )

    async def well_known(_request: Any) -> JSONResponse:
        return JSONResponse(card)

    async def rpc(request: Any) -> JSONResponse:
        try:
            client_host = request.client.host if request.client else None
            checker = getattr(coordinator, "check_rate", None)
            if callable(checker):
                checker(client_host)
        except ReadyAgentsError as extra:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32029, "message": str(extra)},
                },
                status_code=429,
            )
        raw = await request.body()
        if len(raw) > max_body_bytes:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "too large"}},
                status_code=413,
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            )
        rpc_id = payload.get("id") if isinstance(payload, dict) else None
        try:
            result = surface.dispatch(
                payload,
                signature=_header(request, "x-signature"),
                idempotency_key=_header(request, "idempotency-key"),
            )
        except AuthorizationError as extra:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": _JSONRPC_UNAUTHORIZED, "message": str(extra)},
                },
                status_code=200,
            )
        except A2ATransitionError as extra:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": _JSONRPC_STATE, "message": str(extra)},
                }
            )
        except ConfigError as extra:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": _JSONRPC_NOT_FOUND, "message": str(extra)},
                }
            )
        except ReadyAgentsError as extra:
            code = (
                _JSONRPC_METHOD_NOT_FOUND
                if "method not found" in str(extra).lower()
                else _JSONRPC_INVALID_PARAMS
            )
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": code, "message": str(extra)},
                }
            )
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})

    async def task_stream(request: Any) -> Any:
        from starlette.responses import Response

        from readyagents.workflow.stream import (
            get_stream_hub,
            snapshot_events,
            sse_snapshot_bytes,
            wants_event_stream,
        )

        task_id = str(request.path_params.get("task_id") or "")
        try:
            state = surface._load(task_id)
        except ReadyAgentsError as extra:
            return JSONResponse({"error": str(extra)}, status_code=404)
        if not wants_event_stream(request.headers.get("accept")):
            return JSONResponse(
                {"events": snapshot_events(state)},
                headers={"Cache-Control": "no-store"},
            )
        hub = get_stream_hub()
        queue = hub.try_subscribe(state.run_id)
        if queue is None:
            return JSONResponse({"error": "stream cap exceeded"}, status_code=429)
        try:
            extra = list(queue)
        finally:
            hub.unsubscribe(state.run_id, queue)
        return Response(
            sse_snapshot_bytes(state, extra),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store"},
        )

    app = Starlette(
        routes=[
            Route(WELL_KNOWN_CARD, well_known, methods=["GET"]),
            Route(WELL_KNOWN_ALIAS, well_known, methods=["GET"]),
            Route("/", rpc, methods=["POST"]),
            Route("/a2a", rpc, methods=["POST"]),
            Route("/tasks/{task_id}/stream", task_stream, methods=["GET"]),
        ]
    )
    return CacheControlMiddleware(
        AuthMiddleware(
            HostOriginMiddleware(
                BodyLimitMiddleware(app, max_body_bytes=max_body_bytes),
                bind_host=bind_host,
                bind_port=int(bind_port),
            ),
            token=token,
        )
    )


class A2ASurface:
    def __init__(self, *, workflow_path: Path, coordinator: Any, card: Mapping[str, Any]) -> None:
        self.workflow_path = Path(workflow_path)
        self.coordinator = coordinator
        self.card = dict(card)
        self._seen: dict[str, str] = {}

    def dispatch(
        self,
        payload: Any,
        *,
        signature: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ConfigError("JSON-RPC body must be an object")
        method = str(payload.get("method") or "")
        params = payload.get("params") if isinstance(payload.get("params"), Mapping) else {}
        if method == "message/send":
            return self.message_send(params, signature=signature, idempotency_key=idempotency_key)
        if method == "tasks/get":
            return self.tasks_get(params)
        if method == "tasks/cancel":
            return self.tasks_cancel(params)
        raise A2AError("method not found")

    def message_send(
        self,
        params: Mapping[str, Any],
        *,
        signature: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        task_id = params.get("taskId") or params.get("id")
        if isinstance(task_id, str) and task_id.strip():
            return self._continue_task(task_id.strip(), params, signature=signature)
        message = params.get("message") if isinstance(params.get("message"), Mapping) else {}
        text = _message_text(message)
        inputs = _inputs_from_text(text)
        actor = None
        meta = message.get("metadata") if isinstance(message.get("metadata"), Mapping) else {}
        if isinstance(meta.get("actor"), str):
            actor = meta["actor"]
        idem = None
        if isinstance(params.get("idempotencyKey"), str):
            idem = params["idempotencyKey"]
        elif isinstance(idempotency_key, str) and idempotency_key.strip():
            idem = idempotency_key.strip()
        handle = self.coordinator.start_run(
            {
                "path": str(self.workflow_path.name),
                "inputs": inputs,
                "actor": actor,
            },
            idempotency_key=idem,
        )
        run_id = str(handle["run_id"])
        self._seen[run_id] = map_run_to_task_state("queued")
        return self._task_payload(run_id)

    def tasks_get(self, params: Mapping[str, Any]) -> dict[str, Any]:
        run_id = _require_id(params.get("id") or params.get("taskId"))
        return self._task_payload(run_id)

    def tasks_cancel(self, params: Mapping[str, Any]) -> dict[str, Any]:
        run_id = _require_id(params.get("id") or params.get("taskId"))
        state = self._load(run_id)
        current = map_run_to_task_state(state.status)
        validate_transition(current, WIRE_CANCELED)
        self.coordinator.cancel(run_id, {"reason": "a2a cancel"})
        return self._task_payload(run_id)

    def _continue_task(
        self, run_id: str, params: Mapping[str, Any], *, signature: str | None
    ) -> dict[str, Any]:
        ident = _require_id(run_id)
        state = self._load(ident)
        wire = map_run_to_task_state(state.status)
        if wire != WIRE_INPUT_REQUIRED:
            raise A2ATransitionError(f"task is not input-required (state={wire})")
        message = params.get("message") if isinstance(params.get("message"), Mapping) else {}
        text = _message_text(message)
        decision, actor = _decision_from_text(text)
        node_id = str(state.pending_node or "")
        if not node_id:
            raise A2AError("paused run has no pending node")
        secret = getattr(getattr(self.coordinator, "settings", None), "decision_secret", None)
        meta = message.get("metadata") if isinstance(message.get("metadata"), Mapping) else {}
        if isinstance(meta.get("actor"), str):
            actor = meta["actor"]
        input_key = f"a2a.{ident}.{node_id}"
        if secret:
            from readyagents.decisions.signing import verify_signed_body

            body = canonical_decision_bytes(
                run_id=ident,
                node_id=node_id,
                decision=decision,
                actor=actor,
                input_request_key=input_key,
            )
            try:
                verify_signed_body(str(secret), body, signature)
            except ValueError as extra:
                self._audit(
                    "decision_refused",
                    run_id=ident,
                    node_id=node_id,
                    decision=decision,
                    actor=actor,
                    reason=str(extra),
                )
                raise AuthorizationError(actor, decision, node_id) from extra
        try:
            self.coordinator.decide(
                ident,
                {"node_id": node_id, "decision": decision, "actor": actor},
                input_request_key=input_key,
            )
        except AuthorizationError as extra:
            self._audit(
                "decision_refused",
                run_id=ident,
                node_id=node_id,
                decision=decision,
                actor=actor,
                reason=str(extra),
            )
            raise
        return self._task_payload(ident)

    def _task_payload(self, run_id: str) -> dict[str, Any]:
        state = self._load(run_id)
        wire = map_run_to_task_state(state.status)
        previous = self._seen.get(run_id)
        if previous:
            try:
                validate_transition(previous, wire)
            except A2ATransitionError:
                wire = previous
        self._seen[run_id] = wire
        prompt = None
        if wire == WIRE_INPUT_REQUIRED:
            pending = state.pending if isinstance(state.pending, Mapping) else {}
            raw = str(pending.get("prompt") or f"Approval required at {state.pending_node}")
            prompt = {
                "role": "agent",
                "parts": [{"kind": "text", "text": sanitize_prompt(raw, limit=PROMPT_MAX_CHARS)}],
                "metadata": {
                    "node_id": state.pending_node,
                    "attribution": "local approval gate",
                },
            }
        status = task_status_object(state, message=prompt)
        status["state"] = wire
        body: dict[str, Any] = {
            "id": state.run_id,
            "contextId": state.run_id,
            "kind": "task",
            "status": status,
        }
        if wire == WIRE_COMPLETED:
            body["artifacts"] = artifacts_from_state(state)
        if wire == WIRE_FAILED:
            errors = list(state.errors or [])
            body["status"]["message"] = {
                "role": "agent",
                "parts": [
                    {
                        "kind": "text",
                        "text": sanitize_prompt("; ".join(errors) or "failed", limit=500),
                    }
                ],
            }
        return body

    def _load(self, run_id: str):
        try:
            return self.coordinator._load_exact(run_id)
        except Exception as extra:  # noqa: BLE001
            raise ConfigError(NOT_FOUND_MESSAGE) from extra

    def _audit(self, event: str, **fields: Any) -> None:
        settings = getattr(self.coordinator, "settings", None)
        if settings is None:
            return
        from readyagents.audit import append_audit_event

        append_audit_event(settings.audit_dir(), {"event": event, **fields})


def _header(request: Any, name: str) -> str | None:
    try:
        return request.headers.get(name)
    except Exception:  # noqa: BLE001
        return None


def _require_id(raw: Any) -> str:
    import re

    if not isinstance(raw, str) or not re.fullmatch(r"[0-9a-f]{32}", raw):
        raise ConfigError(NOT_FOUND_MESSAGE)
    return raw


def _message_text(message: Mapping[str, Any]) -> str:
    parts = message.get("parts")
    chunks: list[str] = []
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, Mapping) and part.get("kind") == "text":
                chunks.append(str(part.get("text") or ""))
    text = "\n".join(chunks) or str(message.get("text") or "")
    return sanitize_prompt(text, limit=8000)


def _inputs_from_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return {"message": stripped}


def _decision_from_text(text: str) -> tuple[str, str | None]:
    stripped = text.strip().lower()
    actor = None
    if stripped.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                decision = str(data.get("decision") or "").strip().lower()
                if isinstance(data.get("actor"), str):
                    actor = data["actor"]
                if decision in {"approve", "reject"}:
                    return decision, actor
        except json.JSONDecodeError:
            pass
    if stripped in {"approve", "approved", "yes"}:
        return "approve", actor
    if stripped in {"reject", "denied", "no"}:
        return "reject", actor
    raise ConfigError('decision must be exactly "approve" or "reject"')


def serve_a2a(
    workflow_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8770,
    token: str | None = None,
    allow_public_bind: bool = False,
    token_env: str = "READYAGENTS_A2A_TOKEN",
) -> None:
    """Foreground A2A server. Blocking. Stops when the process stops."""
    import sys

    from readyagents.config import get_settings
    from readyagents.mcp.http import assert_loopback_host, resolve_bearer_token
    from readyagents.mcp.run_api import RunCoordinator

    wf = Path(workflow_path).expanduser().resolve()
    if not wf.is_file():
        raise A2AError(f"Workflow file not found: {wf}")
    if allow_public_bind:
        print(
            "Warning: A2A bind is not loopback. You own the exposure. "
            "Do not reverse-proxy this door onto the public internet.",
            file=sys.stderr,
        )
        bind = host
        env_token = (os.environ.get(token_env) or "").strip()
        token = token or env_token or None
        if not token:
            raise A2AError(f"public A2A bind requires {token_env}")
    else:
        bind = assert_loopback_host(host)
        if token is None:
            token = resolve_bearer_token(auth_mode="token", token_env=token_env, bind_host=bind)
            existing = (os.environ.get(token_env) or "").strip()
            if not existing:
                if not token:
                    token = uuid.uuid4().hex
                print("Generated A2A bearer token (shown once):", file=sys.stderr)
                print(token, file=sys.stderr)
    settings = get_settings()
    coordinator = RunCoordinator(settings=settings, workspace=wf.parent)
    coordinator.started_by_kind = "a2a"
    app = compose_a2a_app(
        workflow_path=wf,
        coordinator=coordinator,
        token=token,
        bind_host=bind,
        bind_port=int(port),
        canonical_url=f"http://{bind}:{int(port)}",
        allow_public_bind=allow_public_bind,
    )
    try:
        import uvicorn
    except ImportError as extra:
        raise A2AError(
            "A2A serve needs starlette and uvicorn. "
            f"{missing_extra_message('MCP', 'mcp')} "
            "Also: pip install starlette uvicorn"
        ) from extra
    try:
        uvicorn.run(app, host=bind, port=int(port), log_level="warning")
    finally:
        shutdown = getattr(coordinator, "shutdown", None)
        if callable(shutdown):
            shutdown()
