# Connector SDK

Write a governed connector by implementing `Connector` with a frozen
`ConnectorSpec` and a `call(args, ctx)` method. **`ConnectorContext` is the
only allowed path** to HTTP, secrets, caps, and the rate limiter.

The in-process conformance harness is **not an OS sandbox**. It fails a
connector that opens its own socket, reads `os.environ` for a non-granted
secret, or exceeds size/page caps. A native extension can still bypass it.

## Spec

Declared on the connector, read by policy, the broker, replay, and the catalog:

- `input_schema` / `output_schema` (JSON Schema)
- `auth` (secret *names* and how they are presented)
- `destinations` (hosts, or `local`)
- `determinism` (`recomputed` / `sealable` / `unsealable`)
- `idempotent` / `idempotency_key`
- `rate_limit`
- `side_effects` (`none` / `read` / `write`)

Register with `readyagents.connectors.registry.register` and expose a
`FunctionTool` so calls go through `dispatch_tool`.

## Context

```python
ctx.secret("SUPPORT_TOKEN")  # granted only
ctx.http("GET", url, headers=...)  # SSRF-pinned, Retry-After, size cap
ctx.idempotent(key, produce)  # write de-dupe
ctx.cap_body(data)
```

Helpers: `iter_cursor_pages`, `iter_token_pages`, `FixtureStore` for offline
tests. Do not import vendor SDKs into core. Heavy connectors belong in a pack.

See [connectors.md](connectors.md).
