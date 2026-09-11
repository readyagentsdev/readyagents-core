# Multimodal I/O

Typed media parts sit alongside unchanged text. ReadyAgents stores every part
content-addressed (SHA-256) under the run media directory and references it by
hash in the run record, the cassette, and the evidence pack. Giant base64
blobs do not travel in state.

This is plumbing and governance: size caps, metadata strip, taint, redaction,
per-part cost, and offline replay. It is **not** an OCR product, **not** a
vision model, and **not** a claim of extraction accuracy.

## MediaPart

A value in run state may be text or a media ref:

| Field | Meaning |
| --- | --- |
| `kind` | `image`, `audio`, `video`, `document`, `page`, or `binary` |
| `mime` | Sniffed MIME type |
| `sha256` | Content hash of the stored bytes |
| `bytes_len` | Size after metadata strip |
| `width` / `height` | Image dimensions from the container header |
| `duration_ms` | WAV duration (other containers need the audio extra) |
| `page_index` | Zero-based page index for document pages |
| `provenance` | Always untrusted after ingest |
| `redaction` | Regions/classes removed, when redaction ran |

Bytes live in `$READYAGENTS_HOME/media/<aa>/<sha256>`. The record stores the
ref, not the payload.

## Agent attach

```yaml
- id: extract
  type: agent
  model: openai:gpt-4o
  media: ["{{ pages.0.image }}"]
  prompt: "Extract the totals table. Cite the page number."
```

The engine checks the bundled capability matrix **before** `complete()`. A
text-only model (`media: false`) is a typed `CapabilityError` and does not
spend. Images larger than `media.downscale_max_edge` (default 2048) are
downscaled before the request when the part is a PNG; other codecs need the
`image` extra.

Text-only agent nodes are unchanged: `Message.content` stays a string and
`complete(messages)` is byte-identical when `media:` is omitted.

## `type: document`

```yaml
- id: pages
  type: document
  source: "{{ invoice_pdf }}"
  render: {dpi: 72, max_pages: 20, max_bytes_per_page: 2000000}
  output_key: pages
```

Output:

```json
[
  {"index": 0, "page": 1, "text": "...", "image": {"_media": true, "sha256": "...", "kind": "page"}}
]
```

Page numbers are 1-based and ordered. Downstream nodes cite `page`. Simple
uncompressed PDFs split with the stdlib path; general PDFs need
`readyagentsdev[pdf]` (`pypdf`). Missing extra is the same install-hint shape
as OpenAI. Page images are deterministic PNGs for addressing and replay, not
print-quality renders.

## `type: transcribe`

```yaml
- id: talk
  type: transcribe
  source: "{{ clip }}"
  model: local
  output_key: transcript
```

`model: local` (or `local:…`, `mock:…`) never sends audio off-machine. A hosted
provider is a typed egress refuse unless a test/provider hook is installed.
WAV duration is stdlib; other containers need `readyagentsdev[audio]`.

## Builtins

| Tool | Core | Extra |
| --- | --- | --- |
| `media_read` | Hash-store a workspace file | — |
| `media_resize` | PNG nearest-neighbor | `image` for JPEG and others |
| `media_extract_audio` | WAV identity | `audio` for containers |
| `media_convert` | no-op when MIME matches | `image` / `audio` otherwise |

## Caps and safety

Hard caps apply **before** decode:

- per-part bytes
- per-run media bytes (`budget.max_media_bytes` or `media.max_run_bytes`)
- width / height / pixels
- page count
- duration

Distinct typed errors: `MediaBomb` (decompression bomb), `MediaSizeExceeded`,
`MediaDimensionExceeded`, `MediaMalformed`, `MediaPageLimitExceeded`,
`MediaDurationExceeded`, `MediaBudgetExceeded`.

Ingest strips PNG text/EXIF chunks and JPEG APP1/IPTC/COM by default. Extracted
text and model output derived from media are taint-untrusted.

## Redaction

Declared regions (`media_redact.regions`) are boxed on PNG parts before the
part is persisted, recorded, or sent. Detected classes (`faces`, card numbers,
ID numbers) are opt-in and pluggable; without a detector extra they fail closed
with the image-extra install hint. Redaction is engine-enforced, not a prompt.
What was removed is recorded on the part. Completeness is **not** claimed.

## Cost and replay

Each part records estimated tokens and `cost_micros` on the run record
(`metadata.media_parts`, `usage.media_tokens`) and on the spend ledger. The
estimate is governance accounting, not a vendor invoice.

The cassette stores media by hash in a sibling `*.media/` directory. Offline
replay reconstructs the run from hashes: no re-upload, no re-spend, no
network. A media part in an evidence pack is sensitive — the pack README
warns and the files are confined under `media/`.

## Extras

```bash
pip install "readyagentsdev[image]"   # pillow — JPEG resize/convert, detectors
pip install "readyagentsdev[pdf]"     # pypdf — general PDF split
pip install "readyagentsdev[audio]"   # pydub — non-WAV containers
```

None of these are in `[all]`. Core installs stay text-only.

## What this is not

- Image, audio, or video **generation**
- A bundled OCR or vision model
- Streaming video analysis
- A claim that redaction or extraction is complete or accurate
