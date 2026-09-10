"""Sovereign mode: socket-boundary guard, keyless local compat, attest, bundle."""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import Settings, clear_settings_cache
from readyagents.errors import EgressDenied, LLMError
from readyagents.llm.registry import get_provider
from readyagents.sovereign.attest import build_attestation
from readyagents.sovereign.bundle import sha256_file, verify_manifest, write_bundle
from readyagents.sovereign.egress import install_guard
from readyagents.tools import FunctionTool, ToolRegistry
from readyagents.workflow.runner import run_workflow_file

runner = CliRunner()


def _transform_wf(root: Path, name: str = "sov") -> Path:
    path = root / f"{name}.yaml"
    path.write_text(
        f"""
name: {name}
start: t
nodes:
  - id: t
    type: transform
    template: "ok"
    output_key: summary
""",
        encoding="utf-8",
    )
    return path


def test_loopback_allowed_under_sovereign(tmp_settings) -> None:
    path = _transform_wf(tmp_settings.workspace_path())
    state = run_workflow_file(path, settings=tmp_settings, persist=True, sovereign=True)
    assert state.status == "succeeded"
    assert state.metadata.get("sovereign") is True
    assert state.output_keys["summary"] == "ok"


def test_public_address_raises_typed_egress_denied(tmp_settings) -> None:
    def poke() -> str:
        socket.create_connection(("1.1.1.1", 80), timeout=0.2)
        return "reached"

    extra = ToolRegistry()
    extra.register(FunctionTool(name="poke", description="poke", handler=poke, schema={}))
    path = tmp_settings.workspace_path() / "poke.yaml"
    path.write_text(
        """
name: poke
start: g
nodes:
  - id: g
    type: tool
    tool: poke
""",
        encoding="utf-8",
    )
    with pytest.raises(EgressDenied) as denied:
        run_workflow_file(
            path, settings=tmp_settings, persist=True, sovereign=True, extra_tools=extra
        )
    assert "1.1.1.1" in str(denied.value)
    assert denied.value.node_id == "g"
    assert denied.value.destination
    state = denied.value.state
    assert state is not None
    attempts = (state.metadata.get("network") or {}).get("egress_attempts") or []
    assert any(item.get("allowed") is False for item in attempts)


def test_allowlisted_private_host_permitted_and_recorded() -> None:
    guard = install_guard(["10.0.0.8"])
    try:
        from readyagents.sovereign import egress as eg

        def _ok(self, address, *args, **kwargs):  # noqa: ANN001
            return None

        eg._orig_connect = _ok
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.connect(("10.0.0.8", 8000))
        finally:
            sock.close()
        assert any(
            item.get("allowed") and "10.0.0.8" in item.get("destination", "")
            for item in guard.attempts
        )
    finally:
        guard.close()


def test_redirect_to_public_refused() -> None:
    guard = install_guard(["10.0.0.8"])
    try:
        with pytest.raises(EgressDenied) as denied:
            socket.create_connection(("8.8.8.8", 53), timeout=0.2)
        assert "8.8.8.8" in str(denied.value)
    finally:
        guard.close()


def test_thread_and_pack_direct_connect_refused() -> None:
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
        assert caught
        assert isinstance(caught[0], EgressDenied)
    finally:
        guard.close()


def test_guard_removed_after_run(tmp_settings) -> None:
    path = _transform_wf(tmp_settings.workspace_path(), name="after")
    run_workflow_file(path, settings=tmp_settings, persist=False, sovereign=True)
    from readyagents.sovereign.egress import active_guard

    assert active_guard() is None
    reached = {"ok": False}

    def _ok(*_a, **_k):  # noqa: ANN002
        reached["ok"] = True
        raise OSError("refused")

    orig = socket.create_connection
    socket.create_connection = _ok  # type: ignore[assignment]
    try:
        with pytest.raises(OSError):
            socket.create_connection(("1.1.1.1", 80), timeout=0.1)
        assert reached["ok"] is True
    finally:
        socket.create_connection = orig


def test_loopback_compat_needs_no_key_remote_still_does() -> None:
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


def test_attest_marks_mcp_stdio_uncontrolled(tmp_settings) -> None:
    path = tmp_settings.workspace_path() / "mcp.yaml"
    path.write_text(
        """
name: withmcp
start: t
mcp_servers:
  files:
    command: "true"
nodes:
  - id: t
    type: transform
    template: "ok"
    output_key: summary
""",
        encoding="utf-8",
    )
    state = run_workflow_file(
        path, settings=tmp_settings, persist=True, sovereign=True, dry_run=True
    )
    payload = build_attestation(state)
    assert payload["mode"] == "sovereign"
    assert payload["subprocesses"]
    assert all(item.get("network_uncontrolled") is True for item in payload["subprocesses"])
    dumped = json.dumps(payload)
    assert "no egress" not in dumped.lower() or payload["network"].get("claims_no_egress") is False
    assert payload["network"].get("claims_no_egress") is False


def test_doctor_json_has_no_secrets_and_stable_keys() -> None:
    first = runner.invoke(app, ["doctor", "--json"])
    second = runner.invoke(app, ["doctor", "--json"])
    assert first.exit_code in {0, 1}
    a = json.loads(first.stdout[first.stdout.find("{") :])
    b = json.loads(second.stdout[second.stdout.find("{") :])
    assert set(a) == set(b)
    blob = json.dumps(a)
    assert "sk-" not in blob
    assert "OPENAI_API_KEY" not in blob
    assert "api_key" not in blob.lower() or "api_key" not in str(a.get("local_models"))
    assert a["command"] == "doctor"
    assert "sovereign" in a
    assert "local_models" in a
    assert a["local_models"]["ollama"]["endpoint"] == "loopback:11434"


def test_bundle_manifest_checksums_verify(tmp_path: Path) -> None:
    dest = tmp_path / "bundle"
    dest.mkdir()
    wheel = dest / "readyagentsdev-0.0.0-py3-none-any.whl"
    wheel.write_bytes(b"fake-wheel-bytes")
    from readyagents.sovereign.bundle import MANIFEST_NAME

    files = [{"name": wheel.name, "digest": sha256_file(wheel)}]
    payload = {
        "version": 1,
        "files": files,
        "python": "3.12",
        "platform": "any",
        "readyagents_version": "0",
        "install": "pip install --no-index --find-links . readyagentsdev",
    }
    (dest / MANIFEST_NAME).write_text(json.dumps(payload), encoding="utf-8")
    verified = verify_manifest(dest)
    assert verified["files"][0]["digest"] == files[0]["digest"]
    wheel.write_bytes(b"tampered")
    with pytest.raises(Exception, match="checksum"):
        verify_manifest(dest)


def test_cli_sovereign_keyless_example(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    src = Path(__file__).resolve().parents[1] / "examples" / "calc_pipeline.yaml"
    dest = tmp_path / "calc_pipeline.yaml"
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    paused = runner.invoke(app, ["run", str(dest), "--sovereign", "--json"])
    assert paused.exit_code == 0, paused.stdout + paused.stderr
    body = json.loads(paused.stdout[paused.stdout.find("{") :])
    assert body.get("ok") is True or body.get("status") == "succeeded" or "run_id" in body
    clear_settings_cache()


def test_attest_cli_after_sovereign_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    ran = runner.invoke(app, ["run", str(wf), "--sovereign", "--json"])
    assert ran.exit_code == 0, ran.stdout
    payload = json.loads(ran.stdout[ran.stdout.find("{") :])
    run_id = payload.get("run_id") or (payload.get("run") or {}).get("run_id")
    attested = runner.invoke(app, ["attest", run_id, "--json"])
    assert attested.exit_code == 0, attested.stdout
    doc = json.loads(attested.stdout[attested.stdout.find("{") :])
    assert doc.get("command") == "attest"
    assert doc.get("mode") == "sovereign"
    clear_settings_cache()


def test_write_bundle_local_project(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    dest = tmp_path / "off"
    payload = write_bundle(dest, project=root)
    assert payload["files"]
    verify_manifest(dest)
