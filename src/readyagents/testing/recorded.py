"""Offline / recorded LLM: replay a cassette file, no network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from readyagents.errors import LLMError
from readyagents.llm.base import CompletionResult, Message
from readyagents.llm.tool_calls import tool_calls_from_json, tool_calls_to_json


class RecordedLLM:
    """Replay stored completions. Optional ``inner`` records new calls to disk."""

    name = "recorded"

    def __init__(self, cassette: Path | str, *, inner: Any | None = None) -> None:
        self.cassette = Path(cassette)
        self.inner = inner
        self.calls: list[list[Message]] = []
        self.models: list[str] = []
        self._tape: list[dict[str, Any]] = []
        self._cassette_doc = None
        if self.cassette.is_file():
            loaded = json.loads(self.cassette.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                self._tape = [row for row in loaded if isinstance(row, dict)]
            elif isinstance(loaded, dict):
                from readyagents.replay.cassette import Cassette

                self._cassette_doc = Cassette.load(self.cassette)
        self._index = 0

    def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: Any = None,
        **kwargs: Any,
    ) -> CompletionResult:
        self.calls.append(messages)
        self.models.append(model)
        if self._cassette_doc is not None:
            return self._cassette_doc.replay_llm(
                node_id="",
                model=model,
                messages=messages,
                tools=tools if isinstance(tools, list) else None,
            )
        if self._index < len(self._tape):
            row = self._tape[self._index]
            self._index += 1
            usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
            return CompletionResult(
                text=str(row.get("text") or ""),
                model=str(row.get("model") or model),
                usage=dict(usage),
                tool_calls=tool_calls_from_json(row.get("tool_calls")),
            )
        if self.inner is None:
            raise LLMError(
                f"No recorded completion at index {self._index} in {self.cassette} "
                "(offline replay — no network)"
            )
        result = self.inner.complete(messages, model=model, tools=tools, **kwargs)
        self._tape.append(
            {
                "text": result.text,
                "model": result.model or model,
                "usage": dict(result.usage or {}),
                "tool_calls": tool_calls_to_json(result.tool_calls),
            }
        )
        self._index += 1
        self.cassette.parent.mkdir(parents=True, exist_ok=True)
        self.cassette.write_text(json.dumps(self._tape, indent=2, ensure_ascii=False) + "\n")
        return result
