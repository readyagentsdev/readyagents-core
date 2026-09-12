"""Stream user-facing text through the existing incremental redactor."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from readyagents.workflow.stream import IncrementalRedactor


def redact_chunks(chunks: Iterable[str], redactor: Any | None) -> list[str]:
    """Hold back a secret split across chunks; never emit then retract."""
    inc = IncrementalRedactor(redactor)
    out: list[str] = []
    for chunk in chunks:
        piece = inc.push(str(chunk))
        if piece:
            out.append(piece)
    tail = inc.flush()
    if tail:
        out.append(tail)
    return out
