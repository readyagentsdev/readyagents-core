"""Opt-in run event stream: a view over the durable path, not a bypass.

Frozen contract
---------------
Events: token, node.started, node.partial, node.finished, run.started,
run.finished, run.cancelled. ``--stream --json`` is newline-delimited JSON
with an ``event`` key and no Rich markup. Token text is incrementally
redacted (lookback = longest protected literal/pattern). Output-contract
nodes buffer (no token events). Partials persist at a byte interval and are
never a complete result (``complete: false``).
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from readyagents.observability import RunEvent
from readyagents.workflow.cancellation import CancellationToken

EVENT_TOKEN = "token"
EVENT_NODE_STARTED = "node.started"
EVENT_NODE_PARTIAL = "node.partial"
EVENT_NODE_FINISHED = "node.finished"
EVENT_RUN_STARTED = "run.started"
EVENT_RUN_FINISHED = "run.finished"
EVENT_RUN_CANCELLED = "run.cancelled"

MAX_TOKEN_CHARS = 256
MAX_PARTIAL_CHARS = 8192
DEFAULT_PARTIAL_BYTES = 1024
MAX_SSE_STREAMS = 8
MAX_EVENTS_PER_SEC = 200


@dataclass
class StreamEvent:
    event: str
    run_id: str | None = None
    node: str | None = None
    seq: int | None = None
    text: str | None = None
    bytes: int | None = None
    ttft_ms: int | None = None
    total_ms: int | None = None
    inter_token_ms: float | None = None
    status: str | None = None
    complete: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"event": self.event}
        if self.run_id:
            payload["run_id"] = self.run_id
        if self.node:
            payload["node"] = self.node
        if self.seq is not None:
            payload["seq"] = self.seq
        if self.text is not None:
            payload["text"] = self.text
        if self.bytes is not None:
            payload["bytes"] = self.bytes
        if self.ttft_ms is not None:
            payload["ttft_ms"] = self.ttft_ms
        if self.total_ms is not None:
            payload["total_ms"] = self.total_ms
        if self.inter_token_ms is not None:
            payload["inter_token_ms"] = self.inter_token_ms
        if self.status is not None:
            payload["status"] = self.status
        if self.complete is not None:
            payload["complete"] = self.complete
        return payload


class IncrementalRedactor:
    """Redact streaming text; hold a lookback window so split secrets never emit."""

    def __init__(self, redactor: Any | None) -> None:
        self._redactor = redactor
        self._hold = ""
        self._window = _lookback(redactor)

    def push(self, chunk: str) -> str:
        if not chunk:
            return ""
        if self._redactor is None:
            return chunk
        self._hold += chunk
        if len(self._hold) <= self._window:
            return ""
        ready = self._hold[: -self._window]
        self._hold = self._hold[-self._window :]
        return self._redact(ready)

    def flush(self) -> str:
        if self._redactor is None:
            text = self._hold
            self._hold = ""
            return text
        text = self._redact(self._hold)
        self._hold = ""
        return text

    def _redact(self, text: str) -> str:
        fn = getattr(self._redactor, "redact_text", None)
        if callable(fn):
            return str(fn(text))
        return text


def _lookback(redactor: Any | None) -> int:
    window = 64
    if redactor is None:
        return window
    for lit in getattr(redactor, "_literals", None) or ():
        window = max(window, len(str(lit)))
    for pat in getattr(redactor, "_patterns", None) or ():
        raw = getattr(pat, "pattern", None) or str(pat)
        window = max(window, min(len(str(raw)), 256))
    return window


@dataclass
class _NodeStream:
    seq: int = 0
    assembled: str = ""
    flushed: int = 0
    first_mono: float | None = None
    last_mono: float | None = None
    token_count: int = 0
    redactor: IncrementalRedactor = field(default_factory=lambda: IncrementalRedactor(None))


class StreamSession:
    """Collects/emits run events. Optional NDJSON sink. Optional hub publish."""

    def __init__(
        self,
        *,
        run_id: str | None = None,
        ndjson: bool = False,
        write: Callable[[str], None] | None = None,
        on_persist: Callable[[Any], None] | None = None,
        redactor: Any | None = None,
        partial_bytes: int = DEFAULT_PARTIAL_BYTES,
        buffer_nodes: set[str] | None = None,
        hub: StreamHub | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.run_id = run_id
        self.ndjson = ndjson
        self._write = write
        self._on_persist = on_persist
        self._redactor = redactor
        self.partial_bytes = max(1, int(partial_bytes))
        self.buffer_nodes = set(buffer_nodes or ())
        self.hub = hub
        self._clock = clock or time.monotonic
        self.events: list[StreamEvent] = []
        self._nodes: dict[str, _NodeStream] = {}
        self._lock = threading.Lock()
        self._started: dict[str, float] = {}
        self._event_times: deque[float] = deque()

    def shutdown(self) -> None:
        return None

    def on_event(self, event: RunEvent) -> None:
        name = event.name
        mapped = {
            "node.started": EVENT_NODE_STARTED,
            "node.finished": EVENT_NODE_FINISHED,
            "run.started": EVENT_RUN_STARTED,
            "run.finished": EVENT_RUN_FINISHED,
            "run.paused": EVENT_RUN_FINISHED,
        }.get(name)
        if mapped is None:
            return
        extra: dict[str, Any] = {}
        if mapped == EVENT_NODE_FINISHED and event.node_id:
            extra.update(
                {k: v for k, v in self.latency_for(event.node_id).items() if v is not None}
            )
            leftover = self._flush_node(event.node_id)
            if leftover:
                self._emit(
                    StreamEvent(
                        event=EVENT_TOKEN,
                        run_id=event.run_id,
                        node=event.node_id,
                        text=leftover,
                    )
                )
        if mapped == EVENT_RUN_FINISHED and event.status == "cancelled":
            mapped = EVENT_RUN_CANCELLED
        if event.duration_ms is not None:
            extra.setdefault("total_ms", event.duration_ms)
        self._emit(
            StreamEvent(
                event=mapped,
                run_id=event.run_id,
                node=event.node_id,
                status=event.status,
                **extra,
            )
        )

    def on_token(self, node_id: str, piece: str, *, state: Any = None) -> None:
        if state is not None and getattr(state, "run_id", None):
            self.run_id = state.run_id
        if not piece:
            return
        if node_id in self.buffer_nodes:
            row = self._node(node_id)
            row.assembled += piece
            return
        now = self._clock()
        with self._lock:
            if not self._rate_ok(now):
                piece = piece  # still assemble; may drop the event
                drop_event = True
            else:
                drop_event = False
        row = self._node(node_id)
        if row.first_mono is None:
            row.first_mono = now
        row.last_mono = now
        row.token_count += 1
        row.assembled += piece
        safe = row.redactor.push(piece)
        if safe and not drop_event:
            for part in _chunks(safe, MAX_TOKEN_CHARS):
                row.seq += 1
                self._emit(
                    StreamEvent(
                        event=EVENT_TOKEN,
                        run_id=self.run_id,
                        node=node_id,
                        seq=row.seq,
                        text=part,
                    )
                )
        if len(row.assembled) - row.flushed >= self.partial_bytes:
            self._persist_partial(node_id, state)

    def latency_for(self, node_id: str) -> dict[str, Any]:
        row = self._nodes.get(node_id)
        started = self._started.get(node_id)
        now = self._clock()
        out: dict[str, Any] = {}
        if row is None:
            return out
        if row.first_mono is not None and started is not None:
            out["ttft_ms"] = max(0, int((row.first_mono - started) * 1000))
        if started is not None:
            out["total_ms"] = max(0, int((now - started) * 1000))
        if row.token_count > 1 and row.first_mono is not None and row.last_mono is not None:
            span = row.last_mono - row.first_mono
            out["inter_token_ms"] = round(1000.0 * span / (row.token_count - 1), 3)
        return out

    def note_node_start(self, node_id: str) -> None:
        self._started[node_id] = self._clock()

    def _node(self, node_id: str) -> _NodeStream:
        row = self._nodes.get(node_id)
        if row is None:
            row = _NodeStream(redactor=IncrementalRedactor(self._redactor))
            self._nodes[node_id] = row
        return row

    def _flush_node(self, node_id: str) -> str:
        row = self._nodes.get(node_id)
        if row is None:
            return ""
        return row.redactor.flush()

    def _persist_partial(self, node_id: str, state: Any) -> None:
        row = self._nodes.get(node_id)
        if row is None or state is None:
            return
        text = row.assembled[-MAX_PARTIAL_CHARS:]
        row.flushed = len(row.assembled)
        pending = {
            "node_id": node_id,
            "type": "agent",
            "partial": True,
            "complete": False,
            "bytes": len(row.assembled),
            "text": text,
        }
        state.pending_node = node_id
        state.pending = pending
        self._emit(
            StreamEvent(
                event=EVENT_NODE_PARTIAL,
                run_id=getattr(state, "run_id", self.run_id),
                node=node_id,
                bytes=len(row.assembled),
                complete=False,
            )
        )
        if self._on_persist is not None:
            self._on_persist(state)

    def _rate_ok(self, now: float) -> bool:
        cutoff = now - 1.0
        while self._event_times and self._event_times[0] < cutoff:
            self._event_times.popleft()
        if len(self._event_times) >= MAX_EVENTS_PER_SEC:
            return False
        self._event_times.append(now)
        return True

    def _emit(self, event: StreamEvent) -> None:
        if self.run_id and not event.run_id:
            event.run_id = self.run_id
        self.events.append(event)
        if event.node and event.event == EVENT_NODE_STARTED:
            self.note_node_start(event.node)
        line = json.dumps(event.as_dict(), ensure_ascii=False)
        if self._write is not None:
            self._write(line + "\n")
        if self.hub is not None and event.run_id:
            self.hub.publish(event.run_id, event.as_dict())


def _chunks(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    return [text[i : i + size] for i in range(0, len(text), size)]


class StreamHub:
    """Process-local fan-out for SSE. Bounded subscriber count."""

    def __init__(self, *, max_streams: int = MAX_SSE_STREAMS) -> None:
        self.max_streams = max(1, int(max_streams))
        self._lock = threading.Lock()
        self._subs: dict[str, list[deque[dict[str, Any]]]] = {}
        self._count = 0

    def try_subscribe(self, run_id: str) -> deque[dict[str, Any]] | None:
        with self._lock:
            if self._count >= self.max_streams:
                return None
            q: deque[dict[str, Any]] = deque(maxlen=256)
            self._subs.setdefault(run_id, []).append(q)
            self._count += 1
            return q

    def unsubscribe(self, run_id: str, q: deque[dict[str, Any]]) -> None:
        with self._lock:
            rows = self._subs.get(run_id) or []
            if q in rows:
                rows.remove(q)
                self._count = max(0, self._count - 1)
            if not rows:
                self._subs.pop(run_id, None)

    def publish(self, run_id: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            rows = list(self._subs.get(run_id) or ())
        item = dict(payload)
        for q in rows:
            q.append(item)

    def live_count(self) -> int:
        with self._lock:
            return self._count


_HUB: StreamHub | None = None
_HUB_LOCK = threading.Lock()


def get_stream_hub() -> StreamHub:
    global _HUB
    with _HUB_LOCK:
        if _HUB is None:
            _HUB = StreamHub()
        return _HUB


def wants_event_stream(accept: str | None) -> bool:
    """True only when SSE is the preferred Accept type.

    Default GET is a JSON snapshot. MCP clients send
    ``application/json, text/event-stream`` and must not attach to SSE.
    """
    raw = (accept or "").strip().lower()
    if not raw:
        return False
    first = raw.split(",", 1)[0].split(";", 1)[0].strip()
    return first == "text/event-stream"


def snapshot_events(state: Any) -> list[dict[str, Any]]:
    """Durable view of a run as stream events (no phantom complete from partials)."""
    run_id = getattr(state, "run_id", None)
    events: list[dict[str, Any]] = [{"event": EVENT_RUN_STARTED, "run_id": run_id}]
    pending = getattr(state, "pending", None) or {}
    if pending.get("partial") and pending.get("complete") is False:
        events.append(
            {
                "event": EVENT_NODE_PARTIAL,
                "run_id": run_id,
                "node": pending.get("node_id"),
                "bytes": pending.get("bytes"),
                "complete": False,
            }
        )
    for row in getattr(state, "results", None) or ():
        item = {
            "event": EVENT_NODE_FINISHED,
            "run_id": run_id,
            "node": getattr(row, "node_id", None),
            "status": getattr(row, "status", None),
        }
        if getattr(row, "ttft_ms", None) is not None:
            item["ttft_ms"] = row.ttft_ms
        if getattr(row, "total_ms", None) is not None:
            item["total_ms"] = row.total_ms
        events.append(item)
    status = getattr(state, "status", None)
    if status in {"succeeded", "failed", "paused"}:
        events.append({"event": EVENT_RUN_FINISHED, "run_id": run_id, "status": status})
    elif status == "cancelled":
        events.append({"event": EVENT_RUN_CANCELLED, "run_id": run_id, "status": status})
    return events


def format_sse(payload: Mapping[str, Any]) -> bytes:
    return f"data: {json.dumps(dict(payload), ensure_ascii=False)}\n\n".encode()


def sse_snapshot_bytes(
    state: Any,
    extra: list[Mapping[str, Any]] | None = None,
) -> bytes:
    """Finite SSE body: durable snapshot plus any already-queued events."""
    frames = [format_sse(item) for item in snapshot_events(state)]
    for item in extra or ():
        frames.append(format_sse(dict(item)))
    return b"".join(frames)


class VoiceReadySession:
    """Incremental input / barge-in / partial-output shape for an optional pack.

    Core does not capture, play, or codec audio.
    """

    def __init__(self, cancellation: CancellationToken | None = None) -> None:
        self.cancellation = cancellation or CancellationToken()
        self.input_chunks: list[str] = []
        self._on_partial: Callable[[str], None] | None = None

    def push_input(self, chunk: str) -> None:
        self.input_chunks.append(str(chunk))

    def barge_in(self) -> None:
        self.cancellation.request(reason="barge-in")

    def on_partial(self, callback: Callable[[str], None]) -> None:
        self._on_partial = callback

    def emit_partial(self, text: str) -> None:
        if self._on_partial is not None:
            self._on_partial(text)
