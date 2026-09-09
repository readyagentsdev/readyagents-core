"""Local, opt-in LLM response cache. File-backed, skippable, no network."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from readyagents.llm.base import CompletionResult, Message
from readyagents.llm.tool_calls import tool_calls_from_json, tool_calls_to_json


def canonical_json_bytes(payload: Any) -> bytes:
    """Stable UTF-8 JSON used by the LLM cache and the replay cassette."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")


def completion_key(
    model: str,
    messages: list[Message],
    tools: list[dict[str, Any]] | None = None,
) -> str:
    """SHA-256 of the canonical completion payload. Shared with the cassette."""
    payload = {
        "model": model,
        "messages": [_message_payload(m) for m in messages],
        "tools": list(tools or []),
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def tool_call_key(name: str, arguments: Mapping[str, Any] | None = None) -> str:
    """SHA-256 of a canonical tool name+arguments payload. Shared with the cassette."""
    payload = {"name": name, "arguments": dict(arguments or {})}
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


class LLMCache:
    """Content-addressed completions under ``$READYAGENTS_HOME/cache/``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.hits = 0
        self.misses = 0

    def key(
        self,
        model: str,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> str:
        return completion_key(model, messages, tools)

    def get(self, key: str) -> CompletionResult | None:
        path = self.root / f"{key}.json"
        if not path.is_file():
            self.misses += 1
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.misses += 1
            return None
        if not isinstance(data, dict) or "text" not in data:
            self.misses += 1
            return None
        self.hits += 1
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return CompletionResult(
            text=str(data.get("text") or ""),
            model=str(data.get("model") or ""),
            usage=dict(usage),
            tool_calls=tool_calls_from_json(data.get("tool_calls")),
        )

    def put(self, key: str, result: CompletionResult) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{key}.json"
        payload: dict[str, Any] = {
            "text": result.text,
            "model": result.model,
            "usage": dict(result.usage or {}),
            "tool_calls": tool_calls_to_json(result.tool_calls),
        }
        from readyagents.atomic import atomic_write_text

        atomic_write_text(
            path, json.dumps(payload, ensure_ascii=False), encoding="utf-8", newline="\n"
        )


def _message_payload(message: Message) -> dict[str, Any]:
    row: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_call_id:
        row["tool_call_id"] = message.tool_call_id
    if message.name:
        row["name"] = message.name
    if message.tool_calls:
        row["tool_calls"] = tool_calls_to_json(message.tool_calls)
    return row
