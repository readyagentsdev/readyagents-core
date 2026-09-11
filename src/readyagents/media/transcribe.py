"""Audio transcription node. Local providers never send audio off-machine."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from readyagents.errors import EgressDenied, MediaError, MediaMalformed, missing_extra_message
from readyagents.media.cost import note_media_spend
from readyagents.media.ingest import ingest_path, store_from
from readyagents.media.part import is_media_ref, part_from_mapping
from readyagents.media.wav import is_wav, require_audio_extra, wav_duration_ms
from readyagents.workflow.templates import interpolate, lookup


def run_transcribe_node(node: Any, state: Any, ctx: Any) -> Any:
    if getattr(ctx, "offline", False) and getattr(ctx, "cassette", None) is not None:
        return _replay(node, ctx)
    part = _resolve_audio(node, state, ctx)
    note_media_spend(state, [part])
    provider = _provider_ref(node, ctx)
    if _is_local(provider):
        result = _local_transcribe(part, ctx=ctx, node=node, provider=provider)
    else:
        result = _hosted_transcribe(part, ctx=ctx, node=node, provider=provider)
    _record(node, ctx, result)
    if getattr(ctx, "dry_run", False):
        return {"dry_run": True, "provider": provider}
    return result


def _resolve_audio(node: Any, state: Any, ctx: Any) -> Any:
    ns = state.mapping()
    raw = getattr(node, "source", None)
    if not raw:
        raise MediaError(f"Node '{node.id}': transcribe nodes require 'source'")
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.startswith("{{") and stripped.endswith("}}"):
            value = lookup(ns, stripped[2:-2].strip().split("|", 1)[0].strip())
        else:
            value = interpolate(stripped, ns)
    else:
        value = raw
    if is_media_ref(value) or hasattr(value, "sha256"):
        part = part_from_mapping(value)
        if part is None:
            raise MediaMalformed("transcribe source is not a media part")
        return part
    if isinstance(value, (bytes, bytearray)):
        from readyagents.media.ingest import ingest_bytes

        return ingest_bytes(bytes(value), ctx=ctx, source="transcribe")
    if isinstance(value, str) and value.strip():
        return ingest_path(
            value.strip(),
            ctx=ctx,
            workspace=getattr(ctx, "workflow_dir", None) or Path.cwd(),
            source="transcribe",
        )
    raise MediaMalformed("transcribe source did not resolve to audio")


def _provider_ref(node: Any, ctx: Any) -> str:
    raw = getattr(node, "model", None) or getattr(ctx, "default_model", None) or "local"
    return str(raw).strip()


def _is_local(provider: str) -> bool:
    ref = provider.lower()
    return (
        ref in {"local", "scripted", "mock"}
        or ref.startswith("local:")
        or ref.startswith("mock:")
        or ref.startswith("scripted:")
    )


def _local_transcribe(part: Any, *, ctx: Any, node: Any, provider: str) -> dict[str, Any]:
    hook = getattr(ctx, "transcribe_provider", None)
    if hook is not None:
        return _normalize(hook.transcribe(part, provider=provider, local=True))
    data = store_from(ctx).get(part.sha256)
    if is_wav(data):
        duration = wav_duration_ms(data)
        return {
            "text": "",
            "segments": [{"start_ms": 0, "end_ms": duration, "text": ""}],
            "provider": provider,
            "local": True,
        }
    require_audio_extra()
    raise MediaError(missing_extra_message("audio", "audio"))


def _hosted_transcribe(part: Any, *, ctx: Any, node: Any, provider: str) -> dict[str, Any]:
    if _is_local(provider):
        return _local_transcribe(part, ctx=ctx, node=node, provider=provider)
    hook = getattr(ctx, "transcribe_provider", None)
    if hook is not None:
        return _normalize(hook.transcribe(part, provider=provider, local=False))
    # Never silently ship audio to a hosted API from core.
    raise EgressDenied(
        f"transcription provider '{provider}'",
        node_id=str(getattr(node, "id", "") or None),
    )


def _normalize(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"text": str(raw or ""), "segments": [], "local": False}
    text = str(raw.get("text") or "")
    segments = list(raw.get("segments") or [])
    return {
        "text": text,
        "segments": segments,
        "provider": raw.get("provider"),
        "local": bool(raw.get("local")),
    }


def _record(node: Any, ctx: Any, output: Any) -> None:
    cassette = getattr(ctx, "cassette", None)
    if cassette is None or not getattr(ctx, "recording", False):
        return
    if hasattr(cassette, "record_transcribe"):
        cassette.record_transcribe(node_id=node.id, output=output)


def _replay(node: Any, ctx: Any) -> Any:
    cassette = ctx.cassette
    if hasattr(cassette, "replay_transcribe"):
        return cassette.replay_transcribe(node_id=node.id)
    raise MediaError(f"Node '{node.id}': cassette has no transcribe entry")
