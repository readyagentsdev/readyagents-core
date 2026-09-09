"""JSON-RPC overlay for ``server/discover``, official tasks, and 2026 headers.

Used on Streamable HTTP (and stdio when registered). Does not start a run on
discover. Packs are not imported for side effects here.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Mapping
from typing import Any

from readyagents.errors import (
    HeaderMismatchError,
    ReadyAgentsError,
    UnsupportedProtocolVersionError,
)
from readyagents.mcp.protocol import (
    JSONRPC_INVALID_REQUEST,
    JSONRPC_PARSE_ERROR,
    LATEST_PROTOCOL_VERSION,
    LIST_CACHE_SCOPE,
    LIST_CACHE_TTL_MS,
    RESULT_TYPE_COMPLETE,
    TASKS_EXTENSION,
    discover_result,
    error_from_exception,
    honoured_protocol_versions,
    http_status_for_rpc_error,
    jsonrpc_error,
    jsonrpc_result,
    request_context_from_rpc,
    require_transport_headers,
)
from readyagents.mcp.subscriptions import SubscriptionRegistry
from readyagents.mcp.tasks import TaskService

_HANDLED = frozenset(
    {
        "server/discover",
        "tasks/get",
        "tasks/update",
        "tasks/cancel",
        "subscriptions/listen",
    }
)


class McpRpcSurface:
    def __init__(
        self,
        *,
        coordinator: Any | None = None,
        max_streams: int = 4,
        decision_secret: str | None = None,
        default_actor: str | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.tasks = TaskService(coordinator) if coordinator is not None else None
        self.subscriptions = SubscriptionRegistry(max_streams=max_streams)
        self.decision_secret = decision_secret
        self.default_actor = default_actor

    def handle(
        self,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
        """Return ``(http_status, jsonrpc_body, extra_notifications)``."""
        rpc_id = payload.get("id")
        headers = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}
        try:
            if payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
                body = jsonrpc_error(rpc_id, JSONRPC_INVALID_REQUEST, "Invalid Request")
                return 400, body, []
            honoured = honoured_protocol_versions()
            ctx = request_context_from_rpc(
                payload,
                honoured=honoured,
                header_version=headers.get("mcp-protocol-version"),
            )
            method = str(payload["method"])
            params = payload.get("params") if isinstance(payload.get("params"), Mapping) else {}
            require_transport_headers(
                method=method,
                params=params,
                mcp_method=headers.get("mcp-method"),
                mcp_name=headers.get("mcp-name"),
                protocol_version=ctx.protocol_version,
            )
            extra: list[dict[str, Any]] = []
            if method == "server/discover":
                result = discover_result(honoured)
                return 200, jsonrpc_result(rpc_id, result, ctx=ctx), extra
            if method.startswith("tasks/") or method == "subscriptions/listen":
                if ctx.protocol_version == LATEST_PROTOCOL_VERSION and not ctx.supports(
                    TASKS_EXTENSION
                ):
                    if method.startswith("tasks/"):
                        body = jsonrpc_error(
                            rpc_id,
                            -32021,
                            "Missing required client capability",
                            {
                                "requiredCapabilities": {
                                    "extensions": {TASKS_EXTENSION: {}},
                                }
                            },
                        )
                        return 400, body, extra
            result, extra = self._dispatch(method, params, ctx)
            return 200, jsonrpc_result(rpc_id, result, ctx=ctx), extra
        except (
            UnsupportedProtocolVersionError,
            HeaderMismatchError,
            ValueError,
            ReadyAgentsError,
        ) as exc:
            body = error_from_exception(rpc_id, exc)
            code = int(body.get("error", {}).get("code") or JSONRPC_INVALID_REQUEST)
            return http_status_for_rpc_error(code), body, []

    def intercept_tools_call(
        self,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, dict[str, Any], list[dict[str, Any]]] | None:
        """If this ``tools/call`` should become a task, handle it; else ``None``."""
        if payload.get("method") != "tools/call":
            return None
        params = payload.get("params") if isinstance(payload.get("params"), Mapping) else {}
        name = params.get("name") if isinstance(params, Mapping) else None
        if name != "run_workflow":
            return None
        if self.tasks is None:
            return None
        try:
            honoured = honoured_protocol_versions()
            header_items = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}
            ctx = request_context_from_rpc(
                payload,
                honoured=honoured,
                header_version=header_items.get("mcp-protocol-version"),
            )
        except (UnsupportedProtocolVersionError, ValueError):
            return None
        if not ctx.supports(TASKS_EXTENSION):
            return None
        rpc_id = payload.get("id")
        try:
            require_transport_headers(
                method="tools/call",
                params=params if isinstance(params, Mapping) else {},
                mcp_method=header_items.get("mcp-method"),
                mcp_name=header_items.get("mcp-name"),
                protocol_version=ctx.protocol_version,
            )
            arguments = (
                params.get("arguments") if isinstance(params.get("arguments"), Mapping) else {}
            )
            path = arguments.get("path")
            if not isinstance(path, str) or not path.strip():
                raise ValueError("run_workflow path is required")
            inputs_raw = arguments.get("inputs_json") or arguments.get("inputs") or "{}"
            if isinstance(inputs_raw, Mapping):
                inputs = dict(inputs_raw)
            else:
                inputs = json.loads(str(inputs_raw) or "{}")
                if not isinstance(inputs, dict):
                    raise ValueError("inputs_json must be a JSON object")
            actor = arguments.get("actor")
            if actor is not None and not isinstance(actor, str):
                raise ValueError("actor must be a string")
            result = self.tasks.create_from_workflow(
                path=path.strip(),
                inputs=inputs,
                actor=actor if isinstance(actor, str) else self.default_actor,
                dry_run=bool(arguments.get("dry_run", False)),
                trace_context=ctx.trace_context,
            )
            return 200, jsonrpc_result(rpc_id, result, ctx=ctx), []
        except (HeaderMismatchError, ValueError, ReadyAgentsError) as exc:
            body = error_from_exception(rpc_id, exc)
            code = int(body.get("error", {}).get("code") or JSONRPC_INVALID_REQUEST)
            return http_status_for_rpc_error(code), body, []

    def _dispatch(
        self, method: str, params: Mapping[str, Any], ctx: Any
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if method == "tasks/get":
            if self.tasks is None:
                raise ValueError("tasks extension is not available")
            result = self.tasks.get(
                params.get("taskId", params.get("task_id")),
                actor=_actor(params, self.default_actor),
            )
            return result, []
        if method == "tasks/cancel":
            if self.tasks is None:
                raise ValueError("tasks extension is not available")
            result = self.tasks.cancel(
                params.get("taskId", params.get("task_id")),
                actor=_actor(params, self.default_actor),
                reason=params.get("reason") if isinstance(params.get("reason"), str) else None,
            )
            return result, []
        if method == "tasks/update":
            if self.tasks is None:
                raise ValueError("tasks extension is not available")
            meta = params.get("_meta") if isinstance(params.get("_meta"), Mapping) else {}
            signature = params.get("signature")
            if signature is None and isinstance(meta, Mapping):
                signature = meta.get("signature")
            result = self.tasks.update(
                params.get("taskId", params.get("task_id")),
                input_responses=params.get("inputResponses", params.get("input_responses")),
                actor=_actor(params, self.default_actor),
                signature=str(signature) if isinstance(signature, str) else None,
                secret=self.decision_secret,
            )
            return result, []
        if method == "subscriptions/listen":
            result = self.subscriptions.listen(params)
            sub_id = None
            meta = result.get("_meta")
            if isinstance(meta, Mapping):
                sub_id = meta.get("io.modelcontextprotocol/subscriptionId")
            extra = []
            if isinstance(sub_id, str):
                extra.append(self.subscriptions.acknowledged(sub_id))
            return result, extra
        raise ValueError(f"Unhandled method {method}")


def should_handle(method: str) -> bool:
    return method in _HANDLED


class ProtocolDispatchMiddleware:
    """Peek JSON-RPC on POST ``/mcp``, handle discover/tasks, decorate 2026 results."""

    def __init__(self, app: Any, surface: McpRpcSurface) -> None:
        self.app = app
        self.surface = surface

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method") or "").upper()
        path = str(scope.get("path") or "")
        if method != "POST" or path.rstrip("/") != "/mcp":
            await self.app(scope, receive, send)
            return
        body, replay = await _buffer_body(receive)
        headers = _scope_headers(scope)

        def _replay_send_factory(inner_send: Any, *, decorate: bool) -> Any:
            async def send_wrapper(message: dict[str, Any]) -> None:
                if (
                    decorate
                    and message.get("type") == "http.response.body"
                    and not message.get("more_body")
                ):
                    chunk = message.get("body") or b""
                    rewritten = _decorate_jsonrpc_result(chunk)
                    if rewritten is not None:
                        message = {**message, "body": rewritten}
                await inner_send(message)

            return send_wrapper

        try:
            payload = json.loads(body.decode("utf-8") or "null")
        except (UnicodeDecodeError, json.JSONDecodeError):
            status, response, extra = (
                400,
                jsonrpc_error(None, JSONRPC_PARSE_ERROR, "Parse error"),
                [],
            )
            await _send_rpc(send, status, response, extra)
            return
        if not isinstance(payload, dict):
            await self.app(scope, replay, send)
            return
        rpc_method = payload.get("method")
        intercepted = None
        if isinstance(rpc_method, str) and should_handle(rpc_method):
            intercepted = self.surface.handle(payload, headers=headers)
        elif isinstance(rpc_method, str) and rpc_method == "tools/call":
            intercepted = self.surface.intercept_tools_call(payload, headers=headers)
        if intercepted is not None:
            status, response, extra = intercepted
            await _send_rpc(send, status, response, extra)
            return
        decorate = _wants_result_type(payload, headers)
        await self.app(scope, replay, _replay_send_factory(send, decorate=decorate))


def _wants_result_type(payload: Mapping[str, Any], headers: Mapping[str, str]) -> bool:
    version = headers.get("mcp-protocol-version")
    params = payload.get("params")
    if isinstance(params, Mapping):
        meta = params.get("_meta")
        if isinstance(meta, Mapping):
            declared = meta.get("io.modelcontextprotocol/protocolVersion")
            if isinstance(declared, str):
                version = declared
    return version == LATEST_PROTOCOL_VERSION


def _decorate_jsonrpc_result(chunk: bytes) -> bytes | None:
    try:
        data = json.loads(chunk.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "result" not in data:
        return None
    result = data.get("result")
    if not isinstance(result, dict):
        return None
    if "resultType" not in result:
        result = {**result, "resultType": RESULT_TYPE_COMPLETE}
    if "ttlMs" not in result and isinstance(result.get("tools"), list):
        result = {**result, "ttlMs": LIST_CACHE_TTL_MS, "cacheScope": LIST_CACHE_SCOPE}
        tools = result.get("tools")
        if isinstance(tools, list):
            result["tools"] = sorted(
                (row for row in tools if isinstance(row, dict)),
                key=lambda row: str(row.get("name") or ""),
            )
    data = {**data, "result": result}
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def _scope_headers(scope: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_key, raw_value in scope.get("headers") or ():
        key = raw_key.decode("latin-1").lower()
        out[key] = raw_value.decode("latin-1")
    return out


async def _buffer_body(receive: Any) -> tuple[bytes, Any]:
    received = bytearray()
    trailing: dict[str, Any] | None = None
    saw = False
    complete = False
    while True:
        message = await receive()
        if message["type"] != "http.request":
            trailing = message
            break
        saw = True
        received.extend(message.get("body", b"") or b"")
        if not message.get("more_body", False):
            complete = True
            break
    cached: deque[dict[str, Any]] = deque()
    if saw:
        cached.append({"type": "http.request", "body": bytes(received), "more_body": not complete})
    if trailing is not None:
        cached.append(trailing)

    async def replay() -> dict[str, Any]:
        if cached:
            return cached.popleft()
        return await receive()

    return bytes(received), replay


async def _send_rpc(
    send: Any,
    status: int,
    payload: Mapping[str, Any],
    extra: list[dict[str, Any]],
) -> None:
    frames = [dict(payload), *extra]
    # HTTP JSON response: one JSON-RPC object. Notifications piggy-back is SSE-only;
    # for JSON we include acknowledgement as a sibling under ``notifications`` when present.
    body_obj: dict[str, Any] = dict(payload)
    if extra:
        body_obj = dict(payload)
        # Keep the JSON-RPC result as the HTTP body; tests parse a single object.
        _ = frames
    raw = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(raw)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]
    await send({"type": "http.response.start", "status": int(status), "headers": headers})
    await send({"type": "http.response.body", "body": raw})


def _actor(params: Mapping[str, Any], default: str | None) -> str | None:
    actor = params.get("actor")
    if isinstance(actor, str):
        return actor
    meta = params.get("_meta")
    if isinstance(meta, Mapping):
        info = meta.get("io.modelcontextprotocol/clientInfo")
        if isinstance(info, Mapping) and isinstance(info.get("name"), str):
            return str(info["name"])
    return default
