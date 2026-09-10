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
