"""Media builtins. Codecs other than stdlib PNG/JPEG-header/WAV/simple-PDF are extras."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from readyagents.errors import MediaError, MediaMalformed, ToolError, missing_extra_message
from readyagents.media.attach import downscale_part
from readyagents.media.caps import DEFAULT_DOWNSCALE_MAX_EDGE
from readyagents.media.ingest import ingest_bytes, ingest_path, store_from
from readyagents.media.part import part_from_mapping
from readyagents.media.png import is_png
from readyagents.media.wav import is_wav
from readyagents.tools import FunctionTool, Tool


def media_tools(*, workspace: Path) -> list[Tool]:
    workspace = Path(workspace).resolve()
    return [
        FunctionTool(
            name="media_read",
            description="Read a workspace media file into a typed hash-addressed part.",
            schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=lambda **kwargs: _media_read(workspace=workspace, **kwargs),
            determinism="sealable",
        ),
        FunctionTool(
            name="media_resize",
            description="Downscale an image part to a declared max edge. PNG in core; else extra.",
            schema={
                "type": "object",
                "properties": {
                    "part": {},
                    "max_edge": {"type": "integer"},
                },
                "required": ["part"],
            },
            handler=lambda **kwargs: _media_resize(**kwargs),
        ),
        FunctionTool(
            name="media_extract_audio",
            description="Extract audio from a container. Requires the audio extra except for WAV.",
            schema={
                "type": "object",
                "properties": {"part": {}},
                "required": ["part"],
            },
            handler=lambda **kwargs: _media_extract_audio(**kwargs),
        ),
        FunctionTool(
            name="media_convert",
            description="Convert between media formats. Requires the image or audio extra.",
            schema={
                "type": "object",
                "properties": {
                    "part": {},
                    "mime": {"type": "string"},
                },
                "required": ["part", "mime"],
            },
            handler=lambda **kwargs: _media_convert(**kwargs),
        ),
    ]


def _ctx() -> Any:
    from readyagents.media.ingest import current_ctx

    bound = current_ctx()
    if bound is not None:
        return bound

    class _Shim:
        media_store = None
        workflow = None
        pin_home = None
        cassette = None
        workflow_dir = Path.cwd()

        def __init__(self) -> None:
            from readyagents.config import get_settings

            self.pin_home = get_settings().home_path()
            self.workflow_dir = get_settings().workspace_path()

    return _Shim()


def _media_read(*, workspace: Path, path: str, **_kwargs: Any) -> dict[str, Any]:
    ctx = _ctx()
    ctx.workflow_dir = workspace
    part = ingest_path(path, ctx=ctx, workspace=workspace, source="tool:media_read")
    return part.as_ref()


def _media_resize(*, part: Any, max_edge: int | None = None, **_kwargs: Any) -> dict[str, Any]:
    parsed = part_from_mapping(part)
    if parsed is None:
        raise ToolError("media_resize requires a media part")
    ctx = _ctx()
    edge = int(max_edge or DEFAULT_DOWNSCALE_MAX_EDGE)
    data = store_from(ctx).get(parsed.sha256) if store_from(ctx).has(parsed.sha256) else None
    if data is None:
        raise MediaError("media blob not found for resize")
    if not is_png(data):
        parsed = part_from_mapping(_convert_image(data, "image/png", ctx))
        if parsed is None:
            raise MediaError(missing_extra_message("image", "image"))
    scaled = downscale_part(parsed, ctx=ctx, max_edge=edge)
    return scaled.as_ref()


def _media_extract_audio(*, part: Any, **_kwargs: Any) -> dict[str, Any]:
    parsed = part_from_mapping(part)
    if parsed is None:
        raise ToolError("media_extract_audio requires a media part")
    ctx = _ctx()
    data = store_from(ctx).get(parsed.sha256)
    if is_wav(data):
        return parsed.as_ref()
    try:
        from pydub import AudioSegment  # type: ignore[import-untyped]
    except ImportError as exc:
        raise MediaError(missing_extra_message("audio", "audio")) from exc
    import io

    segment = AudioSegment.from_file(io.BytesIO(data))
    buf = io.BytesIO()
    segment.export(buf, format="wav")
    wav = buf.getvalue()
    extracted = ingest_bytes(wav, ctx=ctx, source="tool:media_extract_audio", kind="audio")
    return extracted.as_ref()


def _media_convert(*, part: Any, mime: str, **_kwargs: Any) -> dict[str, Any]:
    parsed = part_from_mapping(part)
    if parsed is None:
        raise ToolError("media_convert requires a media part")
    target = str(mime or "").strip().lower()
    if parsed.mime == target:
        return parsed.as_ref()
    ctx = _ctx()
    data = store_from(ctx).get(parsed.sha256)
    if target.startswith("image/"):
        return _convert_image(data, target, ctx)
    if target.startswith("audio/"):
        return _convert_audio(data, target, ctx)
    raise MediaMalformed(f"unsupported convert target {mime!r}")


def _convert_image(data: bytes, target: str, ctx: Any) -> dict[str, Any]:
    try:
        from PIL import Image  # type: ignore[import-untyped]
    except ImportError as exc:
        raise MediaError(missing_extra_message("image", "image")) from exc
    import io

    image = Image.open(io.BytesIO(data))
    fmt = "JPEG" if target in {"image/jpeg", "image/jpg"} else "PNG"
    if fmt == "JPEG" and str(getattr(image, "mode", "")) in {"RGBA", "P", "LA"}:
        image = image.convert("RGB")
    buf = io.BytesIO()
    image.save(buf, format=fmt)
    converted = ingest_bytes(
        buf.getvalue(),
        ctx=ctx,
        source="tool:media_convert",
        kind="image",
        mime=target,
    )
    return converted.as_ref()


def _convert_audio(data: bytes, target: str, ctx: Any) -> dict[str, Any]:
    try:
        from pydub import AudioSegment  # type: ignore[import-untyped]
    except ImportError as exc:
        raise MediaError(missing_extra_message("audio", "audio")) from exc
    import io

    fmt = target.split("/", 1)[-1]
    if fmt == "mpeg":
        fmt = "mp3"
    segment = AudioSegment.from_file(io.BytesIO(data))
    buf = io.BytesIO()
    segment.export(buf, format=fmt)
    converted = ingest_bytes(
        buf.getvalue(),
        ctx=ctx,
        source="tool:media_convert",
        kind="audio",
        mime=target,
    )
    return converted.as_ref()
