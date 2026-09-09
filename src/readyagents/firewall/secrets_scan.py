"""Refuse known secret values in model-bound messages."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from readyagents.errors import PolicyDenied
from readyagents.replay.record import contains_secret


def scan_messages(
    messages: Sequence[Any],
    secrets: list[str] | None,
    *,
    node_id: str,
) -> None:
    if not secrets:
        return
    blob_parts: list[str] = []
    for message in messages:
        content = getattr(message, "content", None)
        if content:
            blob_parts.append(str(content))
        elif isinstance(message, dict) and message.get("content"):
            blob_parts.append(str(message["content"]))
    blob = "\n".join(blob_parts)
    if contains_secret(blob, list(secrets)):
        raise PolicyDenied(
            node_id,
            "refusing to place a known secret value into a model request",
            rule="secrets.prompt",
        )
