# Security

## Reporting

If you find a vulnerability in ReadyAgents Core, please **do not** open a public issue.

The GitHub account [readyagentsdev](https://github.com/readyagentsdev) is a **User**, not an Organization.

Use [private vulnerability reporting](https://github.com/readyagentsdev/readyagents-core/security/advisories/new) on this repository for vulnerability reports.

Public contact: [info@readyagents.dev](mailto:info@readyagents.dev)

Include:

- A description of the issue
- Steps to reproduce
- Impact (file sandbox escape, prompt/tool injection, secret leakage, etc.)

We will acknowledge the report and work on a fix before any disclosure.

## Scope notes

- `read_file` / `write_file` / `list_dir` are intentionally sandboxed to a workspace directory (symlinks and `..` cannot escape; writes are atomic)
- `type: include` paths must stay under the parent workflow directory
- `http_get` is disabled unless explicitly opted in, and even then refuses loopback, private, link-local, and metadata hosts (including after redirects)
- `calc` is a restricted arithmetic evaluator, not Python `eval`
- API keys live in the environment / local env files and must never be committed

## Optional MCP HTTP door

v0.9 Streamable HTTP is an explicit foreground command (`readyagents mcp serve --transport streamable-http`). It is not started on install or import.

- Bind is loopback-only (`127.0.0.1`). Non-loopback binds are rejected.
- With `--auth token` (default), all `/mcp` and `/runs` requests require `Authorization: Bearer`. Token bytes are compared in constant time. Missing or wrong tokens return `401` with `WWW-Authenticate: Bearer`.
- The token comes from `READYAGENTS_MCP_TOKEN` (or `--token-env`). There is no token-value CLI flag; process listings expose argv. If the env var is empty, the process generates at least 256 bits of entropy and prints the token once to stderr. It is never persisted or logged.
- `--auth none` is allowed only on an exact loopback bind and prints a warning. Do not expose this listener to the internet.
- `Host` and browser `Origin` are checked against the configured loopback listener (DNS-rebinding defense). Responses use `Cache-Control: no-store`.
- Workspace confinement (`confine_under`), SSRF public-IP pinning, secrets/RBAC/PII hooks, append-only audit, and signed decisions still apply. Provider keys are never accepted in request JSON.
- Cooperative cancel does not kill a blocking tool or provider call. Status stays `cancel_requested` until a safe engine point.

Do not reverse-proxy this door onto the public internet in v0.9.

## Localhost approval UI

`readyagents approvals serve` is an explicit foreground loopback page, not a hosted dashboard.

- Bind is loopback-only. Non-loopback hosts are rejected before a socket opens.
- A one-use bootstrap URL is printed on stderr. After one GET it 303s to `/approvals` and sets an HttpOnly `SameSite=Strict` session cookie on `/approvals`.
- Approve/reject uses one-use HMAC action tokens bound to run id, pending node, revision, and decision. Replay, stale revision, and wrong node do not resume twice.
- Host/Origin checks, CSP (`default-src 'none'` plus self scripts/styles), `nosniff`, `no-store`, and frame denial apply. Query strings are not written to the access log.
- The page is a redacted view. It does not display full inputs, outputs, tool traces, or secrets.
- Trust boundary is the local operator on that host. A privileged local process can still read loopback traffic.
- Outbound `on_pause_url` still refuses loopback/private/metadata URLs; the UI does not add a loopback webhook exception.

## Secrets in issues and PRs

Do not paste API keys, `.env`, `.env-ai`, or MCP bearer tokens into GitHub.
