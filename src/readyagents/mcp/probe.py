"""Read-only MCP ``server/discover`` / ``initialize`` probe. Never calls a tool."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse, urlunparse

from readyagents.errors import MCPError
from readyagents.mcp.protocol import (
    LATEST_PROTOCOL_VERSION,
    META_CLIENT_INFO,
    META_PROTOCOL_VERSION,
    TASKS_EXTENSION,
)


def probe_server(url: str, *, timeout: float = 10.0) -> dict[str, Any]:
    target = _mcp_url(url)
    parsed = urlparse(target)
    if parsed.scheme not in {"http", "https"}:
        raise MCPError("mcp probe only supports http(s) URLs")
    discover_body = {
        "jsonrpc": "2.0",
        "id": "probe-discover",
        "method": "server/discover",
        "params": {
            "_meta": {
                META_PROTOCOL_VERSION: LATEST_PROTOCOL_VERSION,
                META_CLIENT_INFO: {"name": "readyagents-probe", "version": "0"},
            }
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Mcp-Method": "server/discover",
        "Mcp-Protocol-Version": LATEST_PROTOCOL_VERSION,
    }
    try:
        payload = _post_json(target, discover_body, headers=headers, timeout=timeout)
        result = payload.get("result") if isinstance(payload, dict) else None
        if isinstance(result, dict) and (
            result.get("supportedVersions") or result.get("protocolVersions")
        ):
            versions = result.get("supportedVersions") or result.get("protocolVersions") or []
            caps = (
                result.get("capabilities") if isinstance(result.get("capabilities"), dict) else {}
            )
            extensions = []
            ext = caps.get("extensions")
            if isinstance(ext, dict):
                extensions = sorted(str(k) for k in ext)
            elif TASKS_EXTENSION in json.dumps(result):
                extensions = [TASKS_EXTENSION]
            return {
                "url": target,
                "negotiated": "server/discover",
                "protocol_versions": list(versions),
                "extensions": extensions,
                "server_info": (result.get("_meta") or {}).get(
                    "io.modelcontextprotocol/serverInfo"
                ),
            }
    except MCPError:
        raise
    except Exception:  # noqa: BLE001
        pass
    init_body = {
        "jsonrpc": "2.0",
        "id": "probe-init",
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "readyagents-probe", "version": "0"},
        },
    }
    init_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    try:
        payload = _post_json(target, init_body, headers=init_headers, timeout=timeout)
    except Exception as err:  # noqa: BLE001
        raise MCPError(f"mcp probe failed: {err}") from err
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        raise MCPError("mcp probe: initialize returned no result")
    version = result.get("protocolVersion") or "2025-11-25"
    caps = result.get("capabilities") if isinstance(result.get("capabilities"), dict) else {}
    extensions = (
        sorted(str(k) for k in (caps.get("extensions") or {}))
        if isinstance(caps.get("extensions"), dict)
        else []
    )
    return {
        "url": target,
        "negotiated": "initialize",
        "protocol_versions": [version],
        "extensions": extensions,
        "server_info": result.get("serverInfo"),
    }


def _mcp_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        raise MCPError("URL is required")
    parsed = urlparse(raw)
    if not parsed.scheme:
        raw = "http://" + raw
        parsed = urlparse(raw)
    path = parsed.path or ""
    if path in {"", "/"}:
        parsed = parsed._replace(path="/mcp")
    return urlunparse(parsed)


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("content-type") or ""
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            parsed = json.loads(raw.decode("utf-8") or "null")
        except json.JSONDecodeError:
            raise MCPError(f"mcp probe HTTP {err.code}") from err
        if isinstance(parsed, dict):
            if "error" in parsed and "result" not in parsed:
                message = parsed.get("error", {})
                if isinstance(message, dict):
                    raise MCPError(str(message.get("message") or message)) from err
                raise MCPError(str(message)) from err
            return parsed
        raise MCPError(f"mcp probe HTTP {err.code}") from err
    except urllib.error.URLError as err:
        raise MCPError(f"mcp probe failed: {err}") from err
    text = raw.decode("utf-8", errors="replace")
    if "text/event-stream" in ctype:
        for line in text.splitlines():
            if line.startswith("data:"):
                blob = line[5:].strip()
                if blob and blob != "[DONE]":
                    data_obj = json.loads(blob)
                    if isinstance(data_obj, dict):
                        return data_obj
        raise MCPError("mcp probe: no JSON-RPC payload in SSE stream")
    try:
        parsed = json.loads(text or "null")
    except json.JSONDecodeError as err:
        raise MCPError("mcp probe: response is not JSON") from err
    if not isinstance(parsed, dict):
        raise MCPError("mcp probe: response is not a JSON object")
    return parsed
