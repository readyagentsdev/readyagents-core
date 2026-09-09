"""Offline cassette provider. Never constructs a live client or reads a key."""

from __future__ import annotations

from typing import Any

from readyagents.errors import CassetteError
from readyagents.llm.base import CompletionResult, Message
from readyagents.replay.cassette import Cassette


class CassetteProvider:
    """Serves recorded completions. Offline: no network, no keys, no SDK import."""

    name = "cassette"

    def __init__(self, cassette: Cassette, *, node_id: str = "") -> None:
        self.cassette = cassette
        self.node_id = node_id

    def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: Any = None,
        **kwargs: Any,
    ) -> CompletionResult:
        specs = tools if isinstance(tools, list) else None
        return self.cassette.replay_llm(
            node_id=self.node_id,
            model=model,
            messages=messages,
            tools=specs,
        )


def require_cassette(path: Any, *, flag: str = "--record") -> None:
    if path is None:
        raise CassetteError(
            f"No cassette found for this run. Record one with {flag} "
            "or READYAGENTS_RECORD=1, then replay --offline."
        )
