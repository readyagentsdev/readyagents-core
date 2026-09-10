"""SDK helpers: Retry-After, pagination cap, size cap. Drive shipped functions."""

from __future__ import annotations

from pathlib import Path

import pytest

from readyagents.connectors.context import ConnectorContext
from readyagents.connectors.fixtures import FixtureStore
from readyagents.connectors.sdk import iter_cursor_pages, parse_retry_after
from readyagents.connectors.spec import ConnectorSpec, RateLimitSpec
from readyagents.errors import ConnectorCapError, ToolError


def _spec() -> ConnectorSpec:
    return ConnectorSpec(
        name="rest",
        version="1.0.0",
        description="t",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        destinations=("api.example.test",),
        side_effects="read",
        rate_limit=RateLimitSpec(requests=8, window_seconds=1.0),
    )


def test_parse_retry_after_seconds() -> None:
    assert parse_retry_after("2") == 2.0
    assert parse_retry_after("0.5") == 0.5
    assert parse_retry_after(None) is None


def test_cursor_pagination_hits_page_cap() -> None:
    calls = {"n": 0}

    def fetch(cursor):  # noqa: ANN001
        calls["n"] += 1
        return {"next_cursor": str(calls["n"]), "items": [calls["n"]]}

    with pytest.raises(ConnectorCapError, match="page"):
        list(iter_cursor_pages(fetch, max_pages=3))
    assert calls["n"] == 3


def test_http_fixture_and_size_cap(tmp_path: Path) -> None:
    dest = tmp_path / "fx"
    dest.mkdir()
    (dest / "ok.json").write_text(
        '{"method":"GET","url":"https://api.example.test/x","status":200,"body":{"ok":true}}',
        encoding="utf-8",
    )
    ctx = ConnectorContext(_spec(), workspace=tmp_path, fixtures=FixtureStore(dest))
    resp = ctx.http("GET", "https://api.example.test/x")
    assert resp.json()["ok"] is True
    (dest / "huge.json").write_text(
        '{"method":"GET","url":"https://api.example.test/huge","status":200,"body":"'
        + ("x" * 200)
        + '"}',
        encoding="utf-8",
    )
    ctx.fixtures = FixtureStore(dest)
    ctx.max_bytes = 50
    with pytest.raises(ConnectorCapError):
        ctx.http("GET", "https://api.example.test/huge")


def test_undeclared_host_refused(tmp_path: Path) -> None:
    ctx = ConnectorContext(_spec(), workspace=tmp_path, fixtures=FixtureStore(tmp_path))
    with pytest.raises(ToolError, match="not declared"):
        ctx.http("GET", "https://evil.example/x")


def test_connector_http_refuses_loopback(tmp_path: Path) -> None:
    spec = ConnectorSpec(
        name="rest",
        version="1.0.0",
        description="t",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        destinations=("127.0.0.1",),
        side_effects="read",
    )
    ctx = ConnectorContext(spec, workspace=tmp_path)
    with pytest.raises(ToolError, match="not allowed"):
        ctx.http("GET", "http://127.0.0.1/")


def test_connector_http_refuses_redirect_to_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = ConnectorSpec(
        name="rest",
        version="1.0.0",
        description="t",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        destinations=("api.example.test", "127.0.0.1"),
        side_effects="read",
    )
    ctx = ConnectorContext(spec, workspace=tmp_path)

    def fake_resolve(host: str, *, kind: str = "http_get") -> list[str]:
        if host in {"127.0.0.1", "localhost"}:
            from readyagents.mcp.builtin import _resolve_public_ips

            return _resolve_public_ips(host, kind=kind)
        return ["203.0.113.1"]

    def fake_exchange(scheme, hostname, ip, port, path, **kwargs):  # noqa: ANN001
        if hostname in {"127.0.0.1", "localhost"}:
            raise AssertionError("must not connect to loopback")
        return 302, b"", {"Location": "http://127.0.0.1/secret"}

    monkeypatch.setattr("readyagents.connectors.sdk._resolve_public_ips", fake_resolve)
    monkeypatch.setattr("readyagents.connectors.sdk._http_exchange", fake_exchange)
    with pytest.raises(ToolError, match="not allowed"):
        ctx.http("GET", "https://api.example.test/x")
