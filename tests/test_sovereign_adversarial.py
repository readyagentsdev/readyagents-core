"""Adversarial escape suite for TASK-04 sovereign egress. Drive shipped APIs only."""

from __future__ import annotations

import socket
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import Settings, clear_settings_cache
from readyagents.errors import EgressDenied, LLMError
from readyagents.llm.registry import get_provider
from readyagents.sovereign.egress import install_guard
from readyagents.tools import FunctionTool, ToolRegistry
from readyagents.workflow.runner import run_workflow_file

runner = CliRunner()


def _stub_orig_connect(eg) -> None:
    def _ok(self, address, *args, **kwargs):  # noqa: ANN001, ANN002
        return None

    eg._orig_connect = _ok


def _stub_orig_create(eg) -> None:
    def _ok(*args, **kwargs):  # noqa: ANN002
        raise OSError("stubbed connect")

    eg._orig_create = _ok


# --- 1. Pack / extra tool create_connection under sovereign=True ---


def test_pack_tool_create_connection_denied_failed_run(tmp_settings) -> None:
    def poke() -> str:
        socket.create_connection(("1.1.1.1", 80), timeout=0.2)
        return "reached"

    extra = ToolRegistry()
    extra.register(FunctionTool(name="poke", description="escape", handler=poke, schema={}))
    path = tmp_settings.workspace_path() / "adv_poke.yaml"
    path.write_text(
        """
name: adv_poke
start: escape_node
nodes:
  - id: escape_node
    type: tool
    tool: poke
""",
        encoding="utf-8",
    )
    with pytest.raises(EgressDenied) as denied:
        run_workflow_file(
            path, settings=tmp_settings, persist=True, sovereign=True, extra_tools=extra
        )
    exc = denied.value
    assert "1.1.1.1" in str(exc)
    assert "1.1.1.1" in exc.destination
    assert exc.node_id == "escape_node"
    state = exc.state
    assert state is not None
    assert state.status == "failed"
    assert state.status != "succeeded"


# --- 2. Thread escape while guard installed ---


def test_thread_create_connection_raises_egress_denied() -> None:
    guard = install_guard()
    caught: list[BaseException] = []

    def _worker() -> None:
        try:
            socket.create_connection(("1.1.1.1", 443), timeout=0.2)
        except BaseException as extra:  # noqa: BLE001
            caught.append(extra)

    try:
        thread = threading.Thread(target=_worker)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert caught, "thread completed without an exception"
        assert isinstance(caught[0], EgressDenied)
        assert "1.1.1.1" in str(caught[0])
    finally:
        guard.close()


# --- 3. Provider-style socket().connect ---


def test_provider_style_socket_connect_refused() -> None:
    guard = install_guard()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(EgressDenied) as denied:
                sock.connect(("8.8.8.8", 53))
            assert "8.8.8.8" in str(denied.value)
            assert "8.8.8.8" in denied.value.destination
        finally:
            sock.close()
    finally:
        guard.close()


# --- 4. Redirect: private allowlist then public connect ---


def test_redirect_public_after_private_allowlist_refused() -> None:
    guard = install_guard(["10.0.0.8"])
    try:
        with pytest.raises(EgressDenied) as denied:
            socket.create_connection(("8.8.8.8", 53), timeout=0.2)
        assert "8.8.8.8" in str(denied.value)
    finally:
        guard.close()


# --- 5. DNS: public allow spec / hostname resolving public ---


def test_dns_public_literal_allow_spec_refused() -> None:
    with pytest.raises(EgressDenied) as denied:
        install_guard(["8.8.8.8"])
    assert "8.8.8.8" in str(denied.value)


def test_dns_hostname_resolving_public_refused_at_install() -> None:
    def fake_gai(host, port, *args, **kwargs):  # noqa: ANN001, ANN002
        if str(host).rstrip(".").lower() == "evil.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]
        raise OSError("unexpected host")

    with patch("socket.getaddrinfo", side_effect=fake_gai):
        with pytest.raises(EgressDenied) as denied:
            install_guard(["evil.example"])
    assert "evil.example" in str(denied.value)
    assert "8.8.8.8" in str(denied.value)


def test_dns_allowlist_toctou_public_flip_refused() -> None:
    """Hostname private at install must not stay trusted after DNS flips public."""
    state = {"mode": "private"}

    def flip_gai(host, port, *args, **kwargs):  # noqa: ANN001, ANN002
        if str(host).rstrip(".").lower() != "flip.example":
            raise OSError("unexpected")
        ip = "10.0.0.9" if state["mode"] == "private" else "1.1.1.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    with patch("socket.getaddrinfo", side_effect=flip_gai):
        guard = install_guard(["flip.example"])
        try:
            from readyagents.sovereign import egress as eg

            _stub_orig_create(eg)
            state["mode"] = "public"
            with pytest.raises(EgressDenied) as denied:
                socket.create_connection(("flip.example", 80), timeout=0.1)
            assert "1.1.1.1" in str(denied.value) or "flip.example" in str(denied.value)
        finally:
            guard.close()


# --- 6. After guard.close(), EgressDenied is not raised ---


def test_after_close_egress_denied_not_raised() -> None:
    guard = install_guard()
    guard.close()
    from readyagents.sovereign.egress import active_guard

    assert active_guard() is None
    reached = {"ok": False}

    def stub(*_a, **_k):  # noqa: ANN002
        reached["ok"] = True
        raise OSError("refused")

    orig = socket.create_connection
    socket.create_connection = stub  # type: ignore[assignment]
    try:
        with pytest.raises(OSError, match="refused"):
            socket.create_connection(("1.1.1.1", 80), timeout=0.1)
        assert reached["ok"] is True
    finally:
        socket.create_connection = orig


# --- 7. Loopback allowed (stub orig connect) ---


def test_loopback_allowed_with_stubbed_connect() -> None:
    guard = install_guard()
    try:
        from readyagents.sovereign import egress as eg

        _stub_orig_connect(eg)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.connect(("127.0.0.1", 9))
        finally:
            sock.close()
        assert any(
            item.get("allowed") and item.get("reason") == "loopback" for item in guard.attempts
        )
    finally:
        guard.close()


# --- 8. Remote openai-compat still requires API key (+ keyless local) ---


def test_get_provider_keyless_local_vs_remote_key() -> None:
    local = Settings(  # type: ignore[call-arg]
        openai_api_key=None,
        anthropic_api_key=None,
        openai_compat_api_key=None,
        openai_compat_base_url=None,
        _env_file=(),
    )
    provider, model = get_provider("ollama:llama3.2", settings=local)
    assert provider.name == "openai-compat"
    assert model == "llama3.2"
    assert getattr(provider, "_base_url", "").startswith("http://127.0.0.1:11434")

    remote = Settings(  # type: ignore[call-arg]
        openai_api_key=None,
        anthropic_api_key=None,
        openai_compat_api_key=None,
        openai_compat_base_url="https://api.groq.com/openai/v1",
        _env_file=(),
    )
    with pytest.raises(LLMError, match="API key"):
        get_provider("openai-compat:llama-3.1-8b-instant", settings=remote)


def test_cli_sovereign_allow_public_ip_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    wf = tmp_path / "t.yaml"
    wf.write_text(
        """
name: t
start: n
nodes:
  - id: n
    type: transform
    template: "ok"
    output_key: summary
""",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        ["run", str(wf), "--sovereign", "--sovereign-allow", "8.8.8.8", "--json"],
    )
    assert result.exit_code != 0
    blob = (result.stdout + result.stderr).lower()
    assert "egress" in blob or "8.8.8.8" in blob or "denied" in blob
    clear_settings_cache()
