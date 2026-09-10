# Credential brokering

Without a credentials file, tools see the process environment as they do
today. With a file, the broker sits on the same tool-dispatch seam as the
firewall.

```yaml
version: 1
credentials:
  http_get:
    secrets: ["PARTNER_API_TOKEN"]
    ttl: 15m
  write_file:
    secrets: []
```

Resolution: `--credentials`, `READYAGENTS_CREDENTIALS`, then
`readyagents.credentials.yaml` beside the workflow.

## Grant and scrub

Before a tool runs, only the secrets listed for that tool are materialised.
Managed names are stripped from `os.environ` for tools that were not granted
them. After the call, the previous environment is restored.

A determined in-process tool can copy a value *while it is granted*. The
control is against accident and a careless pack, not hostile in-process code.

Grant, use, and denial are audited with tool, scope (names), and
`credential_kind` — never the secret value.

## Static vs minted

If the secrets backend implements `mint(name, ttl_seconds=…)`, the broker
requests a short-lived credential. Otherwise it passes the static value
through and records `credential_kind: "static"`. A static value is never
labelled short-lived.

## Subprocesses

MCP stdio children already receive a passthrough env, not the full process
environment. `readyagents.credentials.minimal_child_env` is the helper for a
minimal declared environment plus explicitly granted values.
