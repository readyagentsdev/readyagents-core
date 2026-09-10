"""HTTP run coordinator: start, poll, decide, and cancel workflow runs.

Request-driven. Persistence cannot be disabled through this API. HTTP never
uses run-id prefix lookup — only a full 32-char hex id is accepted.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

from readyagents.config import Settings, get_settings
from readyagents.errors import (
    ApprovalRequired,
    AuthorizationError,
    ConfigError,
    MCPError,
    ReadyAgentsError,
    WorkflowError,
)
from readyagents.logging import get_logger
from readyagents.packs.loader import collect_pack_authorizers, discover_packs
from readyagents.policy import redactor_from_settings, resolve_authorizer
from readyagents.tools import ToolRegistry
from readyagents.workflow.runner import (
    confine_under,
    load_workflow,
    merge_inputs,
    resume_run,
    run_workflow_file,
)
from readyagents.workflow.state import RunState, persist_run, utc_now

log = get_logger("run_api")

_RUN_ID_RE = re.compile(r"[0-9a-f]{32}")
_MAX_JSON_DEPTH = 32
_MAX_REASON_CHARS = 512
_START_FIELDS = frozenset({"path", "inputs", "actor", "dry_run"})
_DECIDE_FIELDS = frozenset({"node_id", "decision", "actor"})
_CANCEL_FIELDS = frozenset({"actor", "reason"})
_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
_RATE_LIMIT = 120
_RATE_WINDOW = 60.0
_APPROVE = "approve"
_REJECT = "reject"


class _OneShotDecisions(dict):
    """MCP-originated decisions apply to one occurrence, not every later loop."""

    def get(self, key, default=None):  # noqa: ANN001
        if key in self:
            return self.pop(key)
        return default


def _default_max_body() -> int:
    try:
        from readyagents.config import MAX_HTTP_BODY_BYTES

        return int(MAX_HTTP_BODY_BYTES)
    except Exception:  # noqa: BLE001
        return 1_048_576


def _error_type(name: str, fallback: type[ReadyAgentsError]) -> type[ReadyAgentsError]:
    try:
        from readyagents import errors as errmod

        found = getattr(errmod, name, None)
        if isinstance(found, type) and issubclass(found, BaseException):
            return found
    except Exception:  # noqa: BLE001
        pass
    return fallback


class _HttpRequestError(ReadyAgentsError):
    """Malformed or oversized HTTP request."""


class _HttpAuthError(ReadyAgentsError):
    """HTTP authentication failed."""


class _RunConflict(ReadyAgentsError):
    """Idempotency mismatch, stale decide, or concurrent resume."""


class _CancellationRequested(ReadyAgentsError):
    """A run was cancelled at a safe point."""


class QueueOverflow(ReadyAgentsError):
    """Too many queued or running runs to accept another start."""


class ServiceUnavailable(ReadyAgentsError):
    """Coordinator is shutting down."""


HttpRequestError = _error_type("HttpRequestError", _HttpRequestError)
HttpAuthError = _error_type("HttpAuthError", _HttpAuthError)
RunConflict = _error_type("RunConflict", _RunConflict)
CancellationRequested = _error_type("CancellationRequested", _CancellationRequested)


def _cancellation_token_cls() -> type:
    try:
        from readyagents.workflow.cancellation import CancellationToken as imported

        return imported
    except ImportError:
        return _CancellationToken


class _CancellationToken:
    """Fallback token used until ``workflow.cancellation`` is merged."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requested = False
        self.actor: str | None = None
        self.reason: str | None = None
        self.requested_at: str | None = None

    def request(self, actor: str | None = None, reason: str | None = None) -> None:
        with self._lock:
            if self._requested:
                return
            self._requested = True
            self.actor = actor
            self.reason = reason
            self.requested_at = utc_now()

    def is_requested(self) -> bool:
        with self._lock:
            return self._requested


CancellationToken = _cancellation_token_cls()


def load_run_exact(runs_dir: Path, run_id: str) -> RunState:
    """Load ``{runs_dir}/{run_id}.json``. Full 32-char hex id only — no prefix."""
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise HttpRequestError(f"Invalid run id: {run_id}")
    path = Path(runs_dir) / f"{run_id}.json"
    if not path.is_file():
        exc = ConfigError(f"Run not found: {run_id}")
        exc.run_id = run_id
        raise exc
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Corrupt run record {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read run record {path}: {exc}") from exc
    if not isinstance(data, dict) or not data.get("run_id"):
        raise ConfigError(f"Invalid run record: {path}")
    return RunState.from_record(data)


def _links(run_id: str) -> dict[str, str]:
    return {
        "self": f"/runs/{run_id}",
        "decide": f"/runs/{run_id}/decide",
        "cancel": f"/runs/{run_id}/cancel",
    }


def _canonical_body(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _body_hash(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _check_depth(value: Any, depth: int = 1) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise HttpRequestError(f"JSON nesting exceeds {_MAX_JSON_DEPTH}")
    if isinstance(value, dict):
        for item in value.values():
            _check_depth(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_depth(item, depth + 1)


def _unknown_fields(payload: Mapping[str, Any], allowed: frozenset[str], *, what: str) -> None:
    extra = sorted(str(key) for key in payload if key not in allowed)
    if extra:
        raise HttpRequestError(f"Unknown field(s) in {what}: {', '.join(extra)}")


def _require_object(payload: Any, *, what: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HttpRequestError(f"{what} must be a JSON object")
    return payload


def _is_json_content_type(value: str | None) -> bool:
    if not value:
        return False
    return value.split(";", 1)[0].strip().lower() == "application/json"


def _http_err(message: str, status_code: int = 400) -> HttpRequestError:
    exc = HttpRequestError(message)
    exc.status_code = status_code  # type: ignore[attr-defined]
    return exc


def _call_supported(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Invoke ``fn`` dropping kwargs the current signature does not accept."""
    params = inspect.signature(fn).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(*args, **kwargs)
    names = set(params)
    if "initial_state" in kwargs and "initial_state" not in names and "resume_state" in names:
        if kwargs.get("resume_state") is None:
            kwargs["resume_state"] = kwargs["initial_state"]
    filtered = {key: value for key, value in kwargs.items() if key in names}
    return fn(*args, **filtered)


def _envelope(
    exc: BaseException,
    *,
    request_id: str | None = None,
    run_id: str | None = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    status = _status_for(exc)
    error_name = type(exc).__name__
    if error_name.startswith("_"):
        error_name = error_name[1:]
    if isinstance(exc, QueueOverflow):
        error_name = "HttpRequestError"
        status = 429
    elif isinstance(exc, ServiceUnavailable):
        error_name = "MCPError"
        status = 503
    message = str(exc) if isinstance(exc, ReadyAgentsError) else "internal error"
    body: dict[str, Any] = {"ok": False, "error": error_name, "message": message}
    rid = run_id or getattr(exc, "run_id", None)
    if rid:
        body["run_id"] = rid
    if request_id:
        body["request_id"] = request_id
    headers = {"Cache-Control": "no-store"}
    if status == 429:
        headers["Retry-After"] = "1"
    return status, headers, body


def _status_for(exc: BaseException) -> int:
    code = getattr(exc, "status_code", None)
    if isinstance(code, int) and 400 <= code < 600:
        return code
    if isinstance(exc, AuthorizationError):
        return 403
    if isinstance(exc, HttpAuthError):
        return 401
    if isinstance(exc, RunConflict):
        return 409
    if isinstance(exc, QueueOverflow):
        return 429
    if isinstance(exc, ServiceUnavailable):
        return 503
    if isinstance(exc, HttpRequestError):
        return 400
    if isinstance(exc, ConfigError):
        text = str(exc).lower()
        if "not found" in text and "workflow" not in text:
            return 404
        return 400
    if isinstance(exc, (WorkflowError, CancellationRequested)):
        return 400
    if isinstance(exc, MCPError):
        return 503
    if isinstance(exc, ReadyAgentsError):
        return 400
    return 500


class _CancellingTool:
    """Wrap a tool so a requested cancellation becomes ``CancellationRequested``."""

    def __init__(self, inner: Any, token: Any) -> None:
        self._inner = inner
        self._token = token
        self.name = inner.name
        self.description = getattr(inner, "description", inner.name)
        self.schema = getattr(inner, "schema", {}) or {}

    def run(self, **kwargs: Any) -> Any:
        self._raise_if_cancelled()
        try:
            return self._inner.run(**kwargs)
        finally:
            self._raise_if_cancelled()

    def _raise_if_cancelled(self) -> None:
        if self._token is not None and self._token.is_requested():
            raise CancellationRequested("run cancelled")


class _RateLimiter:
    def __init__(self, limit: int = _RATE_LIMIT, window: float = _RATE_WINDOW) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            while bucket and now - bucket[0] > self.window:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(now)
            return True


class RunCoordinator:
    """In-process run queue, idempotency, and HTTP handlers for ``/runs``."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        workspace: Path | str | None = None,
        max_concurrent_runs: int = 4,
        max_pending_runs: int = 32,
        extra_tools: ToolRegistry | None = None,
        extra_packs: Sequence[Any] | None = None,
        max_body_bytes: int = 1_048_576,
    ) -> None:
        bound = settings or get_settings()
        root = workspace if workspace is not None else bound.workspace_path()
        self.workspace = Path(root).expanduser().resolve()
        self.settings = bound.model_copy(update={"workspace": self.workspace})
        self.max_concurrent_runs = max(1, int(max_concurrent_runs))
        self.max_pending_runs = max(0, int(max_pending_runs))
        self.max_body_bytes = int(max_body_bytes) if max_body_bytes else _default_max_body()
        self._extra_tools = extra_tools
        self._extra_packs = list(extra_packs) if extra_packs else []
        packs = list(discover_packs())
        packs.extend(self._extra_packs)
        self._authorizer = resolve_authorizer(collect_pack_authorizers(packs))
        self._redactor = redactor_from_settings(
            enabled=bool(self.settings.redact),
            patterns=self.settings.redact_pattern_list(),
            literals=self.settings.redact_literal_list(),
        )
        self._runs_dir = self.settings.runs_dir()
        self._lock = threading.RLock()
        self._run_locks: dict[str, threading.RLock] = {}
        self._tokens: dict[str, Any] = {}
        self._in_flight_resume: set[str] = set()
        self._active: set[str] = set()
        self._queued = 0
        self._running = 0
        self._idempotency: dict[str, tuple[str, dict[str, Any]]] = {}
        self._shutdown = False
        self._rate = _RateLimiter()
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_concurrent_runs,
            thread_name_prefix="readyagents-run",
        )
        self._store = None
        self._persist_delay_s = 0.0
        try:
            from readyagents.run_store import open_run_store

            self._store = open_run_store(self.settings)
        except ImportError:
            self._store = None

    def attach_store(self, store: Any) -> None:
        self._store = store

    def _load_exact(self, run_id: str) -> RunState:
        if self._store is None:
            return load_run_exact(self._runs_dir, run_id)
        if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
            raise HttpRequestError(f"Invalid run id: {run_id}")
        return self._store.get(run_id, allow_prefix=False).state

    def shutdown(self, timeout: float = 10.0) -> None:
        self._shutdown = True
        with self._lock:
            tokens = list(self._tokens.values())
        for token in tokens:
            try:
                token.request(actor="system", reason="shutdown")
            except Exception:  # noqa: BLE001
                pass
        self._executor.shutdown(wait=False, cancel_futures=True)
        closer = getattr(self._store, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            with self._lock:
                if self._running <= 0 and not self._active:
                    break
            time.sleep(0.05)

    def start_run(self, payload: dict, *, idempotency_key: str | None = None) -> dict:
        """Validate, persist a queued run, and submit it. Raises typed errors."""
        return self._start(payload, idempotency_key=idempotency_key)

    def get_run(self, run_id: str) -> dict:
        state = self._load_exact(run_id)
        return self._record_payload(state)

    def decide(self, run_id: str, payload: dict, *, input_request_key: str | None = None) -> dict:
        return self._decide(run_id, payload, input_request_key=input_request_key)

    def cancel(self, run_id: str, payload: dict | None) -> dict:
        return self._cancel(run_id, payload)

    def submit_run(
        self,
        body: dict,
        *,
        idempotency_key: str | None,
        actor_default: str | None = None,
    ) -> tuple[int, dict]:
        status, _headers, payload = self.handle_start(
            body,
            idempotency_key=idempotency_key,
            actor_default=actor_default,
        )
        return status, payload

    def handle_start(
        self,
        payload: Any,
        *,
        idempotency_key: str | None = None,
        actor_default: str | None = None,
        request_id: str | None = None,
        raw_len: int | None = None,
    ) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            if raw_len is not None and raw_len > self.max_body_bytes:
                raise _http_err("request body too large", 413)
            body = self._start(
                payload,
                idempotency_key=idempotency_key,
                actor_default=actor_default,
            )
            if request_id:
                body = dict(body)
                body["request_id"] = request_id
            run_id = str(body["run_id"])
            headers = {
                "Location": f"/runs/{run_id}",
                "Cache-Control": "no-store",
            }
            return 202, headers, body
        except Exception as exc:  # noqa: BLE001
            return self._caught(exc, request_id=request_id)

    def handle_get(
        self,
        run_id: str,
        *,
        request_id: str | None = None,
    ) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            body = self.get_run(run_id)
            if request_id:
                body = dict(body)
                body["request_id"] = request_id
            return 200, {"Cache-Control": "no-store"}, body
        except Exception as exc:  # noqa: BLE001
            return self._caught(exc, request_id=request_id, run_id=run_id)

    def handle_decide(
        self,
        run_id: str,
        payload: Any,
        *,
        request_id: str | None = None,
        raw_len: int | None = None,
        input_request_key: str | None = None,
    ) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            if raw_len is not None and raw_len > self.max_body_bytes:
                raise _http_err("request body too large", 413)
            body = self._decide(run_id, payload, input_request_key=input_request_key)
            if request_id:
                body = dict(body)
                body["request_id"] = request_id
            return 202, {"Cache-Control": "no-store"}, body
        except Exception as exc:  # noqa: BLE001
            return self._caught(exc, request_id=request_id, run_id=run_id)

    def handle_cancel(
        self,
        run_id: str,
        payload: Any,
        *,
        request_id: str | None = None,
        raw_len: int | None = None,
    ) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            if raw_len is not None and raw_len > self.max_body_bytes:
                raise _http_err("request body too large", 413)
            body = self._cancel(run_id, payload)
            if request_id:
                body = dict(body)
                body["request_id"] = request_id
            status = 200 if body.get("status") in _TERMINAL else 202
            return status, {"Cache-Control": "no-store"}, body
        except Exception as exc:  # noqa: BLE001
            return self._caught(exc, request_id=request_id, run_id=run_id)

    def check_rate(self, client_host: str | None) -> None:
        key = client_host or "unknown"
        if not self._rate.allow(key):
            raise _http_err("rate limit exceeded", 429)

    def _caught(
        self,
        exc: BaseException,
        *,
        request_id: str | None = None,
        run_id: str | None = None,
    ) -> tuple[int, dict[str, str], dict[str, Any]]:
        if not isinstance(exc, ReadyAgentsError):
            log.exception("unhandled run API error")
            body: dict[str, Any] = {
                "ok": False,
                "error": "MCPError",
                "message": "internal error",
            }
            if run_id:
                body["run_id"] = run_id
            if request_id:
                body["request_id"] = request_id
            return 500, {"Cache-Control": "no-store"}, body
        return _envelope(exc, request_id=request_id, run_id=run_id)

    def _record_payload(self, state: RunState) -> dict[str, Any]:
        body = dict(state.to_record())
        body["ok"] = True
        body["links"] = _links(state.run_id)
        body["deprecated"] = True
        body["successor"] = "tasks/*"
        return body

    def _run_lock(self, run_id: str) -> threading.RLock:
        with self._lock:
            lock = self._run_locks.get(run_id)
            if lock is None:
                lock = threading.RLock()
                self._run_locks[run_id] = lock
            return lock

    def _token_for(self, run_id: str) -> Any:
        with self._lock:
            token = self._tokens.get(run_id)
            if token is None:
                token = CancellationToken()
                self._tokens[run_id] = token
            return token

    def _new_token(self, run_id: str) -> Any:
        token = CancellationToken()
        with self._lock:
            self._tokens[run_id] = token
        return token

    def _release_idempotency(self, key: str) -> None:
        with self._lock:
            previous = self._idempotency.pop(key, None)
        if previous is not None and previous[0] == "pending" and previous[2] is not None:
            previous[2].set()

    def _persist(self, state: RunState) -> None:
        delay = float(self._persist_delay_s or 0.0)
        if delay > 0:
            time.sleep(delay)
        if self._store is not None:
            self._store.save(state, redactor=self._redactor)
            return
        persist_run(state, self._runs_dir, redactor=self._redactor)

    def _wrap_tools(self, token: Any) -> ToolRegistry | None:
        if self._extra_tools is None:
            return None
        wrapped = ToolRegistry()
        for tool in self._extra_tools.as_dict().values():
            wrapped.register(_CancellingTool(tool, token))
        return wrapped

    def _start(
        self,
        payload: Any,
        *,
        idempotency_key: str | None,
        actor_default: str | None = None,
    ) -> dict[str, Any]:
        body = _require_object(payload, what="request body")
        _check_depth(body)
        _unknown_fields(body, _START_FIELDS, what="start request")
        path_raw = body.get("path")
        if not isinstance(path_raw, str) or not path_raw.strip():
            raise HttpRequestError("path is required and must be a string")
        inputs = body.get("inputs", {})
        if not isinstance(inputs, dict):
            raise HttpRequestError("inputs must be a JSON object")
        actor = body.get("actor")
        if actor is not None and not isinstance(actor, str):
            raise HttpRequestError("actor must be a string")
        if "dry_run" in body and not isinstance(body["dry_run"], bool):
            raise HttpRequestError("dry_run must be a boolean")
        dry_run = bool(body.get("dry_run", False))
        if actor is None:
            actor = actor_default if actor_default is not None else self.settings.actor

        key = (idempotency_key or "").strip() or None
        canonical = _canonical_body(body)
        digest = _body_hash(canonical)
        pending_event: threading.Event | None = None
        if key:
            while True:
                waiter: threading.Event | None = None
                with self._lock:
                    previous = self._idempotency.get(key)
                    if previous is None:
                        pending_event = threading.Event()
                        self._idempotency[key] = ("pending", digest, pending_event, None)
                        break
                    kind = previous[0]
                    prev_hash = previous[1]
                    if prev_hash != digest:
                        raise RunConflict(
                            "Idempotency-Key was reused with a different request body"
                        )
                    if kind == "ready":
                        handle = previous[3]
                        assert handle is not None
                        return dict(handle)
                    waiter = previous[2]
                if waiter is None:
                    continue
                waiter.wait(timeout=30.0)

        with self._lock:
            if self._shutdown:
                if key:
                    self._release_idempotency(key)
                raise ServiceUnavailable("run API is shutting down")

        try:
            try:
                wf_path = confine_under(path_raw, self.workspace, what="workflow")
            except ConfigError as exc:
                raise HttpRequestError(str(exc)) from exc
            if not wf_path.is_file():
                raise ConfigError(f"Workflow file not found: {wf_path}")

            workflow = load_workflow(wf_path)
            merged = merge_inputs(workflow, inputs)
            self._authorizer.check(actor, "run", workflow.name)

            declared = (workflow.workspace or "").strip()
            try:
                run_workspace = (
                    confine_under(declared, self.workspace, what="workspace")
                    if declared
                    else self.workspace
                )
            except ConfigError as exc:
                raise HttpRequestError(str(exc)) from exc
            allow_http = bool(workflow.allow_http or self.settings.allow_http)

            with self._lock:
                if self._shutdown:
                    raise ServiceUnavailable("run API is shutting down")
                if self._queued + self._running >= self.max_pending_runs:
                    raise QueueOverflow("too many pending runs")
                self._queued += 1
        except Exception:
            if key:
                self._release_idempotency(key)
            raise

        run_id = uuid4().hex
        state = RunState.start(
            workflow.name,
            merged,
            metadata={
                "source": str(wf_path),
                "workspace": str(run_workspace),
                "actor": actor,
                "dry_run": dry_run,
                "allow_http": allow_http,
                "submitted_via": "http",
            },
            run_id=run_id,
        )
        state.status = "queued"
        token = self._new_token(run_id)
        try:
            self._persist(state)
        except Exception:
            with self._lock:
                self._queued = max(0, self._queued - 1)
            if key:
                self._release_idempotency(key)
            raise

        handle = {
            "ok": True,
            "run_id": run_id,
            "status": "queued",
            "links": _links(run_id),
            "deprecated": True,
            "successor": "tasks/*",
        }
        if key:
            with self._lock:
                self._idempotency[key] = ("ready", digest, None, dict(handle))
            if pending_event is not None:
                pending_event.set()
        with self._lock:
            self._active.add(run_id)
        try:
            self._executor.submit(
                self._run_job,
                run_id,
                wf_path,
                merged,
                dry_run,
                actor,
                token,
            )
        except Exception:
            with self._lock:
                self._queued = max(0, self._queued - 1)
                self._active.discard(run_id)
            if key:
                self._release_idempotency(key)
            state.status = "failed"
            state.errors.append("failed to submit run")
            state.finish("failed")
            self._persist(state)
            raise
        return handle

    def _run_job(
        self,
        run_id: str,
        path: Path,
        inputs: dict[str, Any],
        dry_run: bool,
        actor: str | None,
        token: Any,
    ) -> None:
        with self._lock:
            self._queued = max(0, self._queued - 1)
            self._running += 1
        paused = False
        try:
            if token.is_requested():
                self._finish_cancelled(run_id)
                return
            try:
                state = self._load_exact(run_id)
            except ReadyAgentsError:
                return
            if state.status in _TERMINAL:
                return
            if state.status == "cancel_requested":
                self._finish_cancelled(run_id)
                return
            wrapped = self._wrap_tools(token)
            _call_supported(
                run_workflow_file,
                path,
                inputs=inputs,
                dry_run=dry_run,
                settings=self.settings,
                persist=True,
                extra_tools=wrapped,
                extra_packs=self._extra_packs or None,
                actor=actor,
                authorizer=self._authorizer,
                cancellation=token,
                initial_state=state,
                store=self._store,
            )
        except ApprovalRequired:
            paused = True
        except CancellationRequested:
            self._finish_cancelled(run_id)
        except ReadyAgentsError:
            if token.is_requested():
                self._finish_cancelled(run_id)
        except Exception:
            log.exception("run %s worker crashed", run_id)
            if token.is_requested():
                self._finish_cancelled(run_id)
            else:
                self._mark_failed(run_id, "internal error")
        else:
            if token.is_requested() and not paused:
                self._finish_cancelled(run_id)
        finally:
            with self._lock:
                self._running = max(0, self._running - 1)
                self._active.discard(run_id)

    def _decide(
        self, run_id: str, payload: Any, *, input_request_key: str | None = None
    ) -> dict[str, Any]:
        if self._shutdown:
            raise ServiceUnavailable("run API is shutting down")
        self._load_exact(run_id)
        body = _require_object(payload, what="request body")
        _check_depth(body)
        _unknown_fields(body, _DECIDE_FIELDS, what="decide request")
        node_id = body.get("node_id")
        decision = body.get("decision")
        if not isinstance(node_id, str) or not node_id.strip():
            raise HttpRequestError("node_id is required and must be a string")
        if decision not in {_APPROVE, _REJECT}:
            raise HttpRequestError('decision must be exactly "approve" or "reject"')
        actor = body.get("actor")
        if actor is not None and not isinstance(actor, str):
            raise HttpRequestError("actor must be a string")
        actor = actor if actor is not None else self.settings.actor
        node_id = node_id.strip()

        with self._run_lock(run_id):
            state = self._load_exact(run_id)
            if state.status != "paused" or state.pending_node != node_id:
                raise RunConflict(
                    f"Run {run_id} is not paused at node '{node_id}' "
                    f"(status={state.status}, pending_node={state.pending_node})"
                )
            if input_request_key:
                bucket = state.metadata.get("mcp_input_responses")
                applied = bucket.get(input_request_key) if isinstance(bucket, dict) else None
                if applied == decision:
                    with self._lock:
                        in_flight = run_id in self._in_flight_resume
                    if in_flight or state.status != "paused":
                        return {
                            "ok": True,
                            "run_id": run_id,
                            "status": state.status,
                            "links": _links(run_id),
                        }
                elif applied is not None:
                    raise RunConflict("conflicting input response for this input request key")
            try:
                self._authorizer.check(actor, "resume", run_id)
                self._authorizer.check(actor, decision, node_id)
            except Exception:
                raise
            with self._lock:
                if run_id in self._in_flight_resume:
                    raise RunConflict(f"Run {run_id} already has a resume in flight")
                self._in_flight_resume.add(run_id)
            if input_request_key:
                meta = dict(state.metadata)
                responses = dict(meta.get("mcp_input_responses") or {})
                responses[input_request_key] = decision
                meta["mcp_input_responses"] = responses
                meta["mcp_consume_decisions"] = True
                state.metadata = meta
                try:
                    self._persist(state)
                except Exception as extra:  # noqa: BLE001
                    from readyagents.errors import RunStoreConflict

                    with self._lock:
                        self._in_flight_resume.discard(run_id)
                    if isinstance(extra, RunStoreConflict):
                        raise RunConflict(
                            "conflicting input response for this input request key"
                        ) from extra
                    raise
            token = self._new_token(run_id)
            with self._lock:
                self._active.add(run_id)
            raw_decisions = {node_id: decision}
            if isinstance(state.pending, dict):
                inner = state.pending.get("node_id")
                if isinstance(inner, str) and inner.strip() and inner.strip() != node_id:
                    raw_decisions[inner.strip()] = decision
            decisions: dict[str, str] = (
                _OneShotDecisions(raw_decisions) if input_request_key else raw_decisions
            )
            try:
                self._executor.submit(
                    self._resume_job,
                    run_id,
                    decisions,
                    actor,
                    token,
                )
            except Exception:
                with self._lock:
                    self._in_flight_resume.discard(run_id)
                    self._active.discard(run_id)
                raise
        return {
            "ok": True,
            "run_id": run_id,
            "status": "running",
            "links": _links(run_id),
        }

    def _resume_job(
        self,
        run_id: str,
        decisions: dict[str, str],
        actor: str | None,
        token: Any,
    ) -> None:
        paused = False
        try:
            if token.is_requested():
                self._finish_cancelled(run_id)
                return
            wrapped = self._wrap_tools(token)
            _call_supported(
                resume_run,
                run_id,
                settings=self.settings,
                persist=True,
                extra_tools=wrapped,
                extra_packs=self._extra_packs or None,
                decisions=decisions,
                actor=actor,
                authorizer=self._authorizer,
                cancellation=token,
                store=self._store,
            )
        except ApprovalRequired:
            paused = True
        except CancellationRequested:
            self._finish_cancelled(run_id)
        except ReadyAgentsError:
            if token.is_requested():
                self._finish_cancelled(run_id)
        except Exception:
            log.exception("resume %s worker crashed", run_id)
            if token.is_requested():
                self._finish_cancelled(run_id)
        else:
            if token.is_requested() and not paused:
                self._finish_cancelled(run_id)
        finally:
            with self._lock:
                self._in_flight_resume.discard(run_id)
                self._active.discard(run_id)

    def _cancel(self, run_id: str, payload: Any) -> dict[str, Any]:
        self._load_exact(run_id)
        if payload is None:
            body: dict[str, Any] = {}
        else:
            body = _require_object(payload, what="request body")
        _check_depth(body)
        _unknown_fields(body, _CANCEL_FIELDS, what="cancel request")
        actor = body.get("actor")
        if actor is not None and not isinstance(actor, str):
            raise HttpRequestError("actor must be a string")
        reason = body.get("reason")
        if reason is not None:
            if not isinstance(reason, str):
                raise HttpRequestError("reason must be a string")
            if len(reason) > _MAX_REASON_CHARS:
                raise HttpRequestError(f"reason must be at most {_MAX_REASON_CHARS} characters")
            if self._redactor is not None:
                reason = self._redactor.redact_text(reason)
        actor = actor if actor is not None else self.settings.actor

        with self._run_lock(run_id):
            state = self._load_exact(run_id)
            if state.status in _TERMINAL:
                return self._record_payload(state)
            token = self._token_for(run_id)
            token.request(actor=actor, reason=reason)
            state.status = "cancel_requested"
            self._persist(state)
            with self._lock:
                active = run_id in self._active
            if not active:
                self._finish_cancelled(run_id)
                return self._record_payload(self._load_exact(run_id))
            snapshot = self._record_payload(state)
            snapshot["status"] = "cancel_requested"
            return snapshot

    def _finish_cancelled(self, run_id: str) -> None:
        with self._run_lock(run_id):
            try:
                state = self._load_exact(run_id)
            except ReadyAgentsError:
                return
            if state.status in {"succeeded", "cancelled"}:
                return
            state.finish("cancelled")
            try:
                self._persist(state)
            except Exception:  # noqa: BLE001
                log.exception("failed to persist cancelled run %s", run_id)

    def _mark_failed(self, run_id: str, message: str) -> None:
        with self._run_lock(run_id):
            try:
                state = self._load_exact(run_id)
            except ReadyAgentsError:
                return
            if state.status in _TERMINAL:
                return
            state.errors.append(message)
            state.finish("failed")
            try:
                self._persist(state)
            except Exception:  # noqa: BLE001
                log.exception("failed to persist failed run %s", run_id)


def build_run_routes(coordinator: RunCoordinator) -> list[Any]:
    """Starlette routes for POST/GET /runs and decide/cancel."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def _read_json(request: Request) -> tuple[Any, int]:
        raw = await request.body()
        if len(raw) > coordinator.max_body_bytes:
            raise _http_err("request body too large", 413)
        if not raw:
            return {}, len(raw)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HttpRequestError(f"Malformed JSON: {exc}") from exc
        return parsed, len(raw)

    def _request_id(request: Request) -> str | None:
        return request.headers.get("x-request-id")

    def _host(request: Request) -> str | None:
        client = request.client
        return client.host if client is not None else None

    def _respond(status: int, headers: dict[str, str], body: dict[str, Any]) -> JSONResponse:
        return JSONResponse(body, status_code=status, headers=headers)

    async def post_runs(request: Request) -> JSONResponse:
        request_id = _request_id(request)
        try:
            coordinator.check_rate(_host(request))
            if not _is_json_content_type(request.headers.get("content-type")):
                raise _http_err("Content-Type must be application/json", 415)
            payload, raw_len = await _read_json(request)
            key = request.headers.get("idempotency-key")
            return _respond(
                *coordinator.handle_start(
                    payload,
                    idempotency_key=key,
                    request_id=request_id,
                    raw_len=raw_len,
                )
            )
        except ReadyAgentsError as exc:
            return _respond(*coordinator._caught(exc, request_id=request_id))

    async def get_run(request: Request) -> JSONResponse:
        request_id = _request_id(request)
        run_id = request.path_params.get("run_id", "")
        try:
            coordinator.check_rate(_host(request))
            return _respond(*coordinator.handle_get(run_id, request_id=request_id))
        except ReadyAgentsError as exc:
            return _respond(*coordinator._caught(exc, request_id=request_id, run_id=run_id))

    async def post_decide(request: Request) -> JSONResponse:
        request_id = _request_id(request)
        run_id = request.path_params.get("run_id", "")
        try:
            coordinator.check_rate(_host(request))
            if not _is_json_content_type(request.headers.get("content-type")):
                raise _http_err("Content-Type must be application/json", 415)
            payload, raw_len = await _read_json(request)
            return _respond(
                *coordinator.handle_decide(
                    run_id,
                    payload,
                    request_id=request_id,
                    raw_len=raw_len,
                )
            )
        except ReadyAgentsError as exc:
            return _respond(*coordinator._caught(exc, request_id=request_id, run_id=run_id))

    async def post_cancel(request: Request) -> JSONResponse:
        request_id = _request_id(request)
        run_id = request.path_params.get("run_id", "")
        try:
            coordinator.check_rate(_host(request))
            if not _is_json_content_type(request.headers.get("content-type")):
                raise _http_err("Content-Type must be application/json", 415)
            payload, raw_len = await _read_json(request)
            return _respond(
                *coordinator.handle_cancel(
                    run_id,
                    payload,
                    request_id=request_id,
                    raw_len=raw_len,
                )
            )
        except ReadyAgentsError as exc:
            return _respond(*coordinator._caught(exc, request_id=request_id, run_id=run_id))

    return [
        Route("/runs", post_runs, methods=["POST"]),
        Route("/runs/{run_id}", get_run, methods=["GET"]),
        Route("/runs/{run_id}/decide", post_decide, methods=["POST"]),
        Route("/runs/{run_id}/cancel", post_cancel, methods=["POST"]),
    ]


__all__ = [
    "CancellationRequested",
    "CancellationToken",
    "HttpAuthError",
    "HttpRequestError",
    "QueueOverflow",
    "RunConflict",
    "RunCoordinator",
    "ServiceUnavailable",
    "build_run_routes",
    "load_run_exact",
]
