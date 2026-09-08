# Security

## Reporting

If you find a vulnerability in ReadyAgents Core, please **do not** open a public issue.

The GitHub account [readyagents](https://github.com/readyagents) is a **User**, not an Organization.

Use [private vulnerability reporting](https://github.com/readyagents/readyagents-core/security/advisories/new) on this repository. That form is the working contact.

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

## Secrets in issues and PRs

Do not paste API keys, `.env`, `.env-ai`, or MCP bearer tokens into GitHub.
