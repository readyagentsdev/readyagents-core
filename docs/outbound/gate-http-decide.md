# ReadyAgents Gate — signed HTTP decide

When an approval node pauses, core writes the run and can POST outbound (`on_pause_url`). Core does **not** listen. ReadyAgents Gate (example: `examples/packs/hitl_gate.py`) accepts **one signed HTTP POST** and maps it to the shipped `readyagents decide` / `resume` path.

```bash
readyagents run examples/approval_gate.yaml          # exit 2, paused
# POST HMAC-SHA256 body to the Gate handler (header X-ReadyAgents-Signature)
# body: {"run_id":"<id>","node_id":"gate","decision":"approve"}
```

A valid signature resumes the run (`approval_gate ok`). An unsigned or forged signature leaves the run paused.

Three decide paths share resume/RBAC/audit; they are not the same door:

| Path | What signs | Who listens |
| --- | --- | --- |
| `readyagents decide` / `resume --approve` | Nothing HTTP; local CLI | No listener |
| ReadyAgents Gate example (`hitl_gate.py`) | HMAC-SHA256 of the raw POST body (`X-ReadyAgents-Signature`) | Optional one-shot example server only |
| Localhost approval UI (`readyagents approvals serve`) | In-process bootstrap/session/action tokens | Explicit loopback foreground process |
| MCP `tasks/update` (`2026-07-28`) | Same decision object as `decide`; optional HMAC via `READYAGENTS_DECISION_SECRET` | Explicit loopback `mcp serve --transport streamable-http` |

`tasks/update` `inputResponses` are converted into `{node_id, decision, actor}` and travel the existing RBAC, audit, and resume path. A 2026 MCP client is not a bypass.

The CLI does **not** sign HTTP payloads. Shared HMAC helpers live in `readyagents.decisions.signing` so Gate and tests stay compatible. The UI does not relax `on_pause_url` SSRF pinning and does not accept loopback callback URLs.
