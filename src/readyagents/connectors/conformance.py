"""Conformance harness. Fails own-socket, non-granted secret, and cap bypass.

In-process only — not an OS sandbox. Native extensions can still bypass.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from readyagents.connectors.context import (
    ConnectorContext,
    bind_context,
    in_context_http,
    reset_context,
)
from readyagents.connectors.spec import Connector, ConnectorSpec
from readyagents.credentials.broker import GrantedSecret, scoped_env
from readyagents.errors import ConnectorAuthError, ConnectorCapError


@dataclass(frozen=True)
class ConformanceFailure:
    check: str
    message: str


def run_conformance(
    connector: Connector,
    *,
    args: Mapping[str, Any] | None = None,
    granted: Mapping[str, str] | None = None,
    workspace: Any = None,
) -> list[ConformanceFailure]:
    """Return failures. Empty list means the connector passed."""
    from pathlib import Path

    failures: list[ConformanceFailure] = []
    spec = connector.spec
    schema_fail = _schema_issues(spec)
    if schema_fail:
        failures.append(schema_fail)
    own_socket = {"hit": False}
    environ_hit = {"name": None}

    orig_connect = socket.socket.connect
    orig_create = socket.create_connection
    orig_getenv = os.environ.get
    orig_getitem = os.environ.__getitem__

    def _connect(self: socket.socket, address: Any, *a: Any, **k: Any) -> Any:
        if not in_context_http():
            own_socket["hit"] = True
            raise OSError("conformance: own socket refused")
        return orig_connect(self, address, *a, **k)

    def _create(*a: Any, **k: Any) -> Any:
        if not in_context_http():
            own_socket["hit"] = True
            raise OSError("conformance: own socket refused")
        return orig_create(*a, **k)

    def _getenv(key: str, default: Any = None) -> Any:
        granted_names = set((granted or {}).keys())
        if key not in granted_names and _looks_secret(key):
            environ_hit["name"] = key
        return orig_getenv(key, default)

    def _getitem(key: str) -> str:
        granted_names = set((granted or {}).keys())
        if key not in granted_names and _looks_secret(key):
            environ_hit["name"] = key
        return orig_getitem(key)

    socket.socket.connect = _connect  # type: ignore[method-assign]
    socket.create_connection = _create  # type: ignore[assignment]
    os.environ.get = _getenv  # type: ignore[method-assign]
    os.environ.__getitem__ = _getitem  # type: ignore[method-assign]
    try:
        ctx = ConnectorContext(
            spec,
            workspace=Path(workspace) if workspace is not None else Path.cwd(),
            max_bytes=64,
        )
        token = bind_context(ctx)
        secrets = [
            GrantedSecret(name=str(k), value=str(v), kind="static")
            for k, v in dict(granted or {}).items()
        ]
        try:
            with scoped_env(secrets, managed=set()):
                result = connector.call(dict(args or {}), ctx)
            encoded = str(result)
            if len(encoded.encode("utf-8")) > ctx.max_bytes:
                failures.append(
                    ConformanceFailure("exceeds_caps", "connector result exceeded size cap")
                )
        except ConnectorCapError:
            pass
        except ConnectorAuthError as extra:
            if "was not granted" in str(extra):
                failures.append(ConformanceFailure("non_granted_secret", str(extra)))
        except OSError:
            pass
        except Exception as extra:  # noqa: BLE001
            if own_socket["hit"] or "own socket" in str(extra).lower():
                pass
            elif "not granted" in str(extra).lower() or "os.environ" in str(extra).lower():
                failures.append(ConformanceFailure("non_granted_secret", str(extra)))
        finally:
            reset_context(token)
    finally:
        socket.socket.connect = orig_connect
        socket.create_connection = orig_create
        os.environ.get = orig_getenv
        os.environ.__getitem__ = orig_getitem

    if own_socket["hit"]:
        failures.append(
            ConformanceFailure(
                "own_socket", "connector opened a socket outside ConnectorContext.http"
            )
        )
    if environ_hit["name"]:
        failures.append(
            ConformanceFailure(
                "non_granted_secret",
                f"connector read os.environ[{environ_hit['name']!r}]",
            )
        )
    return failures


def _looks_secret(name: str) -> bool:
    upper = name.upper()
    return any(token in upper for token in ("SECRET", "TOKEN", "PASSWORD", "API_KEY", "KEY"))


def _schema_issues(spec: ConnectorSpec) -> ConformanceFailure | None:
    if not spec.name or not spec.input_schema or not spec.output_schema:
        return ConformanceFailure("schema", "connector spec is missing name or schemas")
    if spec.side_effects not in {"none", "read", "write"}:
        return ConformanceFailure("schema", "invalid side_effects")
    if spec.determinism not in {"recomputed", "sealable", "unsealable"}:
        return ConformanceFailure("schema", "invalid determinism")
    return None
