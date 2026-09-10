# Connectors

**The catalog is small by design.** ReadyAgents does not compete on connector
count. Every connector call is a declared `type: tool` node, subject to policy,
taint, credential brokering, egress/SSRF pinning, spend, audit, and offline
replay. Governance is the differentiator — not legal certification.

Connectors sit **above** the existing tool registry. Builtin tools (`now`,
`calc`, `json_get`, `list_dir`, `read_file`, `write_file`, `http_get`) are
untouched. Existing packs load without implementing this contract.

## Shipped set

| Name | Shape | Destinations | Side effects |
| --- | --- | --- | --- |
| `rest` | Config-driven HTTP | host of `base_url` | read (GET) / write (POST…) |
| `sql` | SQLite read-only | `local` | read |
| `object_storage` | Workspace get/put | `local` | put is write |
| `message` | Webhook POST | declared URL host | write (gated) |
| `ingest` | File/CSV | `local` | read |

Write-shaped connectors **gate by default** (pause for `--approve`). Declare an
idempotency key so a retried write does not duplicate an effect.

## REST example (offline fixtures)

```bash
readyagents run examples/connector_rest.yaml
readyagents connectors list
readyagents connectors show rest
readyagents connectors test ingest
```

Config (`examples/connectors/support.yaml`) pins `base_url`. A template that
would expand into a **new host** is refused.

## Catalog

`readyagents connectors list|show|test` reports installed connectors: schemas,
auth *names*, destinations, determinism, idempotency. No secret values.
`test` runs the conformance harness offline.

See [connector-sdk.md](connector-sdk.md).
