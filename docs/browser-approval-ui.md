# Localhost browser approval UI

ReadyAgents can show paused approval nodes in a **foreground localhost page**.
It is not a hosted dashboard, not a control plane, and not an always-on service.

Closing the process stops the page. Tokens live only in that process memory.

## Start

```bash
readyagents approvals serve --host 127.0.0.1 --port 8766
```

The command prints a **single-use bootstrap URL on stderr** (never stdout):

```text
http://127.0.0.1:8766/approvals?token=<bootstrap>
```

Open that URL once. The server consumes the token, sets an HttpOnly `SameSite=Strict`
session cookie scoped to `/approvals`, and redirects to `/approvals` so the token
does not stay in history.

Optional mount on the TASK-01 HTTP process:

```bash
readyagents mcp serve --transport streamable-http --approval-ui
```

v0.9 rejects non-loopback binds (`0.0.0.0`, public IPs). Default port is `8766`.

The server does **not** open a browser by default. `--no-open` is the default.

## What you see

The page lists **only persisted runs** that are `paused` on an `approval` node,
newest first (at most 100). Each row shows:

- full run ID
- workflow name
- pending node id
- prompt
- start time
- actor
- a redacted summary

It does **not** show full inputs, outputs, tool traces, secrets, API keys, or
unredacted PII. Display redaction runs even when workflow persistence redaction
is off.

Approve and Reject confirm in a native dialog, then POST to the same decide/resume
path as `readyagents decide` (RBAC, append-only audit, persistence).
For concurrent local mutations (this page plus CLI/HTTP decide), optional SQLite is
recommended; JSON remains the default ([run-stores.md](run-stores.md)).

## Tokens

| Token | Lifetime (default) | Use |
| --- | --- | --- |
| Bootstrap | 5 minutes, one GET | Printed once on stderr |
| Session cookie | 30 minutes | List/read only |
| Action | 5 minutes, one POST | HMAC-bound to run, node, revision, decision |

A generated server secret is used unless `READYAGENTS_APPROVAL_UI_SECRET` is set.
Never pass the secret as a CLI flag. Restarting the process invalidates tokens.

Replay, a concurrent second click, a stale revision, or the wrong node returns
`409`/`401` and does not resume twice.

## Shutdown

Ctrl-C (or killing the foreground command) closes the listener. There is no
background recovery and no claim that the UI survives process exit.

## Threat boundary

This is a local operator UI on loopback. It is not an isolation boundary against
a compromised host. Host/Origin checks, CSP, and one-use tokens mitigate CSRF
and rebinding from other websites. Outbound `on_pause_url` still refuses
loopback/private/metadata destinations; the UI does not punch a hole in SSRF
pinning.

Walkthrough (keyless): `examples/browser_approval.yaml`.
