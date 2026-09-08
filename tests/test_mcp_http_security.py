from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from readyagents.config import DEFAULT_MCP_TOKEN_ENV
from readyagents.errors import ConfigError, ReadyAgentsError
from readyagents.mcp.server import mcp_available
from readyagents.tools import FunctionTool, ToolRegistry
from readyagents.workflow.runner import confine_under
from readyagents.workflow.state import load_run

pytestmark = pytest.mark.skipif(not mcp_available(), reason="mcp extra not installed")

_TOKEN = "tok_" + ("Z" * 40)
_WRONG_TOKEN = "tok_" + ("Y" * 40)
_BIND_HOST = "127.0.0.1"
_BIND_PORT = 8765
_BASE_URL = f"http://{_BIND_HOST}:{_BIND_PORT}"
_DUMMY_ID = "a" * 32
_MAX_BODY = 1_048_576
_OK_WORKFLOW = """
name: ok
nodes:
  - id: t
    type: transform
    template: "ok"
    output_key: summary
"""
_BLOCK_WORKFLOW = """
name: block-run
start: wait
nodes:
  - id: wait
    type: tool
    tool: block
    output_key: blocked
"""
_SURFACES: tuple[tuple[str, str, dict | None], ...] = (
    ("POST", "/mcp", {}),
    ("GET", "/mcp", None),
    ("POST", "/runs", {"path": "ok.yaml"}),
    ("GET", f"/runs/{_DUMMY_ID}", None),
    ("POST", f"/runs/{_DUMMY_ID}/decide", {"node_id": "gate", "decision": "approve"}),
    ("POST", f"/runs/{_DUMMY_ID}/cancel", {"actor": "tester"}),
)


def _http_apis():
    from readyagents.mcp.http import assert_loopback_host, compose_http_app, resolve_bearer_token
    from readyagents.mcp.run_api import RunCoordinator
    from readyagents.mcp.server import construct_server

    return (
        compose_http_app,
        construct_server,
        RunCoordinator,
        assert_loopback_host,
        resolve_bearer_token,
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _run_files(settings) -> list[Path]:
    runs = settings.runs_dir()
    if not runs.is_dir():
        return []
    return sorted(p for p in runs.glob("*.json") if not p.name.startswith("."))


def _write_ok_workflow(tmp_path: Path) -> Path:
    path = tmp_path / "ok.yaml"
    path.write_text(_OK_WORKFLOW, encoding="utf-8")
    return path


def _close_coordinator(coordinator: object) -> None:
    for name in ("shutdown", "close", "stop"):
        fn = getattr(coordinator, name, None)
        if not callable(fn):
            continue
        try:
            fn()
        except TypeError:
            fn(wait=False)  # type: ignore[misc]
        return


def _make_coordinator(tmp_settings, tmp_path: Path, **opts):
    _compose, _construct, run_coordinator, _assert_loop, _resolve = _http_apis()
    kwargs = dict(opts)
    kwargs.setdefault("settings", tmp_settings)
    try:
        import inspect

        params = inspect.signature(run_coordinator).parameters
        if "workspace" in params:
            kwargs.setdefault("workspace", tmp_path)
    except (TypeError, ValueError):
        pass
    return run_coordinator(**kwargs)


def _compose_app(tmp_path: Path, coordinator, *, token: str = _TOKEN):
    compose_http_app, construct_server, _rc, _assert_loop, _resolve = _http_apis()
    return compose_http_app(
        server=construct_server(workspace=tmp_path),
        coordinator=coordinator,
        token=token,
        bind_host=_BIND_HOST,
        bind_port=_BIND_PORT,
    )


@contextmanager
def _http_client(
    tmp_path: Path,
    tmp_settings,
    *,
    token: str = _TOKEN,
    extra_tools: ToolRegistry | None = None,
    **coord_kwargs,
) -> Iterator[tuple[object, object]]:
    from starlette.testclient import TestClient

    if extra_tools is not None:
        coord_kwargs["extra_tools"] = extra_tools
    coordinator = _make_coordinator(tmp_settings, tmp_path, **coord_kwargs)
    app = _compose_app(tmp_path, coordinator, token=token)
    try:
        with TestClient(app, base_url=_BASE_URL) as client:
            yield client, coordinator
    finally:
        _close_coordinator(coordinator)


def _request(
    client, method: str, path: str, *, token: str | None = None, json_body=None, headers=None
):
    hdrs = dict(headers or {})
    if token is not None:
        hdrs["Authorization"] = f"Bearer {token}"
    kwargs: dict = {"headers": hdrs}
    if json_body is not None:
        kwargs["json"] = json_body
    return client.request(method, path, **kwargs)


def _assert_401_bearer(resp, *, where: str) -> None:
    assert not (200 <= resp.status_code < 300), f"{where}: unexpected 2xx {resp.status_code}"
    assert resp.status_code == 401, (
        f"{where}: expected 401, got {resp.status_code} {resp.text[:400]}"
    )
    www = resp.headers.get("www-authenticate", "")
    assert www.strip().startswith("Bearer"), f"{where}: WWW-Authenticate={www!r}"


def _assert_no_new_runs(settings, before: set[str], *, where: str) -> None:
    after = {p.name for p in _run_files(settings)}
    assert after == before, f"{where}: new run files {after - before}"


def _poll_run(client, run_id: str, *, token: str, wanted: set[str], timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = client.get(f"/runs/{run_id}", headers=_bearer(token))
        if last.status_code == 200:
            status = str(last.json().get("status") or "")
            if status in wanted:
                return last
        time.sleep(0.05)
    body = last.text[:500] if last is not None else "<no response>"
    code = last.status_code if last is not None else None
    raise AssertionError(f"run {run_id} did not reach {wanted}: last={code} {body}")


def _call_resolve_bearer_token(*, auth: str, bind_host: str):
    _compose, _construct, _rc, _assert_loop, resolve_bearer_token = _http_apis()
    return resolve_bearer_token(
        auth_mode=auth,
        token_env=DEFAULT_MCP_TOKEN_ENV,
        bind_host=bind_host,
    )


def _call_assert_loopback_host(host: str):
    _compose, _construct, _rc, assert_loopback_host, _resolve = _http_apis()
    try:
        return assert_loopback_host(host)
    except TypeError:
        return assert_loopback_host(bind_host=host)


def _coordinator_start(coordinator, *, path: str, inputs: dict | None = None):
    return coordinator.start_run({"path": path, "inputs": inputs or {}})


def _response_blob(resp) -> str:
    parts = [str(resp.status_code), resp.text]
    for key, value in resp.headers.items():
        parts.append(f"{key}: {value}")
    return "\n".join(parts)


def test_import_readyagents_without_optional_mcp_http() -> None:
    import readyagents

    assert readyagents.__version__
    # This file must not `import mcp` at module level (only mcp_available() may).
    assert "mcp" not in globals()


def test_forged_or_missing_authorization_returns_401_bearer(tmp_path: Path, tmp_settings) -> None:
    _write_ok_workflow(tmp_path)
    with _http_client(tmp_path, tmp_settings, token=_TOKEN) as (client, _coord):
        for method, path, body in _SURFACES:
            missing = _request(client, method, path, json_body=body)
            _assert_401_bearer(missing, where=f"missing {method} {path}")
            assert _TOKEN not in _response_blob(missing)
            forged = _request(client, method, path, token=_WRONG_TOKEN, json_body=body)
            _assert_401_bearer(forged, where=f"forged {method} {path}")
            assert _TOKEN not in _response_blob(forged)
            assert _WRONG_TOKEN not in _response_blob(forged)


def test_bearer_token_bytes_never_appear_in_responses_or_logs(
    tmp_path: Path, tmp_settings, caplog: pytest.LogCaptureFixture
) -> None:
    _write_ok_workflow(tmp_path)
    logger = logging.getLogger("readyagents")
    captured: list[str] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(self.format(record))
            captured.append(str(record.__dict__))

    handler = _Handler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="readyagents")
    try:
        with _http_client(tmp_path, tmp_settings, token=_TOKEN) as (client, _coord):
            responses = [
                _request(client, "POST", "/runs", json_body={"path": "ok.yaml"}),
                _request(client, "POST", "/runs", token=_TOKEN, json_body={"path": "ok.yaml"}),
                _request(client, "GET", f"/runs/{_DUMMY_ID}", token=_TOKEN),
                _request(client, "POST", "/mcp", token=_TOKEN, json_body={}),
                _request(
                    client,
                    "POST",
                    f"/runs/{_DUMMY_ID}/decide",
                    token=_WRONG_TOKEN,
                    json_body={"node_id": "gate", "decision": "approve"},
                ),
                _request(
                    client,
                    "POST",
                    f"/runs/{_DUMMY_ID}/cancel",
                    token=_TOKEN,
                    json_body={"actor": "tester"},
                ),
            ]
            created = responses[1]
            if created.status_code == 202:
                run_id = created.json().get("run_id")
                if run_id:
                    responses.append(_request(client, "GET", f"/runs/{run_id}", token=_TOKEN))
        blob = "\n".join(captured) + "\n" + caplog.text
        for resp in responses:
            blob += "\n" + _response_blob(resp)
            assert _TOKEN not in _response_blob(resp)
        assert _TOKEN not in blob
        assert _TOKEN not in caplog.text
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def test_dns_rebinding_host_and_origin_rejected_before_run_created(
    tmp_path: Path, tmp_settings
) -> None:
    _write_ok_workflow(tmp_path)
    with _http_client(tmp_path, tmp_settings, token=_TOKEN) as (client, coordinator):
        before = {p.name for p in _run_files(tmp_settings)}
        attacks = [
            {"Host": "evil.example"},
            {"Origin": "http://evil.example"},
            {"Host": "evil.example", "Origin": "http://evil.example"},
            {"Host": "evil.example:8765", "Origin": "http://evil.example"},
        ]
        for extra in attacks:
            resp = client.post(
                "/runs",
                json={"path": "ok.yaml"},
                headers={**_bearer(_TOKEN), **extra},
            )
            assert not (200 <= resp.status_code < 300), (
                f"rebinding {extra} returned {resp.status_code} {resp.text[:300]}"
            )
            _assert_no_new_runs(tmp_settings, before, where=f"rebinding {extra}")
        # Absolute URL also plants Host: evil.example on the ASGI scope.
        resp = client.post(
            "http://evil.example:8765/runs",
            json={"path": "ok.yaml"},
            headers=_bearer(_TOKEN),
        )
        assert not (200 <= resp.status_code < 300)
        _assert_no_new_runs(tmp_settings, before, where="absolute evil Host")
        # Coordinator start would create a file; Host check must run first.
        assert hasattr(coordinator, "start_run")


def test_path_traversal_rejected_http_400_no_run_file(tmp_path: Path, tmp_settings) -> None:
    _write_ok_workflow(tmp_path)
    root = tmp_path.resolve()
    for raw in ("/etc/passwd", "../escape.yaml", "foo\x00bar.yaml"):
        with pytest.raises(ConfigError):
            confine_under(raw, root, what="workflow")

    outside = tmp_path.parent / f"escape-{tmp_path.name}.yaml"
    outside.write_text(_OK_WORKFLOW, encoding="utf-8")
    link = tmp_path / "leak.yaml"
    symlink_ok = True
    try:
        link.symlink_to(outside)
    except OSError:
        symlink_ok = False
    if symlink_ok:
        with pytest.raises(ConfigError):
            confine_under("leak.yaml", root, what="workflow")
        with pytest.raises(ConfigError):
            confine_under(link, root, what="workflow")

    bad_paths = ["/etc/passwd", "../escape.yaml", "foo\x00bar.yaml"]
    if symlink_ok:
        bad_paths.append("leak.yaml")

    with _http_client(tmp_path, tmp_settings, token=_TOKEN) as (client, coordinator):
        before = {p.name for p in _run_files(tmp_settings)}
        for raw in bad_paths:
            resp = _request(
                client,
                "POST",
                "/runs",
                token=_TOKEN,
                json_body={"path": raw},
            )
            assert not (200 <= resp.status_code < 300), (
                f"{raw}: {resp.status_code} {resp.text[:300]}"
            )
            assert resp.status_code == 400, f"{raw}: expected 400, got {resp.status_code}"
            _assert_no_new_runs(tmp_settings, before, where=f"POST /runs path={raw!r}")
            with pytest.raises(ReadyAgentsError):
                _coordinator_start(coordinator, path=raw)
            _assert_no_new_runs(tmp_settings, before, where=f"start_run path={raw!r}")


def test_oversized_body_rejected_without_run_file(tmp_path: Path, tmp_settings) -> None:
    _write_ok_workflow(tmp_path)
    oversize = json.dumps({"path": "ok.yaml", "inputs": {"blob": "x" * (_MAX_BODY + 64)}}).encode(
        "utf-8"
    )
    assert len(oversize) > _MAX_BODY
    with _http_client(tmp_path, tmp_settings, token=_TOKEN) as (client, _coord):
        before = {p.name for p in _run_files(tmp_settings)}
        headers = {
            **_bearer(_TOKEN),
            "Content-Type": "application/json",
            "Content-Length": str(len(oversize)),
        }
        resp = client.post("/runs", content=oversize, headers=headers)
        assert not (200 <= resp.status_code < 300), resp.text[:300]
        assert resp.status_code in {413, 400}, f"expected 413 or 400, got {resp.status_code}"
        _assert_no_new_runs(tmp_settings, before, where="oversized JSON body")

        huge = b"x" * (_MAX_BODY + 1)
        resp = client.post(
            "/runs",
            content=huge,
            headers={
                **_bearer(_TOKEN),
                "Content-Type": "application/json",
                "Content-Length": str(len(huge)),
            },
        )
        assert not (200 <= resp.status_code < 300)
        assert resp.status_code in {413, 400}
        _assert_no_new_runs(tmp_settings, before, where="oversized raw body")


def test_queue_overflow_returns_429_without_second_run_file(tmp_path: Path, tmp_settings) -> None:
    (tmp_path / "block.yaml").write_text(_BLOCK_WORKFLOW, encoding="utf-8")
    started = threading.Event()
    release = threading.Event()

    def block() -> str:
        started.set()
        if not release.wait(timeout=30):
            raise TimeoutError("blocker was not released")
        return "done"

    extra = ToolRegistry()
    extra.register(FunctionTool(name="block", description="block until released", handler=block))
    with _http_client(
        tmp_path,
        tmp_settings,
        token=_TOKEN,
        extra_tools=extra,
        max_pending_runs=1,
        max_concurrent_runs=1,
    ) as (client, _coord):
        try:
            first = _request(
                client, "POST", "/runs", token=_TOKEN, json_body={"path": "block.yaml"}
            )
            assert first.status_code == 202, first.text[:400]
            started.wait(timeout=5)
            before_second = {p.name for p in _run_files(tmp_settings)}
            assert before_second, "first POST /runs must persist a run file"
            second = _request(
                client, "POST", "/runs", token=_TOKEN, json_body={"path": "block.yaml"}
            )
            assert second.status_code == 429, (
                f"expected 429, got {second.status_code} {second.text[:400]}"
            )
            assert not (200 <= second.status_code < 300)
            after = {p.name for p in _run_files(tmp_settings)}
            assert after == before_second
            assert len(after) == 1
        finally:
            release.set()


def test_concurrent_decide_one_202_one_409_then_branch_once(
    tmp_path: Path, tmp_settings, examples_dir: Path
) -> None:
    wf = tmp_path / "approval_gate.yaml"
    wf.write_text(
        (examples_dir / "approval_gate.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    with _http_client(tmp_path, tmp_settings, token=_TOKEN) as (client, _coord):
        created = _request(
            client, "POST", "/runs", token=_TOKEN, json_body={"path": "approval_gate.yaml"}
        )
        assert created.status_code == 202, created.text[:400]
        run_id = created.json()["run_id"]
        paused = _poll_run(client, run_id, token=_TOKEN, wanted={"paused"})
        assert paused.json().get("pending_node") == "gate"

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def _decide():
            barrier.wait(timeout=10)
            return _request(
                client,
                "POST",
                f"/runs/{run_id}/decide",
                token=_TOKEN,
                json_body={"node_id": "gate", "decision": "approve", "actor": "reviewer"},
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_decide) for _ in range(2)]
            responses = []
            for fut in futures:
                try:
                    responses.append(fut.result(timeout=15))
                except Exception as exc:
                    errors.append(exc)
        assert not errors, errors
        codes = sorted(resp.status_code for resp in responses)
        assert codes == [202, 409], [f"{r.status_code}:{r.text[:200]}" for r in responses]
        assert not any(200 <= r.status_code < 300 and r.status_code != 202 for r in responses)

        done = _poll_run(client, run_id, token=_TOKEN, wanted={"succeeded"})
        payload = done.json()
        outputs = payload.get("outputs") or payload.get("output_keys") or {}
        summary = str(outputs.get("summary") or "")
        assert "approval_gate ok" in summary
        assert "approval_gate denied" not in summary

        state = load_run(tmp_settings.runs_dir(), run_id)
        receipts = [row for row in state.results if row.node_id == "receipt"]
        denied = [row for row in state.results if row.node_id == "denied"]
        assert len(receipts) == 1, [row.node_id for row in state.results]
        assert denied == []
        assert "approval_gate ok" in str(state.output_keys.get("summary"))


def test_prefix_run_ids_refused_even_when_unique(tmp_path: Path, tmp_settings) -> None:
    _write_ok_workflow(tmp_path)
    with _http_client(tmp_path, tmp_settings, token=_TOKEN) as (client, _coord):
        created = _request(client, "POST", "/runs", token=_TOKEN, json_body={"path": "ok.yaml"})
        assert created.status_code == 202, created.text[:400]
        run_id = str(created.json()["run_id"])
        assert len(run_id) >= 16
        prefix = run_id[:12]
        assert prefix != run_id
        loaded = load_run(tmp_settings.runs_dir(), prefix)
        assert loaded.run_id == run_id

        for method, path, body in (
            ("GET", f"/runs/{prefix}", None),
            ("POST", f"/runs/{prefix}/decide", {"node_id": "gate", "decision": "approve"}),
            ("POST", f"/runs/{prefix}/cancel", {"actor": "tester"}),
        ):
            resp = _request(client, method, path, token=_TOKEN, json_body=body)
            assert not (200 <= resp.status_code < 300), f"{method} {path}: {resp.status_code}"
            assert resp.status_code == 400, (
                f"{method} {path}: expected 400, got {resp.status_code} {resp.text[:300]}"
            )


def test_auth_none_rejected_for_non_loopback() -> None:
    _call_assert_loopback_host("127.0.0.1")
    for host in ("0.0.0.0", "8.8.8.8", "192.168.1.10", "example.com", "1.2.3.4"):
        with pytest.raises(ReadyAgentsError):
            _call_assert_loopback_host(host)
        with pytest.raises(ReadyAgentsError):
            _call_resolve_bearer_token(auth="none", bind_host=host)
