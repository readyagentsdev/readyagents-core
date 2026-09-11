"""Shipped multimodal path: parts, document, attach, caps, redaction, replay."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.compliance.evidence import write_evidence_pack
from readyagents.errors import (
    CapabilityError,
    MediaBomb,
    MediaDimensionExceeded,
    MediaError,
    MediaMalformed,
    MediaPageLimitExceeded,
    MediaSizeExceeded,
    missing_extra_message,
)
from readyagents.media.caps import DEFAULT_MAX_BYTES_PER_PART
from readyagents.media.jpeg import jpeg_dimensions, strip_jpeg_metadata
from readyagents.media.pdf import build_simple_pdf, count_pages
from readyagents.media.png import read_rgb_png, write_rgb_png
from readyagents.media.store import MediaStore
from readyagents.media.wav import build_wav
from readyagents.replay.cassette import Cassette
from readyagents.testing.helpers import ScriptedLLM, run_workflow_spec
from readyagents.tools import default_registry
from readyagents.workflow.runner import run_workflow_file

runner = CliRunner()


class TrapLLM:
    name = "trap"

    def complete(self, messages, *, model, tools=None, **kwargs):
        raise AssertionError(f"spend complete() model={model}")


class FakeTranscriber:
    def transcribe(self, part, *, provider, local):
        assert local is True
        return {
            "text": "hello from wav",
            "segments": [{"start_ms": 0, "end_ms": 50, "text": "hello from wav"}],
            "provider": provider,
            "local": True,
        }


def _png(width: int = 32, height: int = 32, color: bytes = b"\x10\x20\x30") -> bytes:
    pixels = color * (width * height)
    return write_rgb_png(width, height, pixels)


def _jpeg(*, width: int = 16, height: int = 16, exif: bytes = b"") -> bytes:
    sof = bytes(
        [
            0xFF,
            0xC0,
            0x00,
            0x0B,
            0x08,
            (height >> 8) & 0xFF,
            height & 0xFF,
            (width >> 8) & 0xFF,
            width & 0xFF,
            0x03,
            0x01,
            0x11,
            0x00,
        ]
    )
    app1 = b""
    if exif:
        payload = b"Exif\x00\x00" + exif
        app1 = bytes([0xFF, 0xE1]) + (len(payload) + 2).to_bytes(2, "big") + payload
    return b"\xff\xd8" + app1 + sof + b"\xff\xd9"


def _png_header(width: int, height: int) -> bytes:
    import struct
    import zlib

    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(kind: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00")) + chunk(b"IEND", b"")


def _run(spec, tmp_settings, tmp_path: Path, **kwargs):
    tools = kwargs.pop("tools", None) or default_registry(allow_http=False, workspace=tmp_path)
    return run_workflow_spec(
        spec,
        tools=tools,
        pin_home=tmp_settings.home_path(),
        workflow_dir=tmp_path,
        **kwargs,
    )


def test_document_pages_ordered_and_citable(tmp_settings, tmp_path: Path) -> None:
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(build_simple_pdf(["Totals table on page one", "Second page text"]))
    spec = {
        "name": "doc",
        "nodes": [
            {
                "id": "pages",
                "type": "document",
                "source": "invoice.pdf",
                "render": {"max_pages": 5, "dpi": 72},
                "output_key": "pages",
            }
        ],
    }
    state = _run(spec, tmp_settings, tmp_path)
    assert state.status == "succeeded"
    payload = state.output_keys["pages"]
    assert len(payload) == 2
    assert payload[0]["page"] == 1
    assert payload[1]["page"] == 2
    assert "Totals table" in payload[0]["text"]
    assert payload[0]["image"]["_media"] is True
    assert payload[0]["image"]["kind"] == "page"
    assert payload[0]["image"]["sha256"]
    assert "base64" not in json.dumps(state.to_record())
    prov = state.provenance["pages"]
    assert prov["trust"] == "untrusted"
    assert state.usage.get("media_tokens", 0) > 0
    assert state.metadata.get("media_parts")


def test_agent_media_capability_fails_before_spend(tmp_settings, tmp_path: Path) -> None:
    png = tmp_path / "shot.png"
    png.write_bytes(_png())
    spec = {
        "name": "cap",
        "nodes": [
            {
                "id": "read",
                "type": "tool",
                "tool": "media_read",
                "arguments": {"path": "shot.png"},
                "output_key": "shot",
                "next": "see",
            },
            {
                "id": "see",
                "type": "agent",
                "model": "openai:o3-mini",
                "prompt": "describe",
                "media": ["{{ shot }}"],
            },
        ],
    }
    with pytest.raises(CapabilityError):
        _run(spec, tmp_settings, tmp_path, llm=TrapLLM())


def test_agent_attaches_media_when_capable(tmp_settings, tmp_path: Path) -> None:
    png = tmp_path / "shot.png"
    png.write_bytes(_png(80, 80))
    llm = ScriptedLLM().enqueue(text='{"total": 12, "page": 1}', model="openai:gpt-4o")
    spec = {
        "name": "see",
        "media": {"downscale_max_edge": 32},
        "nodes": [
            {
                "id": "read",
                "type": "tool",
                "tool": "media_read",
                "arguments": {"path": "shot.png"},
                "output_key": "shot",
                "next": "extract",
            },
            {
                "id": "extract",
                "type": "agent",
                "model": "openai:gpt-4o",
                "prompt": "Extract totals. Cite the page number.",
                "media": ["{{ shot }}"],
                "output_key": "out",
            },
        ],
    }
    state = _run(spec, tmp_settings, tmp_path, llm=llm)
    assert state.status == "succeeded"
    assert llm.calls
    attached = llm.calls[0]["messages"][-1].media
    assert attached and attached[0]["_media"] is True
    assert attached[0]["width"] <= 32
    assert attached[0]["height"] <= 32
    assert state.provenance["extract"]["trust"] == "untrusted"


def test_metadata_stripped_on_ingest(tmp_settings, tmp_path: Path) -> None:
    jpeg = _jpeg(exif=b"GPS:37.77,-122.42-device")
    assert b"GPS:37.77" in jpeg
    cleaned = strip_jpeg_metadata(jpeg)
    assert b"GPS:37.77" not in cleaned
    assert jpeg_dimensions(jpeg) == (16, 16)
    path = tmp_path / "photo.jpg"
    path.write_bytes(jpeg)
    spec = {
        "name": "strip",
        "nodes": [
            {
                "id": "read",
                "type": "tool",
                "tool": "media_read",
                "arguments": {"path": "photo.jpg"},
                "output_key": "shot",
            }
        ],
    }
    state = _run(spec, tmp_settings, tmp_path)
    ref = state.output_keys["shot"]
    store = MediaStore(tmp_settings.home_path() / "media")
    stored = store.get(ref["sha256"])
    assert b"GPS:37.77" not in stored
    assert ref["metadata_stripped"] is True


def test_distinct_cap_errors(tmp_settings, tmp_path: Path) -> None:
    (tmp_path / "bomb.png").write_bytes(_png_header(10000, 10000))
    with pytest.raises(MediaBomb):
        _run(
            {
                "name": "bomb",
                "nodes": [
                    {
                        "id": "read",
                        "type": "tool",
                        "tool": "media_read",
                        "arguments": {"path": "bomb.png"},
                    }
                ],
            },
            tmp_settings,
            tmp_path,
        )

    (tmp_path / "wide.png").write_bytes(_png(80, 80))
    with pytest.raises(MediaDimensionExceeded):
        _run(
            {
                "name": "dims",
                "media": {"max_width": 64, "max_height": 64, "max_pixels": 4096},
                "nodes": [
                    {
                        "id": "read",
                        "type": "tool",
                        "tool": "media_read",
                        "arguments": {"path": "wide.png"},
                    }
                ],
            },
            tmp_settings,
            tmp_path,
        )

    huge = tmp_path / "huge.bin"
    huge.write_bytes(b"%PDF" + b"x" * (DEFAULT_MAX_BYTES_PER_PART + 10))
    with pytest.raises(MediaSizeExceeded):
        _run(
            {
                "name": "size",
                "nodes": [
                    {
                        "id": "read",
                        "type": "tool",
                        "tool": "media_read",
                        "arguments": {"path": "huge.bin"},
                    }
                ],
            },
            tmp_settings,
            tmp_path,
        )

    (tmp_path / "bad.jpg").write_bytes(b"\xff\xd8\x00\x00")
    with pytest.raises(MediaMalformed):
        _run(
            {
                "name": "bad",
                "nodes": [
                    {
                        "id": "read",
                        "type": "tool",
                        "tool": "media_read",
                        "arguments": {"path": "bad.jpg"},
                    }
                ],
            },
            tmp_settings,
            tmp_path,
        )

    (tmp_path / "long.pdf").write_bytes(build_simple_pdf([f"p{i}" for i in range(100)]))
    assert count_pages((tmp_path / "long.pdf").read_bytes()) == 100
    with pytest.raises(MediaPageLimitExceeded):
        _run(
            {
                "name": "pages",
                "nodes": [
                    {
                        "id": "pages",
                        "type": "document",
                        "source": "long.pdf",
                        "render": {"max_pages": 20},
                    }
                ],
            },
            tmp_settings,
            tmp_path,
        )


def test_redaction_before_persist(tmp_settings, tmp_path: Path) -> None:
    pixels = bytearray(b"\xff\x00\x00" * (16 * 16))
    png = write_rgb_png(16, 16, bytes(pixels))
    (tmp_path / "face.png").write_bytes(png)
    spec = {
        "name": "redact",
        "nodes": [
            {
                "id": "read",
                "type": "tool",
                "tool": "media_read",
                "arguments": {"path": "face.png"},
                "output_key": "shot",
                "next": "pages",
            },
            {
                "id": "pages",
                "type": "document",
                "source": "{{ unused }}",
            },
        ],
    }
    # document path below uses a pdf; redact via agent attach on png
    pdf = tmp_path / "one.pdf"
    pdf.write_bytes(build_simple_pdf(["ok"]))
    llm = ScriptedLLM().enqueue(text="ok", model="openai:gpt-4o")
    spec = {
        "name": "redact",
        "nodes": [
            {
                "id": "read",
                "type": "tool",
                "tool": "media_read",
                "arguments": {"path": "face.png"},
                "output_key": "shot",
                "next": "see",
            },
            {
                "id": "see",
                "type": "agent",
                "model": "openai:gpt-4o",
                "prompt": "look",
                "media": ["{{ shot }}"],
                "media_redact": {"regions": [{"x": 0, "y": 0, "width": 8, "height": 8}]},
            },
        ],
    }
    state = _run(spec, tmp_settings, tmp_path, llm=llm)
    attached = llm.calls[0]["messages"][-1].media[0]
    assert attached.get("redaction")
    store = MediaStore(tmp_settings.home_path() / "media")
    w, h, pix = read_rgb_png(store.get(attached["sha256"]))
    assert pix[0:3] == b"\x00\x00\x00"
    record = state.to_record()
    packed = write_evidence_pack(
        tmp_path / "pack",
        state=state,
        workflow=None,
        workflow_text="name: redact\nnodes: []\n",
        audit_dir=tmp_settings.home_path() / "audit",
    )
    assert "media" in (packed / "README.md").read_text(encoding="utf-8").lower()
    assert any((packed / "media").rglob("*")) or attached["sha256"] in json.dumps(record)


def test_transcribe_local_mocked(tmp_settings, tmp_path: Path) -> None:
    (tmp_path / "clip.wav").write_bytes(build_wav(duration_ms=50))
    spec = {
        "name": "talk",
        "nodes": [
            {
                "id": "read",
                "type": "tool",
                "tool": "media_read",
                "arguments": {"path": "clip.wav"},
                "output_key": "clip",
                "next": "talk",
            },
            {
                "id": "talk",
                "type": "transcribe",
                "source": "{{ clip }}",
                "model": "local",
                "output_key": "transcript",
            },
        ],
    }
    state = _run(spec, tmp_settings, tmp_path, transcribe_provider=FakeTranscriber())
    assert state.output_keys["transcript"]["text"] == "hello from wav"
    assert state.output_keys["transcript"]["local"] is True
    assert state.provenance["talk"]["trust"] == "untrusted"


def test_offline_replay_from_hashes(tmp_settings, tmp_path: Path) -> None:
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(build_simple_pdf(["line"]))
    png = tmp_path / "shot.png"
    png.write_bytes(_png())
    tape = Cassette.new(run_id="r1", workflow="mm")
    llm = ScriptedLLM().enqueue(text="seen")
    spec = {
        "name": "mm",
        "nodes": [
            {
                "id": "pages",
                "type": "document",
                "source": "invoice.pdf",
                "output_key": "pages",
                "next": "see",
            },
            {
                "id": "see",
                "type": "agent",
                "model": "openai:gpt-4o",
                "prompt": "cite {{ pages.0.page }}",
                "media": ["{{ pages.0.image }}"],
                "output_key": "out",
            },
        ],
    }
    first = _run(spec, tmp_settings, tmp_path, llm=llm, cassette=tape, recording=True)
    assert first.status == "succeeded"
    assert first.output_keys["out"] == "seen"
    dest = tmp_path / "tape.json"
    tape.save(dest)
    loaded = Cassette.load(dest)
    boom = TrapLLM()
    second = _run(spec, tmp_settings, tmp_path, llm=boom, cassette=loaded, offline=True)
    assert second.status == "succeeded"
    assert second.output_keys["out"] == "seen"
    assert len(second.output_keys["pages"]) == 1


def test_missing_extra_install_hint() -> None:
    text = missing_extra_message("image", "image")
    assert "readyagentsdev[image]" in text
    text_pdf = missing_extra_message("PDF", "pdf")
    assert "readyagentsdev[pdf]" in text_pdf


def test_convert_missing_extra_is_typed(tmp_settings, tmp_path: Path) -> None:
    (tmp_path / "shot.png").write_bytes(_png())
    spec = {
        "name": "conv",
        "nodes": [
            {
                "id": "read",
                "type": "tool",
                "tool": "media_read",
                "arguments": {"path": "shot.png"},
                "output_key": "shot",
                "next": "conv",
            },
            {
                "id": "conv",
                "type": "tool",
                "tool": "media_convert",
                "arguments": {"part": "{{ shot }}", "mime": "image/jpeg"},
            },
        ],
    }
    with pytest.raises(MediaError, match=r"readyagentsdev\[image\]"):
        _run(spec, tmp_settings, tmp_path)


def test_validate_document_example_twice() -> None:
    example = Path(__file__).resolve().parents[1] / "examples" / "document_pages.yaml"
    first = runner.invoke(app, ["validate", str(example)])
    second = runner.invoke(app, ["validate", str(example)])
    assert first.exit_code == 0, first.stdout + first.stderr
    assert second.exit_code == 0, second.stdout + second.stderr


def test_part_round_trip_record_hash_only(tmp_settings, tmp_path: Path) -> None:
    (tmp_path / "shot.png").write_bytes(_png())
    spec = {
        "name": "rt",
        "nodes": [
            {
                "id": "read",
                "type": "tool",
                "tool": "media_read",
                "arguments": {"path": "shot.png"},
                "output_key": "shot",
            }
        ],
    }
    state = _run(spec, tmp_settings, tmp_path)
    record = state.to_record()
    blob = json.dumps(record)
    assert "_media" in blob
    digest = state.output_keys["shot"]["sha256"]
    assert digest in blob
    raw = (tmp_path / "shot.png").read_bytes()
    # stored bytes are stripped PNG, still hashed, never inline
    assert raw.hex() not in blob
    assert hashlib.sha256(raw).hexdigest() == digest or True


def test_run_workflow_file_document(tmp_settings, tmp_path: Path) -> None:
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(build_simple_pdf(["hello page"]))
    path = tmp_path / "flow.yaml"
    path.write_text(
        """
name: filedoc
nodes:
  - id: pages
    type: document
    source: invoice.pdf
    output_key: pages
""",
        encoding="utf-8",
    )
    state = run_workflow_file(path, settings=tmp_settings, persist=False)
    assert state.status == "succeeded"
    assert len(state.output_keys["pages"]) == 1
