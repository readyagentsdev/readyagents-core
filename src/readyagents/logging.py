"""Process-wide logging setup."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_CONFIGURED = False

_FORMAT = "%(asctime)s %(levelname)s %(name)s run=%(run_id)s node=%(node_id)s: %(message)s"

_RECORD_SKIP = set(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "msg",
    "args",
    "exc_text",
}


class _SafeStreamHandler(logging.StreamHandler):
    """StreamHandler that does not raise when pytest or the runtime closed the stream.

    Background run workers log after a test has replaced or closed stderr. Python's
    default handleError then writes a traceback to that same closed stream and can
    stall or kill the worker (seen on Windows 3.14 A2A cancel waits).
    """

    def handleError(self, record: logging.LogRecord) -> None:
        try:
            stream = getattr(self, "stream", None)
            if stream is None or getattr(stream, "closed", False):
                return
            stderr = sys.stderr
            if stderr is None or getattr(stderr, "closed", False):
                return
            super().handleError(record)
        except (ValueError, OSError, AttributeError):
            return


class _RunContextFilter(logging.Filter):
    """Ensure every record has run_id / node_id so the formatter never KeyErrors."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "run_id"):
            record.run_id = "-"
        if not hasattr(record, "node_id"):
            record.node_id = "-"
        if not hasattr(record, "event"):
            record.event = "-"
        return True


class JsonLogFormatter(logging.Formatter):
    """Machine-parseable JSON lines with `run` and `node` on every event."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "run": getattr(record, "run_id", "-"),
            "node": getattr(record, "node_id", "-"),
            "event": getattr(record, "event", "-"),
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RECORD_SKIP or key in {
                "run_id",
                "node_id",
                "event",
                "run",
                "node",
            }:
                continue
            if value is None or isinstance(value, (str, int, float, bool)):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class RedactLogFilter(logging.Filter):
    """Mask configured PII/secrets in log messages and extra string fields."""

    def __init__(self, redactor: Any) -> None:
        super().__init__()
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        redact = getattr(self.redactor, "redact_text", None)
        if not callable(redact):
            return True
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()
            }
        for key, value in list(record.__dict__.items()):
            if key in _RECORD_SKIP:
                continue
            if isinstance(value, str):
                setattr(record, key, redact(value))
        return True


def configure_logging(level: str = "INFO", **kwargs: Any) -> None:
    """Configure the root `readyagents` logger.

    ``fmt`` / ``format`` may be ``text`` (default) or ``json``.
    """
    global _CONFIGURED
    numeric = getattr(logging, level.upper(), logging.INFO)
    logger = logging.getLogger("readyagents")
    logger.setLevel(numeric)
    fmt_raw = kwargs.pop("fmt", None) or kwargs.pop("format", None)
    formatter: logging.Formatter | None = None
    if fmt_raw is not None:
        fmt = str(fmt_raw).strip().lower()
        if fmt not in {"text", "json"}:
            fmt = "text"
        formatter = JsonLogFormatter() if fmt == "json" else logging.Formatter(_FORMAT)
    redactor = kwargs.pop("redactor", None)
    if not logger.handlers:
        handler = _SafeStreamHandler()
        handler.addFilter(_RunContextFilter())
        handler.setFormatter(formatter or logging.Formatter(_FORMAT))
        logger.addHandler(handler)
        logger.propagate = False
    elif formatter is not None:
        for handler in logger.handlers:
            if not any(isinstance(f, _RunContextFilter) for f in handler.filters):
                handler.addFilter(_RunContextFilter())
            handler.setFormatter(formatter)
    else:
        for handler in logger.handlers:
            if not any(isinstance(f, _RunContextFilter) for f in handler.filters):
                handler.addFilter(_RunContextFilter())
    if redactor is not None:
        _install_redactor(logger, redactor)
    _CONFIGURED = True
    if kwargs:
        logger.debug("extra logging kwargs ignored: %s", sorted(kwargs))


def _install_redactor(logger: logging.Logger, redactor: Any) -> None:
    for handler in logger.handlers:
        if not any(isinstance(f, RedactLogFilter) for f in handler.filters):
            handler.addFilter(RedactLogFilter(redactor))
        else:
            for filt in handler.filters:
                if isinstance(filt, RedactLogFilter):
                    filt.redactor = redactor


def get_logger(name: str) -> logging.Logger:
    if not name.startswith("readyagents"):
        name = f"readyagents.{name}"
    return logging.getLogger(name)


def log_event(
    logger: logging.Logger,
    event: str,
    message: str,
    *args: Any,
    run_id: str = "-",
    node_id: str = "-",
    **fields: Any,
) -> None:
    """Log a structured event. JSON format emits ``event``, ``run``, and ``node``."""
    extra: dict[str, Any] = {"run_id": run_id, "node_id": node_id, "event": event}
    for key, value in fields.items():
        if key in extra or key in _RECORD_SKIP:
            continue
        extra[key] = value
    logger.info(message, *args, extra=extra)
