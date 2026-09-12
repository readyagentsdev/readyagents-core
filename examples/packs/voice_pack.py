"""Optional voice-ready pack: records partial/barge-in/stream. No audio in core."""

from __future__ import annotations

from readyagents.packs import BasePack
from readyagents.sessions.voice import NullVoiceDriver


class VoicePack(BasePack):
    name = "voice-stub"
    version = "0.1.0"

    def __init__(self) -> None:
        self.driver = NullVoiceDriver()

    def register_nodes(self) -> dict:
        return {}


def get_pack() -> VoicePack:
    return VoicePack()
