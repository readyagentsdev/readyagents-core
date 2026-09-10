# Enterprise approvals

**Core has no timer, scheduler, cron, or daemon.** A deadline on an approval
gate is evaluated the next time the run is touched — `resume`, `decide`, or a
status query (`readyagents runs show`). An unattended expiry does **not** fire
on its own. A scheduled sweep belongs in an optional pack, not in core.

Expiry uses the **host clock**. A host whose clock is untrusted weakens
deadlines; the evaluated timestamp and `clock_source: host` are recorded on the
pause and in the audit trail.

`on_expire: approve` is refused at schema validation. An attacker who can stall
a gate must never gain an approval. Allowed values are `reject`, `escalate`,
and `fail`.

Actors are strings checked by the existing RBAC hook. There is no user
directory. The strength of an approval is the strength of the operator’s actor
model and signing keys. HMAC signatures on inbound decision bodies and Ed25519
signatures on workflow/pack artifacts stay separate.

A gate that declares **none** of the fields below is unchanged from 1.0: one
decision resolves it, pause is exit **2**, `readyagents decide` / the localhost
UI / signed decision files keep the same path.

## Quorum and separation of duties

```yaml
- id: gate
  type: approval
  prompt: "Release payment of {{total}}?"
  then: receipt
  else: denied
  approvals_required: 2
  distinct_actors: true          # default when quorum > 1
  deny_actor: ["$initiator"]     # rewritten to metadata.actor
```

The run stays paused until `approvals_required` approve votes are recorded.
Each vote is persisted **before** the threshold is evaluated, so a crash cannot
lose a partial vote. Votes are idempotent per normalised actor
(`strip().casefold()`). `distinct_actors` refuses the same identity twice,
including casing variants (`Alice` / `alice`).

A single `reject` short-circuits unless the workflow sets
`reject_short_circuit: false`.

`$initiator` / `initiator` in `deny_actor` is rewritten to the run’s
`metadata.actor` at pause time.

## Roles

```yaml
  approver_roles: [security, finance]
  require: any    # or all
```

`require: any` (default) means a vote from any declared role counts toward
quorum. `require: all` means every declared role must approve. The resolved
eligible set is recorded on the pause as `eligible_actors` (`role:security`,
or `["*"]` when no roles are declared).

An ineligible actor is refused, audited, and the run **stays paused**.

## Deadlines and escalation

```yaml
  expires_in: 4h
  on_expire: escalate    # reject | escalate | fail — never approve
  escalate_to: [director]
```

`expires_in` accepts `30s`, `15m`, `4h`, `1d` (and the longer spellings).
On the first touch after the deadline:

- `reject` — take the `else` path (audited, elapsed time recorded)
- `fail` — raise `GateExpired`; the run fails
- `escalate` — rewrite `eligible_actors` **only** to the workflow-declared
  `escalate_to` list (one-shot, clock is not reset). A vote from the new
  eligible set is accepted on that same touch. The next touch while still
  past the deadline fails.

See `examples/expiring_gate.yaml` (keyless).

## Delegation

```bash
readyagents delegate --from alice --to bob --until 2026-09-20T00:00:00Z --scope security
readyagents delegations list --json
readyagents delegations revoke ID
```

Stored under `$READYAGENTS_HOME/delegations.json` (atomic write, restrictive
permissions). Checked at **decision** time, so a grant created after a pause
still applies, and one that expired before the decision does not.

Refused: self-delegation, chains, overlapping grants, and a later unscoped
grant that would widen an existing scoped grant. A delegated vote records
both `actor` (the delegate) and `delegated_from` (the delegator) and is
audited for both parties.

## Reasons and overrides

`require_reason: true` refuses a vote with no reason; the run stays paused.
`readyagents decide … --reason "checked invoice"` (also on `resume`).

`recommendation: approve` (or `reject`) on the node: a human decision that
contradicts it is stored as `override: true` on the vote (Article 14
confirmed-or-overridden).

## Notification channels

Opt-in. Failure logs once and **never** changes run state. Payloads carry run
id, node id, a redacted bounded question, eligible roles, and the deadline —
not the full prompt or node outputs.

```yaml
  notify:
    - kind: file
      path: .readyagents/pending.jsonl
    - kind: command
      command: ["/usr/local/bin/notify-approvers"]
    - kind: webhook
      url: "https://internal.example.com/hook"
```

Webhooks use `notify.post_json` (SSRF pinning and egress allowlist). The
existing `on_pause` / `on_pause_url` hook still fires from the engine.

## Queue

```bash
readyagents approvals list
readyagents approvals list --role security --actor alice --expiring-within 1h --json
```

Shows paused approval gates the caller may see. Unauthorized and missing look
identical (empty). The localhost UI (`readyagents approvals serve`) lists the
same pauses with additive vote/deadline fields; its token model is unchanged.

Every new path converts into the existing signed decision object. Unsigned or
unauthorized decisions (HMAC when `READYAGENTS_DECISION_SECRET` is set, RBAC
deny, UI action-token miss) are refused, audited, and leave the run paused.

## Compatibility

0.9-era pause payloads without the new fields resume as a single-approval
gate. `examples/approval_gate.yaml`, `multi_gate.yaml`, `fanout_gate.yaml`,
and `composed_gate.yaml` are unchanged.

Walkthroughs: `examples/quorum_gate.yaml`, `examples/expiring_gate.yaml`.
