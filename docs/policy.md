# Firewall policy

A declarative file, resolved separately from the workflow so the person who
governs is not necessarily the person who authors.

This is defence in depth, not a solution to prompt injection.

## Resolution order

1. `--policy PATH`
2. `READYAGENTS_POLICY`
3. The policy path stored on a resumed run (so `resume` / `decide` cannot drop it)
4. `readyagents.policy.yaml` beside the workflow
5. none (identity: 1.0 behaviour)

Write-shaped connectors gate by default even with no policy file. An explicit
`tools.<name>` rule opts out of that default. See [connectors.md](connectors.md).

A malformed, unreadable, or **referenced-but-missing** file is a hard error.
Failing open is not allowed. A gated run records the resolved policy path on
the run record and reloads it on every resume or decide.

## Schema

```yaml
version: 1
default: allow            # or deny (opt-in)
egress:
  allow_hosts: ["api.example.com"]
tools:
  write_file:
    on_tainted: gate      # allow | gate | deny
    paths: ["out/**"]
  http_get:
    on_tainted: deny
    allow_hosts: ["docs.example.com"]
  "mcp:*":
    on_description_change: gate
detection:
  injection:
    threshold: 0.7
    on_match: gate
nodes:
  publish:
    require_approval: true
require_signed: false     # opt-in: refuse unsigned artifacts
frozen: false             # opt-in: refuse lockfile drift
on_lock_mismatch: allow   # allow | gate | deny
```

Unknown keys are rejected. `default: deny` means a tool runs only if a rule
allows it. Detection defaults to `gate`, not `deny`.

`gate` reuses the existing signed approval pause (exit **2**). Resume with
`readyagents resume RUN_ID --approve NODE` or `readyagents decide`. Omitting
`--policy` on resume does not drop a policy that was in force for that run.

`nodes.<id>.require_approval: true` pauses until a signed approve. Reject is a
policy deny (the node does not run).

`type: memory` is governed as `memory.write` / `memory.read` / `memory.search`
/ `memory.forget`. Memory output is untrusted; `tools.write_file.on_tainted:
deny` is how a poisoned recall is stopped. See [memory.md](memory.md).

`type: a2a` is governed as tool name `a2a`. `default: deny` without a
`tools.a2a` rule refuses delegation. `tools.a2a.allow_hosts` and
`egress.allow_hosts` restrict destination hosts. When a policy file exists,
the remote Agent Card digest is pinned (`$READYAGENTS_HOME/a2a-pins/`);
`on_description_change` defaults to **gate**. See [a2a.md](a2a.md).

MCP pins persist under `$READYAGENTS_HOME/mcp-pins/` so a description change
is detected on a later run, not only inside the run that first saw the server.

`require_signed`, `frozen`, and `on_lock_mismatch` are the supply-chain
controls. They are inert at their defaults. See [supply-chain.md](supply-chain.md).
Signing proves origin, not safety; this policy file is still the control for
tool behaviour.

## Commands

```bash
readyagents policy check examples/readyagents.policy.yaml
readyagents policy explain examples/policy_gated.yaml --policy examples/readyagents.policy.yaml
readyagents run examples/policy_gated.yaml --policy examples/readyagents.policy.yaml
```

`policy explain` answers which tools each node may call and why.
