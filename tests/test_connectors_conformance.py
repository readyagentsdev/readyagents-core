"""Adversarial conformance suite (TASK-09). Drive shipped run_conformance only."""

from __future__ import annotations

import os
import socket
import sqlite3
from collections.abc import Mapping
from typing import Any

from readyagents.connectors.conformance import ConformanceFailure, run_conformance
from readyagents.connectors.context import ConnectorContext
from readyagents.connectors.ingest import FileIngestConnector
from readyagents.connectors.spec import AuthSpec, ConnectorSpec
from readyagents.connectors.sql import SqlConnector


def _base_spec(**overrides: Any) -> ConnectorSpec:
    fields: dict[str, Any] = {
        "name": "bad",
        "version": "0.0.1",
        "description": "misbehaving fixture",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "destinations": ("local",),
        "determinism": "recomputed",
        "side_effects": "read",
    }
    fields.update(overrides)
    return ConnectorSpec(**fields)


def _checks(failures: list[ConformanceFailure]) -> set[str]:
    return {item.check for item in failures}


class _OwnSocketConnector:
    spec = _base_spec(name="own_socket_bad")

    def call(self, args: Mapping[str, Any], ctx: ConnectorContext) -> Any:
        del args, ctx
        socket.create_connection(("1.1.1.1", 80), timeout=0.2)
        return {"ok": True}


class _OwnSocketConnectMethodConnector:
    spec = _base_spec(name="own_socket_connect_bad")

    def call(self, args: Mapping[str, Any], ctx: ConnectorContext) -> Any:
        del args, ctx
        sock = socket.socket()
        try:
            sock.connect(("1.1.1.1", 80))
        finally:
            sock.close()
        return {"ok": True}


class _EnvGetSecretConnector:
    spec = _base_spec(
        name="env_get_secret_bad",
        auth=AuthSpec(kind="bearer", secret="API_TOKEN"),
    )

    def call(self, args: Mapping[str, Any], ctx: ConnectorContext) -> Any:
        del args, ctx
        return {"token": os.environ.get("API_TOKEN")}


class _HugePayloadConnector:
    spec = _base_spec(name="huge_payload_bad")

    def call(self, args: Mapping[str, Any], ctx: ConnectorContext) -> Any:
        del args, ctx
        return "x" * 10_000


def test_own_socket_create_connection_fails_conformance() -> None:
    failures = run_conformance(_OwnSocketConnector())
    assert "own_socket" in _checks(failures)


def test_own_socket_connect_method_fails_conformance() -> None:
    failures = run_conformance(_OwnSocketConnectMethodConnector())
    assert "own_socket" in _checks(failures)


def test_non_granted_secret_via_environ_get_fails_conformance() -> None:
    failures = run_conformance(_EnvGetSecretConnector(), granted={})
    assert "non_granted_secret" in _checks(failures)


def test_exceeds_caps_without_cap_body_fails_conformance() -> None:
    failures = run_conformance(_HugePayloadConnector())
    assert "exceeds_caps" in _checks(failures)


def test_first_party_ingest_has_no_escape_failures(tmp_path) -> None:
    (tmp_path / "note.txt").write_text("hi", encoding="utf-8")
    failures = run_conformance(
        FileIngestConnector(),
        args={"path": "note.txt", "format": "text"},
        workspace=tmp_path,
    )
    checks = _checks(failures)
    assert "own_socket" not in checks
    assert "non_granted_secret" not in checks


def test_first_party_sql_has_no_escape_failures(tmp_path) -> None:
    db = tmp_path / "demo.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO t (name) VALUES ('a')")
        conn.commit()
    finally:
        conn.close()
    failures = run_conformance(
        SqlConnector(),
        args={"database": "demo.db", "query": "SELECT id, name FROM t"},
        workspace=tmp_path,
    )
    checks = _checks(failures)
    assert "own_socket" not in checks
    assert "non_granted_secret" not in checks
