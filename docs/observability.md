# Observability

ReadyAgents has a **local observer seam**. It is not an always-on collector, not a hosted APM, and not an audit substitute. Default installs emit no telemetry to the network.

## Observer seam

`readyagents.observability` defines `RunEvent` and `emit_event`. The engine notifies observers **after** a durable-state boundary (persist of node ok / pause / finish). Events include:

- `run.started` / `run.finished`
- `node.started` / `node.finished`

A `RunEvent` carries name, timestamp, run id, workflow, optional node id/type, status, duration, and numeric `usage`. It is not a prompt log.

Rules:

- Observers **cannot** change status, exit code, the run record, or the audit trail.
- One observer failing is a redacted warning. Other observers still run.
- Nested `include` nodes do not double-emit from the parent depth.
- `shutdown_observers` runs when the runner finishes.

Packs may implement `register_observers()`. Old packs without the method still load. Core's `readyagents.packs` entry-point group stays empty, so nothing is discovered at install time. The runner collects observers from packs that were actually loaded (`--pack` / installed entry points from *other* distributions).

## Default: content-free, off

No observer is registered unless a pack returns one. JSON/text logs (`--log-format`) are local stderr, not a collector. `--record` cassettes are a separate, opt-in prompt store — not observability.

## Optional OpenTelemetry pack

`readyagents.packs.otel.OtelPack` is an in-tree pack:

- **Disabled by default.** `OtelPack().register_observers()` returns `[]` unless `READYAGENTS_OTEL` is `1` / `true` / `yes` / `on`.
- **Import is idle.** `import readyagents.packs.otel` starts no exporter, no collector, no thread, and no network. OpenTelemetry is lazy-imported only when the pack is enabled.
- **Content-free.** Spans may carry usage, model, node, status, run id, and namespaced cost. They must not carry prompt text, inputs, outputs, tool arguments, or secret/key values.
- **No always-on collector.** Enabling the pack does not start an OTLP exporter. Pass an exporter into `OtelPack(exporter=...)` only in tests or in your own process. Core does not read a collector endpoint on import.

Install the extra (this extra is **not** part of `all`):

```bash
pip install "readyagentsdev[otel]"
```

Enable for a process that has loaded the pack:

```bash
export READYAGENTS_OTEL=1
```

Without that env var the pack is a no-op even if the extra is installed. Without loading the pack, the env var alone starts nothing.

### Span attributes (allowlist)

When enabled, each event becomes a span whose attributes are limited to:

| Attribute | Source |
| --- | --- |
| `readyagents.run_id` | run id |
| `readyagents.workflow` | workflow name |
| `readyagents.node_id` | node id when present |
| `readyagents.node_type` | node type when present |
| `readyagents.status` | status when present |
| `readyagents.duration_ms` | duration when present |
| `readyagents.cost_micros` | `usage["cost_micros"]` |
| `gen_ai.request.model` | configured model name only |
| `gen_ai.usage.input_tokens` | `usage["prompt_tokens"]` (count, not the prompt) |
| `gen_ai.usage.output_tokens` | `usage["completion_tokens"]` |
| `gen_ai.usage.total_tokens` | `usage["total_tokens"]` |

Anything else on the event (prompt, input, output, arguments, secrets) is dropped.

### Test hook

```python
from readyagents.packs.otel import MemorySpanExporter, OtelPack

exporter = OtelPack.memory_exporter()  # or MemorySpanExporter()
observers = OtelPack(exporter=exporter).register_observers()
```

`MemorySpanExporter.get_finished_spans()` returns in-process records. No network.

## What this is not

- Not a replacement for the append-only audit JSONL.
- Not legal compliance or certification (see [compliance.md](compliance.md)).
- Not encryption, not a SIEM, not a promise that spans arrived anywhere.
