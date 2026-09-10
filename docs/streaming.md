# Streaming

Opt-in token and run-event streaming. The stream is a **view over the durable
path**, not a bypass. Without `--stream`, CLI output, exit codes, and run
records are unchanged. Core does not capture or play audio.

```bash
readyagents run examples/calc_pipeline.yaml --stream
readyagents run flow.yaml --stream --json
```

`--stream --json` writes newline-delimited JSON (no Rich markup):

```json
{"event":"run.started","run_id":"…"}
{"event":"node.started","node":"draft"}
{"event":"token","node":"draft","seq":1,"text":"Hel"}
{"event":"node.partial","node":"draft","bytes":1024,"complete":false}
{"event":"node.finished","node":"draft","ttft_ms":12,"total_ms":40}
{"event":"run.finished","status":"succeeded"}
```

Providers that implement `stream()` assemble the same `CompletionResult` as
`complete()`. Providers without `stream()` still run via `complete()`.

Partials persist at a byte interval and are marked `complete: false`. A crash
loses at most one interval; a partial is never a finished node result.

Secrets are redacted **as chunks arrive**, with a lookback window so a secret
split across two tokens cannot leak. Nodes with `output_schema` buffer rather
than emit token events.

Time-to-first-token (`ttft_ms`), inter-token latency, and total latency are
recorded on the node result when streaming produced tokens. They are not a
marketing claim; hardware and method are not published here.

SSE (opt-in, capped, same auth as the surface, clean unsubscribe):

- MCP HTTP: `GET /runs/{run_id}/events`
- A2A: `GET /tasks/{task_id}/stream`

An optional pack can use `VoiceReadySession` (incremental input, barge-in
cancel, partial output). ReadyAgents Core does not own audio.

Cancellation mid-stream uses the existing cooperative token and leaves a
resumable cancelled record, not a phantom succeeded node.
