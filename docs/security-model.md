# Security model

This is **defence in depth**, not a solution to prompt injection. An agent
that reads untrusted text and then acts can still be manipulated. ReadyAgents
puts a firewall **between the model and its effects** so the graph, not a
system prompt, decides whether a tool runs.

## What the firewall stops

- A denied tool, even if the model emits a call for it.
- Tainted data (tool/HTTP/file/MCP/A2A/memory/browser-page/user-turn/model-from-untrusted) reaching a tool that
  policy marks `on_tainted: deny` or `gate`.
- Hosts outside an egress allowlist (`http_get` and policy `allow_hosts`),
  layered on the existing public-IP SSRF pin.
- Paths outside a policy glob, still inside the workspace sandbox.
- MCP tool description/schema changes after first use (rug pull), including
  across later runs, **when a firewall policy file is present** (pins live
  under `$READYAGENTS_HOME/mcp-pins/`; without a policy file, `evaluate` is
  identity allow and `pin_changed` does not gate).
- Agent tool-calls whose prompt or system interpolates untrusted state, even
  when the model emits literal arguments.
- Known secret values being placed into a model request.

## Supply chain

`--require-signed` refuses unsigned workflows and packs **before** import.
Signatures prove origin, not safety. This firewall is still what decides
whether a tool runs. See [supply-chain.md](supply-chain.md).

## What it does not stop

- Prompt injection as a class. Heuristics are bounded, explainable, and
  offline; they route to policy (`allow` / `gate` / `deny`) and **never
  silently rewrite** content.
- OS-level sandboxing (containers, seccomp, VMs). Confinement is process- and
  filesystem-level (`resolve_within`). Stronger isolation is an operator
  responsibility.
- A second LLM classifier. There is no extra model call in core.

## Taint

Every value entering run state carries provenance (`trusted` vs `untrusted`)
in a **parallel** `provenance` map. Provenance is evidence for policy. It is
never an authorization decision on its own without a rule.

## Policy

See [policy.md](policy.md). Without a policy file, behaviour is the 1.0
engine: no extra denies, no gates, no pins.

## Existing controls (unchanged)

Workspace path containment, `http_get` SSRF pinning, signed approval
decisions, RBAC hooks, cassette redaction, and append-only audit remain in
force. Policy gates reuse the existing approval pause (exit 2, `decide`,
decision files, localhost approval UI). Enterprise HITL (quorum, roles, lazy
deadlines, delegation) is opt-in and uses that same decision object;
[approvals.md](approvals.md). `type: a2a` output is untrusted (`source=a2a`);
see [a2a.md](a2a.md).
