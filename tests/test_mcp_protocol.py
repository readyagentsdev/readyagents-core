from __future__ import annotations

import pytest

from readyagents.errors import HeaderMismatchError, UnsupportedProtocolVersionError
from readyagents.mcp.protocol import (
    JSONRPC_UNSUPPORTED_PROTOCOL,
    LATEST_PROTOCOL_VERSION,
    LEGACY_PROTOCOL_VERSION,
    META_CLIENT_CAPABILITIES,
    META_CLIENT_INFO,
    META_PROTOCOL_VERSION,
    TASKS_EXTENSION,
    RequestContext,
    error_from_exception,
    honoured_protocol_versions,
    parse_trace_context,
    require_transport_headers,
    sanitize_prompt,
    sdk_capability,
)


def test_from_meta_valid_stateless() -> None:
    ctx = RequestContext.from_meta(
        {
            META_PROTOCOL_VERSION: LATEST_PROTOCOL_VERSION,
            META_CLIENT_CAPABILITIES: {"extensions": {TASKS_EXTENSION: {}}},
            META_CLIENT_INFO: {"name": "t", "version": "1"},
        },
        honoured=("2026-07-28", "2025-11-25", "2025-06-18"),
    )
    assert ctx.protocol_version == LATEST_PROTOCOL_VERSION
    assert ctx.is_stateless
    assert ctx.supports(TASKS_EXTENSION)
    assert ctx.client_info == {"name": "t", "version": "1"}


def test_from_meta_missing_version_is_legacy() -> None:
    ctx = RequestContext.from_meta(
        {},
        honoured=("2026-07-28", "2025-11-25", "2025-06-18"),
    )
    assert ctx.protocol_version == LEGACY_PROTOCOL_VERSION
    assert not ctx.is_stateless


def test_from_meta_unknown_keys_ignored() -> None:
    ctx = RequestContext.from_meta(
        {META_PROTOCOL_VERSION: "2025-06-18", "totally-unknown": 1},
        honoured=("2026-07-28", "2025-11-25", "2025-06-18"),
    )
    assert ctx.protocol_version == "2025-06-18"


def test_from_meta_wrong_type_version() -> None:
    with pytest.raises(ValueError, match="must be a string"):
        RequestContext.from_meta(
            {META_PROTOCOL_VERSION: 2026},
            honoured=("2026-07-28",),
        )


def test_from_meta_out_of_range_is_minus_32022() -> None:
    with pytest.raises(UnsupportedProtocolVersionError) as caught:
        RequestContext.from_meta(
            {META_PROTOCOL_VERSION: "1900-01-01"},
            honoured=("2026-07-28", "2025-11-25"),
        )
    assert caught.value.requested == "1900-01-01"
    assert "2026-07-28" in caught.value.supported
    body = error_from_exception(1, caught.value)
    assert body["error"]["code"] == JSONRPC_UNSUPPORTED_PROTOCOL
    assert body["error"]["data"]["supported"] == ["2026-07-28", "2025-11-25"]
    assert body["error"]["data"]["requested"] == "1900-01-01"


def test_trace_context_valid_and_malformed_dropped() -> None:
    parent = "00-" + ("a" * 32) + "-" + ("b" * 16) + "-01"
    got = parse_trace_context(
        {
            "traceparent": parent,
            "tracestate": "congo=t61rcWkgMzE",
            "baggage": "userId=alice",
        }
    )
    assert got["traceparent"] == parent
    assert got["tracestate"] == "congo=t61rcWkgMzE"
    assert got["baggage"] == "userId=alice"
    dropped = parse_trace_context(
        {
            "traceparent": "not-a-trace",
            "tracestate": "x" * 600,
            "baggage": "has\nnewline",
        }
    )
    assert dropped == {}


def test_header_mismatch_2026_only() -> None:
    require_transport_headers(
        method="tools/list",
        params={},
        mcp_method=None,
        mcp_name=None,
        protocol_version="2025-11-25",
    )
    with pytest.raises(HeaderMismatchError):
        require_transport_headers(
            method="tools/list",
            params={},
            mcp_method=None,
            mcp_name=None,
            protocol_version=LATEST_PROTOCOL_VERSION,
        )
    with pytest.raises(HeaderMismatchError):
        require_transport_headers(
            method="tools/call",
            params={"name": "calc"},
            mcp_method="tools/call",
            mcp_name="now",
            protocol_version=LATEST_PROTOCOL_VERSION,
        )


def test_sanitize_prompt_strips_controls_and_bounds() -> None:
    class _Redactor:
        def redact_text(self, text: str) -> str:
            return text.replace("secret", "[redacted]")

    out = sanitize_prompt("hello\x00secret\x07" + ("z" * 5000), redactor=_Redactor(), limit=20)
    assert "\x00" not in out
    assert "secret" not in out
    assert len(out) <= 20


def test_sdk_capability_reports_installed_pin() -> None:
    cap = sdk_capability()
    assert cap["package"] == "mcp"
    assert cap["tier"] in {"full", "legacy", "unknown", "absent"}
    honoured = honoured_protocol_versions()
    if cap["tier"] == "full":
        assert LATEST_PROTOCOL_VERSION in honoured
    elif cap["tier"] == "legacy":
        assert LATEST_PROTOCOL_VERSION not in honoured
        assert "2025-11-25" in honoured or "2025-06-18" in honoured
