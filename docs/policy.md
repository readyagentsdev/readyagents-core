# Firewall policy

A declarative file, resolved separately from the workflow so the person who
governs is not necessarily the person who authors.

This is defence in depth, not a solution to prompt injection.

## Resolution order

1. `--policy PATH`
2. `READYAGENTS_POLICY`
3. `readyagents.policy.yaml` beside the workflow
4. none (identity: 1.0 behaviour)

A malformed, unreadable, or **referenced-but-missing** file is a hard error.
Failing open is not allowed.

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
```

Unknown keys are rejected. `default: deny` means a tool runs only if a rule
allows it. Detection defaults to `gate`, not `deny`.

`gate` reuses the existing signed approval pause (exit **2**). Resume with
`readyagents resume RUN_ID --approve NODE` or `readyagents decide`.

## Commands

```bash
readyagents policy check examples/readyagents.policy.yaml
readyagents policy explain examples/policy_gated.yaml --policy examples/readyagents.policy.yaml
readyagents run examples/policy_gated.yaml --policy examples/readyagents.policy.yaml
```

`policy explain` answers which tools each node may call and why.
