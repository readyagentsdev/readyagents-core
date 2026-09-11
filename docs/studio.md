# Workflow studio (`readyagents studio`)

**Unreleased on this checkout — not on the 1.9.0 tag.** Opt-in foreground
localhost canvas and run inspector. It is not a hosted product, not a
multi-user editor, and it does not make workflows safe.

```bash
readyagents studio --port 8790
readyagents studio --read-only
readyagents studio --open
```

The process binds loopback only, prints a **single-use bootstrap token on
stderr** (not in the URL), and stops when you stop the command. Unlock is
`POST /studio/session` with the token in the body; it is never placed in a
query string or `Location`. `--read-only` disables every write path on the
server (save, fork, freeze, decide), not just in the UI.

## What it shows

- **Graph** — nodes, routing, nested `parallel` branches, `foreach` bodies,
  and `include` paths, with click-through to YAML line/column.
- **Edit** — forms from the shipped workflow JSON Schema. Live validation
  uses the same `load_workflow` / source-located errors as
  `readyagents validate`. A successful save writes the user's YAML
  atomically and keeps comments and key order. A file that changed on disk
  while open is refused. Studio never edits policy, secrets, or packs.
- **Runs** — list, filter by status, timeline (timing, tokens, cost, tool
  calls, retries, taint, policy). Inspector: inputs, output, redacted
  prompt, model, cassette entry.
- **Fork / freeze / diff / approve** — the existing `runs fork`,
  `runs freeze`, `runs diff`, and signed action-token decide path. An
  unsigned decide is refused.

Every studio action has a CLI equivalent. The studio is never required.
Workflows stay hand-editable YAML.

## Security

Reuse of the approvals composition: loopback bind, Host/Origin checks, CSP
(`script-src 'self'`, no inline script), body and rate limits, DNS-rebinding
rejection, path containment. Workflow text and run output are untrusted and
escaped. Do not treat screenshots as less sensitive than an evidence pack.
