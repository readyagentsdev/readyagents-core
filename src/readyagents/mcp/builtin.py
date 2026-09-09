"""Python-native tools that work with zero extra servers."""

from __future__ import annotations

import ast
import http.client
import ipaddress
import json
import operator
import socket
import ssl
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import ParseResult, urljoin, urlparse

from readyagents import __version__
from readyagents.errors import AtomicWriteError, ToolError
from readyagents.tools import FunctionTool, Tool

_BINOPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY: dict[type, Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_HTTP_TIMEOUT = 20
_MAX_HTTP_BYTES = 1_000_000
_MAX_HTTP_REDIRECTS = 5
_MAX_POW_EXP = 32
_MAX_FILE_BYTES = 1_000_000
_MAX_LIST_DIR_ENTRIES = 500
_MAX_JSON_BYTES = 1_000_000
_MAX_CALC_CHARS = 256
_JSON_REFUSED_SEGMENTS = frozenset({"constructor", "prototype"})
_BLOCKED_HOST_NAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata.google.internal",
    }
)


def builtin_tools(*, allow_http: bool, workspace: Path) -> list[Tool]:
    workspace = Path(workspace).resolve()
    tools: list[Tool] = [
        FunctionTool(
            name="now",
            description="Current UTC time as ISO-8601.",
            schema={"type": "object", "properties": {}},
            handler=tool_now,
        ),
        FunctionTool(
            name="calc",
            description="Evaluate a safe arithmetic expression (e.g. '2 + 2 * 10').",
            schema={
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
            },
            handler=tool_calc,
        ),
        FunctionTool(
            name="json_get",
            description="Extract a dotted path from JSON text or an object.",
            schema={
                "type": "object",
                "properties": {
                    "data": {},
                    "path": {"type": "string"},
                },
                "required": ["data", "path"],
            },
            handler=tool_json_get,
        ),
        FunctionTool(
            name="json_set",
            description="Set a dotted path in JSON text or an object and return the document.",
            schema={
                "type": "object",
                "properties": {
                    "data": {},
                    "path": {"type": "string"},
                    "value": {},
                },
                "required": ["data", "path", "value"],
            },
            handler=tool_json_set,
        ),
        FunctionTool(
            name="json_merge",
            description="Merge a JSON object at a dotted path (empty path merges at the root).",
            schema={
                "type": "object",
                "properties": {
                    "data": {},
                    "path": {"type": "string"},
                    "value": {},
                },
                "required": ["data", "path", "value"],
            },
            handler=tool_json_merge,
        ),
        FunctionTool(
            name="list_dir",
            description="List a directory sandboxed to the workflow workspace (dotfiles skipped).",
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "include_hidden": {"type": "boolean"},
                    "max_entries": {"type": "integer"},
                },
            },
            handler=lambda path=".", include_hidden=False, max_entries=200: tool_list_dir(
                path,
                workspace=workspace,
                include_hidden=include_hidden,
                max_entries=max_entries,
            ),
        ),
        FunctionTool(
            name="read_file",
            description="Read a UTF-8 text file sandboxed to the workflow workspace.",
            schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=lambda path: tool_read_file(path, workspace=workspace),
        ),
        FunctionTool(
            name="write_file",
            description="Write a UTF-8 text file sandboxed to the workflow workspace.",
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            handler=lambda path, content: tool_write_file(path, content, workspace=workspace),
        ),
        FunctionTool(
            name="http_get",
            description="HTTP GET a URL. Disabled unless allow_http is enabled.",
            schema={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            handler=lambda url: tool_http_get(url, allow_http=allow_http),
        ),
    ]
    return tools


def tool_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def tool_calc(expression: str | int | float) -> int | float:
    if isinstance(expression, (int, float)):
        return expression
    text = str(expression).strip()
    if not text:
        raise ToolError("calc: empty expression")
    if len(text) > _MAX_CALC_CHARS:
        raise ToolError(f"calc: expression too long (max {_MAX_CALC_CHARS} characters)")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ToolError(f"calc: invalid expression: {exc}") from exc
    return _eval_ast(tree.body)


def _eval_ast(node: ast.AST) -> int | float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        if isinstance(node.value, bool):
            raise ToolError("calc: only numbers and + - * / // % ** are allowed")
        return node.value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval_ast(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left = _eval_ast(node.left)
        right = _eval_ast(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_POW_EXP:
            raise ToolError("calc: exponent too large")
        try:
            return _BINOPS[type(node.op)](left, right)
        except ZeroDivisionError as exc:
            raise ToolError("calc: division by zero") from exc
    if isinstance(node, ast.Expr):
        return _eval_ast(node.value)
    raise ToolError("calc: only numbers and + - * / // % ** are allowed")


def tool_json_get(data: Any, path: str) -> Any:
    current: Any = _parse_json_input(data, tool="json_get")
    for part in str(path).split("."):
        if part == "":
            continue
        if isinstance(current, dict):
            if part not in current:
                raise ToolError(f"json_get: path not found: {path}")
            current = current[part]
            continue
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as exc:
                raise ToolError(f"json_get: path not found: {path}") from exc
            continue
        raise ToolError(f"json_get: path not found: {path}")
    return current


def tool_json_set(data: Any, path: str, value: Any) -> Any:
    doc = _json_clone(_parse_json_input(data, tool="json_set"), tool="json_set")
    parsed = _json_clone(_parse_json_value(value, tool="json_set"), tool="json_set")
    parts = _json_path_parts(path, tool="json_set")
    result = _json_assign(doc, parts, parsed, tool="json_set")
    _assert_json_size(result, tool="json_set")
    return result


def tool_json_merge(data: Any, path: str, value: Any) -> Any:
    doc = _json_clone(_parse_json_input(data, tool="json_merge"), tool="json_merge")
    parsed = _json_clone(_parse_json_value(value, tool="json_merge"), tool="json_merge")
    if not isinstance(parsed, dict):
        raise ToolError("json_merge: value must be an object")
    parts = _json_path_parts(path, tool="json_merge", allow_root=True)
    result = _json_merge_at(doc, parts, parsed, tool="json_merge")
    _assert_json_size(result, tool="json_merge")
    return result


def _parse_json_input(data: Any, *, tool: str) -> Any:
    current: Any = data
    if isinstance(current, (bytes, bytearray)):
        if len(current) > _MAX_JSON_BYTES:
            raise ToolError(f"{tool}: data too large (max {_MAX_JSON_BYTES} bytes)")
        current = current.decode("utf-8")
    if isinstance(current, str):
        if len(current.encode("utf-8")) > _MAX_JSON_BYTES:
            raise ToolError(f"{tool}: data too large (max {_MAX_JSON_BYTES} bytes)")
        text = current.strip()
        if text:
            try:
                current = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ToolError(f"{tool}: data is not valid JSON: {exc}") from exc
    return current


def _parse_json_value(value: Any, *, tool: str) -> Any:
    current: Any = value
    if isinstance(current, (bytes, bytearray)):
        if len(current) > _MAX_JSON_BYTES:
            raise ToolError(f"{tool}: value too large (max {_MAX_JSON_BYTES} bytes)")
        current = current.decode("utf-8")
    if isinstance(current, str):
        if len(current.encode("utf-8")) > _MAX_JSON_BYTES:
            raise ToolError(f"{tool}: value too large (max {_MAX_JSON_BYTES} bytes)")
        text = current.strip()
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return current
    return current


def _json_clone(data: Any, *, tool: str) -> Any:
    try:
        return json.loads(json.dumps(data, ensure_ascii=False))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ToolError(f"{tool}: data is not valid JSON: {exc}") from exc


def _assert_json_size(data: Any, *, tool: str) -> None:
    try:
        blob = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{tool}: result is not valid JSON: {exc}") from exc
    if len(blob.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ToolError(f"{tool}: result too large (max {_MAX_JSON_BYTES} bytes)")


def _json_path_parts(path: str, *, tool: str, allow_root: bool = False) -> list[str]:
    text = str(path)
    if allow_root and text in {"", "."}:
        return []
    parts = text.split(".")
    for part in parts:
        if part == "":
            raise ToolError(f"{tool}: empty path segment")
        if part.startswith("__") or part in _JSON_REFUSED_SEGMENTS:
            raise ToolError(f"{tool}: refused path segment: {part}")
    return parts


def _json_assign(doc: Any, parts: list[str], value: Any, *, tool: str) -> Any:
    if not parts:
        raise ToolError(f"{tool}: path is required")
    if not isinstance(doc, (dict, list)):
        raise ToolError(f"{tool}: data must be an object or array")
    parent = _json_walk_parent(doc, parts[:-1], tool=tool)
    _json_put(parent, parts[-1], value, tool=tool)
    return doc


def _json_merge_at(doc: Any, parts: list[str], value: dict[str, Any], *, tool: str) -> Any:
    if not parts:
        if not isinstance(doc, dict):
            raise ToolError(f"{tool}: target is not an object")
        doc.update(value)
        return doc
    if not isinstance(doc, (dict, list)):
        raise ToolError(f"{tool}: data must be an object or array")
    parent = _json_walk_parent(doc, parts[:-1], tool=tool)
    target = _json_child_object(parent, parts[-1], tool=tool)
    target.update(value)
    return doc


def _json_walk_parent(doc: Any, parts: list[str], *, tool: str) -> Any:
    current = doc
    for part in parts:
        current = _json_ensure_container(current, part, tool=tool)
    return current


def _json_ensure_container(current: Any, part: str, *, tool: str) -> Any:
    if isinstance(current, dict):
        child = current.get(part, None)
        if child is None:
            current[part] = {}
            return current[part]
        if not isinstance(child, (dict, list)):
            raise ToolError(f"{tool}: cannot descend into non-container at {part}")
        return child
    if isinstance(current, list):
        idx = _json_list_index(current, part, tool=tool)
        child = current[idx]
        if child is None:
            current[idx] = {}
            return current[idx]
        if not isinstance(child, (dict, list)):
            raise ToolError(f"{tool}: cannot descend into non-container at {part}")
        return child
    raise ToolError(f"{tool}: cannot descend into non-container at {part}")


def _json_put(parent: Any, part: str, value: Any, *, tool: str) -> None:
    if isinstance(parent, dict):
        parent[part] = value
        return
    if isinstance(parent, list):
        parent[_json_list_index(parent, part, tool=tool)] = value
        return
    raise ToolError(f"{tool}: cannot set path")


def _json_child_object(parent: Any, part: str, *, tool: str) -> dict[str, Any]:
    if isinstance(parent, dict):
        child = parent.get(part, None)
        if child is None:
            parent[part] = {}
            return parent[part]
        if not isinstance(child, dict):
            raise ToolError(f"{tool}: target is not an object")
        return child
    if isinstance(parent, list):
        idx = _json_list_index(parent, part, tool=tool)
        child = parent[idx]
        if child is None:
            parent[idx] = {}
            return parent[idx]
        if not isinstance(child, dict):
            raise ToolError(f"{tool}: target is not an object")
        return child
    raise ToolError(f"{tool}: target is not an object")


def _json_list_index(seq: list[Any], part: str, *, tool: str) -> int:
    try:
        idx = int(part)
        seq[idx]
    except (ValueError, IndexError) as exc:
        raise ToolError(f"{tool}: path not found: {part}") from exc
    return idx


def tool_http_get(url: str, *, allow_http: bool) -> str:
    if not allow_http:
        raise ToolError(
            "http_get is disabled. Set READYAGENTS_ALLOW_HTTP=1 or set allow_http: true "
            "on the workflow if you intend to fetch URLs."
        )
    current = url.strip() if isinstance(url, str) else ""
    for _ in range(_MAX_HTTP_REDIRECTS + 1):
        parsed = _assert_public_http_url(current)
        assert parsed.hostname is not None
        ips = _resolve_public_ips(parsed.hostname)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        last_err: Exception | None = None
        status = 0
        body = b""
        location: str | None = None
        for ip in ips:
            try:
                status, body, location = _http_exchange(
                    parsed.scheme, parsed.hostname, ip, port, path
                )
                last_err = None
                break
            except (TimeoutError, OSError) as exc:
                last_err = exc
        if last_err is not None:
            raise ToolError(f"http_get failed: {last_err}") from last_err
        if status in {301, 302, 303, 307, 308} and location:
            current = urljoin(current, location)
            continue
        if status >= 400:
            raise ToolError(f"http_get failed: HTTP Error {status}:")
        if len(body) > _MAX_HTTP_BYTES:
            raise ToolError("http_get: response too large")
        return body.decode("utf-8", errors="replace")
    raise ToolError("http_get: too many redirects")


def _http_exchange(
    scheme: str,
    hostname: str,
    ip: str,
    port: int,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
) -> tuple[int, bytes, str | None]:
    timeout = _HTTP_TIMEOUT if timeout is None else timeout
    req_headers = {"User-Agent": f"readyagents/{__version__}"}
    if headers:
        req_headers.update(headers)
    if scheme == "https":
        ctx = ssl.create_default_context()
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            hostname, port, timeout=timeout, context=ctx
        )

        def connect() -> None:
            sock = socket.create_connection((ip, port), timeout)
            conn.sock = ctx.wrap_socket(sock, server_hostname=hostname)

        conn.connect = connect  # type: ignore[method-assign]
    else:
        conn = http.client.HTTPConnection(hostname, port, timeout=timeout)

        def connect() -> None:
            conn.sock = socket.create_connection((ip, port), timeout)

        conn.connect = connect  # type: ignore[method-assign]
    try:
        if body is None:
            conn.request(method, path, headers=req_headers)
        else:
            conn.request(method, path, body=body, headers=req_headers)
        resp = conn.getresponse()
        payload = resp.read(_MAX_HTTP_BYTES + 1)
        return resp.status, payload, resp.getheader("Location")
    finally:
        conn.close()


def _assert_public_http_url(url: object, *, kind: str = "http_get") -> ParseResult:
    if not isinstance(url, str) or not url.strip():
        raise ToolError(f"{kind}: url must start with http:// or https://")
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ToolError(f"{kind}: url must start with http:// or https://")
    if parsed.username is not None or parsed.password is not None:
        raise ToolError(f"{kind}: URLs with userinfo are not allowed")
    if not parsed.hostname:
        raise ToolError(f"{kind}: URL must include a host")
    return parsed


def _resolve_public_ips(host: str, *, kind: str = "http_get") -> list[str]:
    name = host.strip().lower().rstrip(".")
    not_allowed = (
        f"{kind}: host '{host}' is not allowed "
        "(loopback, private, link-local, or metadata addresses)"
    )
    if name in _BLOCKED_HOST_NAMES or name.endswith(".localhost") or name.endswith(".local"):
        raise ToolError(not_allowed)
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        ip = None
    if ip is not None:
        if _ip_is_blocked(ip):
            raise ToolError(not_allowed)
        return [str(ip)]
    try:
        infos = socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ToolError(f"{kind}: could not resolve host '{host}'") from exc
    if not infos:
        raise ToolError(f"{kind}: could not resolve host '{host}'")
    ips: list[str] = []
    seen: set[str] = set()
    for info in infos:
        addr = info[4][0]
        try:
            parsed_ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _ip_is_blocked(parsed_ip):
            raise ToolError(not_allowed)
        text = str(parsed_ip)
        if text not in seen:
            seen.add(text)
            ips.append(text)
    if not ips:
        raise ToolError(f"{kind}: could not resolve host '{host}'")
    return ips


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


def tool_list_dir(
    path: str = ".",
    *,
    workspace: Path,
    include_hidden: bool = False,
    max_entries: int = 200,
) -> list[dict[str, Any]]:
    """List a workspace directory. Dotfiles skipped unless include_hidden."""
    target = _sandbox_dir(path, workspace)
    try:
        cap = int(max_entries)
    except (TypeError, ValueError) as extra:
        raise ToolError(f"list_dir: max_entries must be an integer: {extra}") from extra
    if cap < 1:
        raise ToolError("list_dir: max_entries must be >= 1")
    cap = min(cap, _MAX_LIST_DIR_ENTRIES)
    hidden = bool(include_hidden)
    rows: list[dict[str, Any]] = []
    try:
        names = sorted(target.iterdir(), key=lambda p: p.name.lower())
    except OSError as extra:
        raise ToolError(f"list_dir: could not list {path}: {extra}") from extra
    for child in names:
        if not hidden and child.name.startswith("."):
            continue
        try:
            from readyagents.errors import PathError
            from readyagents.paths import resolve_within

            resolved_child = resolve_within(child, workspace, what="list_dir entry")
        except PathError:
            continue
        try:
            is_dir = resolved_child.is_dir()
            is_file = resolved_child.is_file()
            size = resolved_child.stat().st_size if is_file else 0
        except OSError:
            continue
        kind = "dir" if is_dir else "file" if is_file else "other"
        rows.append({"name": child.name, "type": kind, "size": int(size)})
        if len(rows) >= cap:
            break
    return rows


def _sandbox_dir(path: str, workspace: Path) -> Path:
    """Resolve a directory path inside workspace (workspace root is allowed)."""
    from readyagents.errors import PathError
    from readyagents.paths import resolve_within

    text = str(path).strip() or "."
    try:
        resolved = resolve_within(text, workspace, what="Path")
    except PathError as extra:
        raise ToolError(str(extra)) from extra
    if not resolved.is_dir():
        raise ToolError(f"list_dir: not a directory: {path}")
    return resolved


def tool_read_file(path: str, *, workspace: Path) -> str:
    target = _sandbox_path(path, workspace)
    if not target.is_file():
        raise ToolError(f"read_file: not a file: {path}")
    try:
        size = target.stat().st_size
        if size > _MAX_FILE_BYTES:
            raise ToolError(f"read_file: file too large ({size} bytes, max {_MAX_FILE_BYTES})")
        return target.read_text(encoding="utf-8")
    except ToolError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ToolError(f"read_file: could not read {path}: {exc}") from exc


def tool_write_file(path: str, content: str, *, workspace: Path) -> str:
    target = _sandbox_path(path, workspace)
    if target.exists() and not target.is_file():
        raise ToolError(f"write_file: not a file: {path}")
    payload = str(content)
    size = len(payload.encode("utf-8"))
    if size > _MAX_FILE_BYTES:
        raise ToolError(f"write_file: content too large ({size} bytes, max {_MAX_FILE_BYTES})")
    try:
        from readyagents.atomic import atomic_write_text

        atomic_write_text(target, payload, encoding="utf-8", newline="\n")
    except (OSError, AtomicWriteError) as exc:
        raise ToolError(f"write_file: could not write {path}: {exc}") from exc
    return str(target)


def _sandbox_path(path: str, workspace: Path) -> Path:
    from readyagents.errors import PathError
    from readyagents.paths import resolve_within

    text = str(path).strip()
    if not text or text in {".", ".."}:
        raise ToolError("Path must be a file inside the workspace")
    try:
        resolved = resolve_within(text, workspace, what="Path")
    except PathError as extra:
        raise ToolError(str(extra)) from extra
    root = Path(workspace).expanduser().resolve()
    if resolved == root:
        raise ToolError(f"Path '{path}' is outside the workspace")
    return resolved
