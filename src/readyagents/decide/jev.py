"""HTTP client for TypeSafe System One (Jev). Stdlib sockets so egress sees the connect."""

from __future__ import annotations

import ipaddress
import json
import socket
import ssl
import time
from collections.abc import Callable, Mapping
from contextvars import ContextVar, Token
from typing import Any

from readyagents import __version__
from readyagents.decide.base import validate_questions
from readyagents.decide.types import Answer, DecideState, Decision, Question, QuestionType
from readyagents.errors import DecideError, ToolError
from readyagents.logging import get_logger

log = get_logger("decide.jev")

PINNED_JEV_MODEL = "jev-1.13.0"
JEV_ALIASES = frozenset({"jev-latest", "jev-preview"})
DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_PATH = "/v1/systemone"
MAX_BODY_BYTES = 10 * 1024 * 1024
ERROR_BODY_CAP = 512
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504, 529})

JevTransport = Callable[..., tuple[int, bytes, dict[str, str]]]
_transport: ContextVar[JevTransport | None] = ContextVar("readyagents_jev_transport", default=None)


def use_transport(exchange: JevTransport) -> Token[JevTransport | None]:
    """Install an in-process HTTP exchange (tests). Not a public network bypass."""
    return _transport.set(exchange)


def reset_transport(token: Token[JevTransport | None]) -> None:
    _transport.reset(token)


def warn_unpinned_jev(model: str, min_confidence: float | None) -> None:
    """Aliases move; a tuned min_confidence should pin a versioned id."""
    if min_confidence is None:
        return
    name = (model or "").strip()
    if name in JEV_ALIASES:
        log.warning(
            "decider model %s is a moving alias; pin %s when min_confidence is set",
            name,
            PINNED_JEV_MODEL,
        )


def normalize_jev_usage(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Map Jev input_tokens/output_tokens onto the ledger's prompt/completion keys."""
    data = dict(raw or {})
    prompt = _as_int(data.get("prompt_tokens", data.get("input_tokens")))
    completion = _as_int(data.get("completion_tokens", data.get("output_tokens")))
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "input_tokens": prompt,
        "output_tokens": completion,
    }


def _as_int(raw: Any) -> int:
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


class JevDecider:
    name = "jev"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        path: str = DEFAULT_PATH,
        timeout: float = 15.0,
        max_retries: int = 2,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._api_key = api_key
        self.base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
        self.path = str(path or DEFAULT_PATH)
        if not self.path.startswith("/"):
            self.path = "/" + self.path
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self._sleep = sleep or time.sleep

    def decide(
        self,
        *,
        state: DecideState,
        questions: Mapping[str, Question],
        model: str,
        timeout: float | None = None,
    ) -> Decision:
        validate_questions(questions)
        url = f"{self.base_url}{self.path}"
        custom = _transport.get()
        _validate_jev_url(url, skip_dns=custom is not None)
        body = {
            "model": model,
            "state": state,
            "questions": {key: question.wire() for key, question in questions.items()},
        }
        payload = json.dumps(body).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"readyagents/{__version__}",
        }
        wait = float(timeout) if timeout is not None else self.timeout
        attempts = self.max_retries + 1
        last_error: DecideError | None = None
        for attempt in range(attempts):
            try:
                status, raw, _hdrs = self._exchange(
                    url, method="POST", body=payload, headers=headers, timeout=wait
                )
            except DecideError:
                raise
            except (TimeoutError, OSError, ConnectionError) as exc:
                last_error = DecideError(
                    f"Jev request failed: {type(exc).__name__}",
                    question_keys=list(questions),
                )
                if attempt + 1 < attempts:
                    self._sleep(min(2**attempt, 8))
                    continue
                raise last_error from exc
            if status in RETRY_STATUSES and attempt + 1 < attempts:
                self._sleep(min(2**attempt, 8))
                last_error = self._http_error(status, raw, questions)
                continue
            if status < 200 or status >= 300:
                raise self._http_error(status, raw, questions)
            return self._parse_decision(raw, questions=questions, requested_model=model)
        if last_error is not None:
            raise last_error
        raise DecideError("Jev request failed", question_keys=list(questions))

    def _exchange(
        self,
        url: str,
        *,
        method: str,
        body: bytes,
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, bytes, dict[str, str]]:
        custom = _transport.get()
        if custom is not None:
            return custom(url, method=method, body=body, headers=headers, timeout=timeout)
        return _http_post(url, body=body, headers=headers, timeout=timeout)

    def _http_error(
        self, status: int, raw: bytes, questions: Mapping[str, Question]
    ) -> DecideError:
        excerpt = _safe_body_excerpt(raw, self._api_key)
        return DecideError(
            f"Jev HTTP {status}: {excerpt}",
            status=status,
            question_keys=list(questions),
        )

    def _parse_decision(
        self,
        raw: bytes,
        *,
        questions: Mapping[str, Question],
        requested_model: str,
    ) -> Decision:
        if len(raw) > MAX_BODY_BYTES:
            raise DecideError(
                "decider response exceeds size cap",
                question_keys=list(questions),
            )
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DecideError(
                "Jev response is not JSON",
                question_keys=list(questions),
            ) from exc
        if not isinstance(body, dict):
            raise DecideError("Jev response is not a JSON object", question_keys=list(questions))
        raw_answers = body.get("answers")
        if not isinstance(raw_answers, dict):
            raise DecideError("Jev response is missing answers", question_keys=list(questions))
        answers: dict[str, Answer] = {}
        for key, question in questions.items():
            if key not in raw_answers:
                raise DecideError(
                    f"Jev response is missing requested answer {key!r}",
                    question_keys=list(questions),
                )
            payload = raw_answers[key]
            if not isinstance(payload, dict):
                raise DecideError(
                    f"Jev answer {key!r} is not an object",
                    question_keys=list(questions),
                )
            answers[key] = _parse_answer(key, payload, question)
        answering = str(body.get("model") or requested_model or "").strip()
        if not answering:
            answering = requested_model
        usage_raw = body.get("usage")
        usage = normalize_jev_usage(usage_raw if isinstance(usage_raw, dict) else {})
        return Decision(
            answers=answers,
            model=answering,
            decider=self.name,
            usage=usage,
            raw=body,
        )


def _parse_answer(key: str, payload: Mapping[str, Any], question: Question) -> Answer:
    qtype: QuestionType = question.type
    declared = str(payload.get("type") or qtype)
    if declared and declared != qtype:
        # Vendor type field is informational; the request type is authoritative.
        qtype = question.type
    confidence = _optional_confidence(payload.get("confidence"), key)
    probabilities = _float_map(payload.get("probabilities"))
    legend = _str_map(payload.get("legend"))
    if qtype == "noul":
        noul = _require_unit_float(payload.get("noul"), key, field="noul")
        return Answer(
            type="noul",
            noul=noul,
            confidence=confidence,
            probabilities=probabilities,
            legend=legend,
        )
    if qtype == "choice":
        choice = payload.get("choice")
        if not isinstance(choice, str) or not choice:
            raise DecideError(f"Jev answer {key!r} is missing choice")
        allowed = set(question.criteria or {}) if isinstance(question.criteria, Mapping) else set()
        if allowed and choice not in allowed:
            raise DecideError(f"Jev answer {key!r} choice {choice!r} is outside the declared space")
        return Answer(
            type="choice",
            choice=choice,
            confidence=confidence,
            probabilities=probabilities,
            legend=legend,
        )
    score = payload.get("score")
    if not isinstance(score, (int, float, str)):
        raise DecideError(f"Jev answer {key!r} is missing score")
    try:
        score_f = float(score)
    except (TypeError, ValueError) as exc:
        raise DecideError(f"Jev answer {key!r} is missing score") from exc
    if isinstance(question.criteria, list):
        high = float(len(question.criteria) - 1)
        if score_f < 0.0 or score_f > high:
            raise DecideError(f"Jev answer {key!r} score {score_f} is outside [0, {high}]")
    return Answer(
        type="score",
        score=score_f,
        confidence=confidence,
        probabilities=probabilities,
        legend=legend,
    )


def _optional_confidence(raw: Any, key: str) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise DecideError(f"Jev answer {key!r} confidence is not a number") from exc
    if value < 0.0 or value > 1.0:
        raise DecideError(f"Jev answer {key!r} confidence {value} is outside [0, 1]")
    return value


def _require_unit_float(raw: Any, key: str, *, field: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise DecideError(f"Jev answer {key!r} is missing {field}") from exc
    if value < 0.0 or value > 1.0:
        raise DecideError(f"Jev answer {key!r} {field} {value} is outside [0, 1]")
    return value


def _float_map(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in raw.items():
        try:
            out[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def _str_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _safe_body_excerpt(raw: bytes, api_key: str) -> str:
    text = raw.decode("utf-8", errors="replace")
    if api_key:
        text = text.replace(api_key, "[redacted]")
    if "Bearer " in text:
        text = text.replace(f"Bearer {api_key}", "Bearer [redacted]")
    if len(text) > ERROR_BODY_CAP:
        text = text[:ERROR_BODY_CAP] + "…"
    return text


def _validate_jev_url(url: str, *, skip_dns: bool) -> None:
    from readyagents.mcp.builtin import _assert_public_http_url, _resolve_public_ips

    try:
        parsed = _assert_public_http_url(url, kind="jev")
    except ToolError as exc:
        raise DecideError(str(exc)) from exc
    host = parsed.hostname or ""
    try:
        ipaddress.ip_address(host)
        literal_ip = True
    except ValueError:
        literal_ip = False
    if literal_ip or not skip_dns:
        try:
            _resolve_public_ips(host, kind="jev")
        except ToolError as exc:
            raise DecideError(str(exc)) from exc


def _http_post(
    url: str,
    *,
    body: bytes,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, bytes, dict[str, str]]:
    from readyagents.mcp.builtin import _assert_public_http_url, _resolve_public_ips

    try:
        parsed = _assert_public_http_url(url, kind="jev")
        host = parsed.hostname
        if host is None:
            raise DecideError("jev: URL must include a host")
        ips = _resolve_public_ips(host, kind="jev")
    except ToolError as exc:
        raise DecideError(str(exc)) from exc
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    last_err: Exception | None = None
    for ip in ips:
        try:
            status, raw, hdrs = _exchange_once(
                parsed.scheme,
                host,
                ip,
                port,
                path,
                body=body,
                headers=headers,
                timeout=timeout,
            )
            return status, raw, hdrs
        except (TimeoutError, OSError) as extra:
            last_err = extra
    raise DecideError(f"Jev HTTP failed: {last_err}") from last_err


def _exchange_once(
    scheme: str,
    hostname: str,
    ip: str,
    port: int,
    path: str,
    *,
    body: bytes,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, bytes, dict[str, str]]:
    import http.client

    if scheme == "https":
        ctx = ssl.create_default_context()
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            hostname, port, timeout=timeout, context=ctx
        )

        def connect() -> None:
            sock = socket.create_connection((ip, port), timeout)
            conn.sock = ctx.wrap_socket(sock, server_hostname=hostname)

        conn.connect = connect  # type: ignore[method-assign]
    else:
        conn = http.client.HTTPConnection(hostname, port, timeout=timeout)

        def connect() -> None:
            conn.sock = socket.create_connection((ip, port), timeout)

        conn.connect = connect  # type: ignore[method-assign]
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read(MAX_BODY_BYTES + 1)
        hdrs = {k: v for k, v in resp.getheaders()}
        return resp.status, raw, hdrs
    finally:
        conn.close()
