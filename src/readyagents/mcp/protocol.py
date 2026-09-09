"""MCP protocol revisions, per-request ``_meta``, and capability advertisement.

Dependency-free: this module must import on a core install without the ``mcp`` extra.
Behaviour is selected per request, not per process.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from readyagents.errors import HeaderMismatchError, UnsupportedProtocolVersionError

SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = ("2026-07-28", "2025-11-25", "2025-06-18")
LATEST_PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-11-25"

META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
META_LOG_LEVEL = "io.modelcontextprotocol/logLevel"
META_SUBSCRIPTION_ID = "io.modelcontextprotocol/subscriptionId"
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"

RESULT_TYPE_COMPLETE = "complete"
RESULT_TYPE_INPUT_REQUIRED = "input_required"
RESULT_TYPE_TASK = "task"

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603
JSONRPC_HEADER_MISMATCH = -32020
JSONRPC_MISSING_CAPABILITY = -32021
JSONRPC_UNSUPPORTED_PROTOCOL = -32022
JSONRPC_TASK_STATE = -32023

LIST_CACHE_TTL_MS = 5_000
LIST_CACHE_SCOPE = "private"
POLL_INTERVAL_MS = 1_000
PROMPT_MAX_CHARS = 2_000
REASON_MAX_CHARS = 500
TRACE_PARENT_MAX = 128
TRACE_STATE_MAX = 512
BAGGAGE_MAX = 512

_TRACEPARENT_RE = re.compile(
    r"^[\da-f]{2}-[\da-f]{32}-[\da-f]{16}-[\da-f]{2}$",
    re.IGNORECASE,
)
_LOG_LEVELS = frozenset(
    {"debug", "info", "notice", "warning", "error", "critical", "alert", "emergency"}
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def package_version() -> str:
    try:
        from readyagents import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001
        return "0.0.0"


def server_info() -> dict[str, str]:
    return {"name": "readyagents", "version": package_version()}


def _as_mapping(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    return None


def parse_trace_context(meta: Mapping[str, Any] | None) -> dict[str, str]:
    """Shape-validate W3C trace fields. Malformed values are dropped, not forwarded."""
    if not isinstance(meta, Mapping):
        return {}
    out: dict[str, str] = {}
    parent = meta.get("traceparent")
    if isinstance(parent, str):
        text = parent.strip()
        if 0 < len(text) <= TRACE_PARENT_MAX and _TRACEPARENT_RE.fullmatch(text):
            out["traceparent"] = text.lower()
    state = meta.get("tracestate")
    if isinstance(state, str):
        text = state.strip()
        if 0 < len(text) <= TRACE_STATE_MAX and text.isprintable() and "\n" not in text:
            out["tracestate"] = text
    baggage = meta.get("baggage")
    if isinstance(baggage, str):
        text = baggage.strip()
        if 0 < len(text) <= BAGGAGE_MAX and text.isprintable() and "\n" not in text:
            out["baggage"] = text
    return out


def _extensions_from_capabilities(caps: Mapping[str, Any]) -> frozenset[str]:
    raw = caps.get("extensions")
    if not isinstance(raw, Mapping):
        return frozenset()
    return frozenset(str(key) for key in raw if str(key).strip())


@dataclass(frozen=True)
class RequestContext:
    """Defensive parse of JSON-RPC ``params._meta`` for one request."""

    protocol_version: str
    client_capabilities: Mapping[str, Any]
    client_info: Mapping[str, Any] | None
    log_level: str | None
    trace_context: Mapping[str, str]
    extensions: frozenset[str]

    @property
    def is_stateless(self) -> bool:
        return self.protocol_version == LATEST_PROTOCOL_VERSION

    def supports(self, extension: str) -> bool:
        return extension in self.extensions

    @classmethod
    def from_meta(
        cls,
        meta: Any,
        *,
        honoured: Sequence[str] | None = None,
        header_version: str | None = None,
    ) -> RequestContext:
        supported = tuple(honoured) if honoured is not None else honoured_protocol_versions()
        if meta is None:
            data: dict[str, Any] = {}
        elif isinstance(meta, Mapping):
            data = dict(meta)
        else:
            raise ValueError("_meta must be a JSON object")

        raw_version = data.get(META_PROTOCOL_VERSION)
        declared_in_meta = raw_version is not None
        if raw_version is None and header_version:
            raw_version = header_version
        if raw_version is None:
            version = next(
                (item for item in supported if item != LATEST_PROTOCOL_VERSION),
                LEGACY_PROTOCOL_VERSION,
            )
            if version not in supported and supported:
                version = supported[-1]
        elif not isinstance(raw_version, str) or not raw_version.strip():
            raise ValueError(f"{META_PROTOCOL_VERSION} must be a string")
        else:
            version = raw_version.strip()
            if version not in supported:
                if declared_in_meta:
                    raise UnsupportedProtocolVersionError(version, supported)
                # Handshake-era MCP-Protocol-Version headers (e.g. 2025-03-26) are
                # not _meta negotiation; pass them through as the newest legacy
                # revision rather than 32022, so older initialize clients keep working.
                version = next(
                    (item for item in supported if item != LATEST_PROTOCOL_VERSION),
                    supported[-1] if supported else LEGACY_PROTOCOL_VERSION,
                )

        caps_raw = data.get(META_CLIENT_CAPABILITIES)
        if caps_raw is None:
            caps: dict[str, Any] = {}
        elif isinstance(caps_raw, Mapping):
            caps = dict(caps_raw)
        else:
            raise ValueError(f"{META_CLIENT_CAPABILITIES} must be a JSON object")

        info_raw = data.get(META_CLIENT_INFO)
        if info_raw is None:
            info = None
        elif isinstance(info_raw, Mapping):
            info = dict(info_raw)
        else:
            raise ValueError(f"{META_CLIENT_INFO} must be a JSON object")

        log_raw = data.get(META_LOG_LEVEL)
        log_level = None
        if log_raw is not None:
            if not isinstance(log_raw, str):
                raise ValueError(f"{META_LOG_LEVEL} must be a string")
            candidate = log_raw.strip().lower()
            if candidate in _LOG_LEVELS:
                log_level = candidate

        return cls(
            protocol_version=version,
            client_capabilities=caps,
            client_info=info,
            log_level=log_level,
            trace_context=parse_trace_context(data),
            extensions=_extensions_from_capabilities(caps),
        )


def request_context_from_rpc(
    payload: Mapping[str, Any],
    *,
    honoured: Sequence[str] | None = None,
    header_version: str | None = None,
) -> RequestContext:
    params = payload.get("params")
    meta = None
    if isinstance(params, Mapping):
        meta = params.get("_meta")
    return RequestContext.from_meta(meta, honoured=honoured, header_version=header_version)


def jsonrpc_error(
    rpc_id: Any,
    code: int,
    message: str,
    data: Any | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": int(code), "message": str(message)}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": error}


def error_from_exception(rpc_id: Any, exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, UnsupportedProtocolVersionError):
        return jsonrpc_error(
            rpc_id,
            JSONRPC_UNSUPPORTED_PROTOCOL,
            str(exc),
            {"requested": exc.requested, "supported": list(exc.supported)},
        )
    if isinstance(exc, HeaderMismatchError):
        return jsonrpc_error(rpc_id, JSONRPC_HEADER_MISMATCH, str(exc))
    from readyagents.errors import TaskStateError

    if isinstance(exc, TaskStateError):
        data = {"run_id": exc.run_id} if exc.run_id else None
        return jsonrpc_error(rpc_id, JSONRPC_TASK_STATE, str(exc), data)
    from readyagents.errors import AuthorizationError, ConfigError, HttpRequestError, RunConflict

    if isinstance(exc, AuthorizationError):
        return jsonrpc_error(rpc_id, JSONRPC_INVALID_PARAMS, str(exc))
    if isinstance(exc, RunConflict):
        return jsonrpc_error(rpc_id, JSONRPC_INVALID_PARAMS, str(exc))
    if isinstance(exc, HttpRequestError):
        return jsonrpc_error(rpc_id, JSONRPC_INVALID_PARAMS, str(exc))
    if isinstance(exc, ConfigError):
        return jsonrpc_error(rpc_id, JSONRPC_INVALID_PARAMS, str(exc))
    if isinstance(exc, ValueError):
        return jsonrpc_error(rpc_id, JSONRPC_INVALID_PARAMS, str(exc))
    return jsonrpc_error(rpc_id, JSONRPC_INTERNAL_ERROR, "internal error")


def http_status_for_rpc_error(code: int) -> int:
    if code in {
        JSONRPC_HEADER_MISMATCH,
        JSONRPC_UNSUPPORTED_PROTOCOL,
        JSONRPC_INVALID_PARAMS,
        JSONRPC_PARSE_ERROR,
    }:
        return 400
    if code == JSONRPC_METHOD_NOT_FOUND:
        return 404
    if code == JSONRPC_MISSING_CAPABILITY:
        return 400
    return 200


def stamp_result_meta(
    result: dict[str, Any], *, ctx: RequestContext | None = None
) -> dict[str, Any]:
    body = dict(result)
    meta = dict(body.get("_meta") or {}) if isinstance(body.get("_meta"), Mapping) else {}
    meta[META_SERVER_INFO] = server_info()
    if ctx is not None and ctx.trace_context:
        for key, value in ctx.trace_context.items():
            meta.setdefault(key, value)
    body["_meta"] = meta
    return body


def ensure_result_type(
    result: dict[str, Any], result_type: str = RESULT_TYPE_COMPLETE
) -> dict[str, Any]:
    body = dict(result)
    if "resultType" not in body:
        body["resultType"] = result_type
    return body


def jsonrpc_result(
    rpc_id: Any, result: Mapping[str, Any], *, ctx: RequestContext | None = None
) -> dict[str, Any]:
    body = ensure_result_type(dict(result))
    body = stamp_result_meta(body, ctx=ctx)
    return {"jsonrpc": "2.0", "id": rpc_id, "result": body}


def sdk_capability() -> dict[str, Any]:
    """What the installed ``mcp`` extra can honour. Never imports 2.x-only names at module load."""
    try:
        import mcp  # noqa: F401
    except ImportError:
        return {"package": "mcp", "version": None, "tier": "absent"}
    version: str | None = None
    try:
        from importlib.metadata import version as dist_version

        version = dist_version("mcp")
    except Exception:  # noqa: BLE001
        version = getattr(mcp, "__version__", None)
    has_server = False
    has_fast = False
    try:
        from mcp.server import MCPServer  # noqa: F401

        has_server = True
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP  # noqa: F401

        has_fast = True
    except ImportError:
        pass
    if has_server:
        tier = "full"
    elif has_fast:
        tier = "legacy"
    else:
        tier = "unknown"
    return {"package": "mcp", "version": version, "tier": tier}


def honoured_protocol_versions() -> tuple[str, ...]:
    """Versions this process will advertise. Never list a revision we cannot fully honour."""
    pin = os.environ.get("READYAGENTS_MCP_PROTOCOL_MAX", "").strip()
    versions = list(SUPPORTED_PROTOCOL_VERSIONS)
    if pin:
        versions = [item for item in versions if item <= pin]
    cap = sdk_capability()
    if cap["tier"] == "absent":
        return ()
    if cap["tier"] == "legacy":
        return tuple(item for item in versions if item != LATEST_PROTOCOL_VERSION)
    return tuple(versions)


def advertised_extensions(versions: Sequence[str] | None = None) -> list[str]:
    honoured = tuple(versions) if versions is not None else honoured_protocol_versions()
    if LATEST_PROTOCOL_VERSION in honoured:
        return [TASKS_EXTENSION]
    return []


def discover_capabilities(versions: Sequence[str] | None = None) -> dict[str, Any]:
    honoured = tuple(versions) if versions is not None else honoured_protocol_versions()
    caps: dict[str, Any] = {"tools": {"listChanged": True}}
    extensions = advertised_extensions(honoured)
    if extensions:
        caps["extensions"] = {name: {} for name in extensions}
    return caps


def discover_result(versions: Sequence[str] | None = None) -> dict[str, Any]:
    honoured = tuple(versions) if versions is not None else honoured_protocol_versions()
    result: dict[str, Any] = {
        "resultType": RESULT_TYPE_COMPLETE,
        "supportedVersions": list(honoured),
        "capabilities": discover_capabilities(honoured),
        "ttlMs": LIST_CACHE_TTL_MS,
        "cacheScope": LIST_CACHE_SCOPE,
        "_meta": {META_SERVER_INFO: server_info()},
    }
    return result


def serve_json_envelope(*, transport: str) -> dict[str, Any]:
    honoured = honoured_protocol_versions()
    return {
        "ok": True,
        "command": "mcp serve",
        "transport": transport,
        "protocol_versions": list(honoured),
        "extensions": advertised_extensions(honoured),
        "sdk": sdk_capability(),
        "deprecated_endpoints": ["/runs"],
    }


def sanitize_prompt(
    text: str, *, redactor: Any | None = None, limit: int = PROMPT_MAX_CHARS
) -> str:
    raw = text if isinstance(text, str) else str(text)
    cleaned = _CONTROL_RE.sub("", raw)
    if redactor is not None:
        method = getattr(redactor, "redact_text", None)
        if callable(method):
            cleaned = str(method(cleaned))
        else:
            cleaned = str(redactor.redact(cleaned) if hasattr(redactor, "redact") else cleaned)
    if len(cleaned) > limit:
        cleaned = cleaned[:limit]
    return cleaned


def mcp_method_header_ok(method: str, header: str | None) -> bool:
    if header is None or not str(header).strip():
        return False
    return str(header).strip() == method


def mcp_name_for_method(method: str, params: Mapping[str, Any] | None) -> str | None:
    data = params if isinstance(params, Mapping) else {}
    if method == "tools/call":
        name = data.get("name")
        return str(name) if isinstance(name, str) and name.strip() else None
    if method in {"tasks/get", "tasks/update", "tasks/cancel"}:
        task_id = data.get("taskId", data.get("task_id"))
        return str(task_id) if isinstance(task_id, str) and task_id.strip() else None
    return None


def require_transport_headers(
    *,
    method: str,
    params: Mapping[str, Any] | None,
    mcp_method: str | None,
    mcp_name: str | None,
    protocol_version: str,
) -> None:
    if protocol_version != LATEST_PROTOCOL_VERSION:
        return
    if not mcp_method_header_ok(method, mcp_method):
        raise HeaderMismatchError(
            "Mcp-Method header is required and must match the JSON-RPC method"
        )
    expected_name = mcp_name_for_method(method, params)
    if expected_name is None:
        return
    if mcp_name is None or str(mcp_name).strip() != expected_name:
        raise HeaderMismatchError("Mcp-Name header is required and must match the JSON-RPC params")
