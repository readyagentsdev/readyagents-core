"""Stdlib WAV duration. Other audio containers need the audio extra."""

from __future__ import annotations

import io
import wave

from readyagents.errors import MediaDurationExceeded, MediaMalformed, missing_extra_message


def is_wav(data: bytes) -> bool:
    return data.startswith(b"RIFF") and b"WAVE" in data[:16]


def wav_duration_ms(data: bytes) -> int:
    if not is_wav(data):
        raise MediaMalformed("not a WAV")
    try:
        with wave.open(io.BytesIO(data), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
    except wave.Error as exc:
        raise MediaMalformed(f"malformed WAV container: {exc}") from exc
    if rate <= 0:
        raise MediaMalformed("WAV has invalid sample rate")
    return int(round(frames * 1000 / rate))


def build_wav(*, duration_ms: int, rate: int = 8000) -> bytes:
    frames = max(1, int(rate * max(0, duration_ms) / 1000))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * frames)
    return buf.getvalue()


def require_audio_extra() -> None:
    try:
        import pydub  # noqa: F401
    except ImportError as exc:
        from readyagents.errors import MediaError

        raise MediaError(missing_extra_message("audio", "audio")) from exc


def check_duration(duration_ms: int, limit_ms: int) -> None:
    if duration_ms > limit_ms:
        raise MediaDurationExceeded(duration_ms, limit_ms)
