"""Offline fixture player/recorder for connector HTTP."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from readyagents.atomic import atomic_write_text
from readyagents.connectors.spec import HttpResponse
from readyagents.errors import ToolError


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme}://{parts.netloc.lower()}{path}{query}"


class FixtureStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory) if directory is not None else None
        self._rows: list[dict[str, Any]] = []
        if self.directory is not None and self.directory.is_dir():
            for path in sorted(self.directory.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(data, dict):
                    self._rows.append(data)
                elif isinstance(data, list):
                    self._rows.extend(item for item in data if isinstance(item, dict))

    def exchange(self, method: str, url: str, *, body: bytes | None = None) -> HttpResponse:
        del body
        want_method = method.upper()
        want_url = normalize_url(url)
        for row in self._rows:
            if str(row.get("method") or "GET").upper() != want_method:
                continue
            if normalize_url(str(row.get("url") or "")) != want_url:
                continue
            payload = row.get("body", b"")
            if isinstance(payload, (dict, list)):
                raw = json.dumps(payload).encode("utf-8")
            elif isinstance(payload, str):
                raw = payload.encode("utf-8")
            elif isinstance(payload, bytes):
                raw = payload
            else:
                raw = b""
            headers = {str(k): str(v) for k, v in dict(row.get("headers") or {}).items()}
            return HttpResponse(
                status=int(row.get("status") or 200),
                headers=headers,
                body=raw,
                url=url,
            )
        raise ToolError(f"no connector fixture for {want_method} {want_url}")

    def record(
        self,
        dest: Path,
        *,
        method: str,
        url: str,
        status: int,
        headers: dict[str, str],
        body: bytes,
    ) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        safe_headers = {
            key: value
            for key, value in headers.items()
            if key.lower() not in {"authorization", "cookie", "set-cookie", "x-api-key"}
        }
        payload = {
            "method": method.upper(),
            "url": normalize_url(url),
            "status": status,
            "headers": safe_headers,
            "body": body.decode("utf-8", errors="replace"),
        }
        atomic_write_text(
            dest,
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return dest
