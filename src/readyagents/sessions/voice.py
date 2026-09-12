"""Voice-ready turn contract. Core owns no audio, codec, SIP, or WebRTC."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class VoiceTurnDriver(Protocol):
    """Optional pack surface: partial input, barge-in, streaming output."""

    def partial_input(self, text: str) -> None: ...

    def barge_in(self, text: str) -> Any: ...

    def stream_output(self, chunk: str) -> None: ...


class NullVoiceDriver:
    """In-process fake. Records calls; never captures audio."""

    def __init__(self) -> None:
        self.partials: list[str] = []
        self.barge_ins: list[str] = []
        self.chunks: list[str] = []

    def partial_input(self, text: str) -> None:
        self.partials.append(str(text))

    def barge_in(self, text: str) -> str:
        self.barge_ins.append(str(text))
        return str(text)

    def stream_output(self, chunk: str) -> None:
        self.chunks.append(str(chunk))
