"""Config-driven REST connector. Templates cannot expand into a new host."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import yaml

from readyagents.connectors.context import ConnectorContext
from readyagents.connectors.fixtures import FixtureStore
from readyagents.connectors.spec import AuthSpec, ConnectorSpec
from readyagents.errors import ConfigError, ToolError
from readyagents.paths import resolve_within
from readyagents.workflow.templates import interpolate

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class RestConnector:
    spec = ConnectorSpec(
        name="rest",
        version="1.0.0",
        description="Config-driven authenticated REST. Host is pinned to connector_config.",
        input_schema={
            "type": "object",
            "properties": {
                "connector_config": {"type": "string"},
                "operation": {"type": "string"},
                "fixtures": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["connector_config", "operation"],
            "additionalProperties": True,
        },
        output_schema={"type": "object"},
        auth=AuthSpec(kind="bearer", secret="REST_TOKEN"),
        destinations=("example.test",),
        determinism="sealable",
        idempotent=True,
        idempotency_key="idempotency_key",
        side_effects="read",
    )

    def call(self, args: Mapping[str, Any], ctx: ConnectorContext) -> Any:
        config_path = str(args.get("connector_config") or "").strip()
        operation = str(args.get("operation") or "").strip()
        if not config_path or not operation:
            raise ToolError("rest requires connector_config and operation")
        cfg = _load_config(ctx.workspace, config_path)
        op = (cfg.get("operations") or {}).get(operation)
        if not isinstance(op, dict):
            raise ToolError(f"rest operation {operation!r} is not in the config")
        base = str(cfg.get("base_url") or "").strip()
        if not base:
            raise ConfigError("rest connector_config must set base_url")
        base_host = (urlparse(base).hostname or "").lower()
        if not base_host:
            raise ConfigError("rest base_url must include a host")
        ctx.spec = ConnectorSpec(
            name=self.spec.name,
            version=self.spec.version,
            description=self.spec.description,
            input_schema=self.spec.input_schema,
            output_schema=self.spec.output_schema,
            auth=_auth_from_config(cfg),
            destinations=(base_host,),
            determinism=self.spec.determinism,
            idempotent=self.spec.idempotent,
            idempotency_key=self.spec.idempotency_key,
            rate_limit=self.spec.rate_limit,
            side_effects="write"
            if str(op.get("method") or "GET").upper() in _WRITE_METHODS
            else "read",
        )
        fixtures_dir = args.get("fixtures") or cfg.get("fixtures")
        if fixtures_dir:
            ctx.fixtures = FixtureStore(resolve_within(str(fixtures_dir), ctx.workspace))
        method = str(op.get("method") or "GET").upper()
        path_t = str(op.get("path") or "/")
        mapping = {str(k): v for k, v in dict(args).items()}
        mapping.update({str(k): v for k, v in dict(op.get("args") or {}).items()})
        path = interpolate(path_t, mapping)
        url = _join_pinned(base, path, base_host)
        headers = {
            str(k): interpolate(str(v), mapping) for k, v in dict(op.get("headers") or {}).items()
        }
        _apply_auth(ctx, cfg, headers)
        json_body = op.get("json")
        if isinstance(json_body, str):
            json_body = interpolate(json_body, mapping)
        query = op.get("query")
        if isinstance(query, dict):
            from urllib.parse import urlencode

            q = {str(k): interpolate(str(v), mapping) for k, v in query.items()}
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urlencode(q)}"

        def _send() -> Any:
            resp = ctx.http(method, url, headers=headers, json_body=json_body)
            if resp.status >= 400:
                raise ToolError(f"rest HTTP {resp.status}")
            try:
                return resp.json()
            except Exception:  # noqa: BLE001
                return {"body": resp.text(), "status": resp.status}

        key = str(args.get("idempotency_key") or "").strip()
        if method in _WRITE_METHODS and key:
            return ctx.idempotent(key, _send)
        return _send()


def _join_pinned(base: str, path: str, base_host: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        host = (urlparse(path).hostname or "").lower()
        if host != base_host:
            raise ToolError("rest URL template must not expand into a new host")
        return path
    joined = urljoin(base.rstrip("/") + "/", path.lstrip("/"))
    host = (urlparse(joined).hostname or "").lower()
    if host != base_host:
        raise ToolError("rest URL template must not expand into a new host")
    return joined


def _load_config(workspace: Path, rel: str) -> dict[str, Any]:
    path = resolve_within(rel, workspace)
    if not path.is_file():
        raise ConfigError(f"rest connector_config not found: {rel}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError("rest connector_config must be a mapping")
    if "base_url" not in data or "operations" not in data:
        raise ConfigError("rest connector_config requires base_url and operations")
    if not isinstance(data.get("operations"), dict):
        raise ConfigError("rest operations must be a mapping")
    return data


def _auth_from_config(cfg: Mapping[str, Any]) -> AuthSpec | None:
    raw = cfg.get("auth")
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or raw.get("type") or "none")
    secret = raw.get("secret")
    return AuthSpec(
        kind=kind if kind in {"none", "bearer", "header", "query"} else "none",  # type: ignore[arg-type]
        secret=str(secret) if secret else None,
        header=str(raw["header"]) if raw.get("header") else None,
        query_param=str(raw["query_param"]) if raw.get("query_param") else None,
    )


def _apply_auth(ctx: ConnectorContext, cfg: Mapping[str, Any], headers: dict[str, str]) -> None:
    raw = cfg.get("auth")
    if not isinstance(raw, dict):
        return
    secret_name = raw.get("secret")
    if not secret_name:
        return
    value = ctx.secret(str(secret_name))
    kind = str(raw.get("kind") or raw.get("type") or "bearer")
    if kind == "bearer":
        headers.setdefault("Authorization", f"Bearer {value}")
    elif kind == "header":
        header = str(raw.get("header") or "X-Api-Key")
        headers.setdefault(header, value)
