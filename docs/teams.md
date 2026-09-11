# Multi-agent teams (`type: team`)

**Unreleased on this checkout — not on the 1.9.0 tag.** Opt-in supervisor plus a
**closed** declared member set. The engine enforces stop conditions. This does
**not** claim routing quality. No runtime member creation, no nested teams, no
new mandatory extra.

```yaml
- id: research_team
  type: team
  strategy: plan_then_execute
  scratchpad:
    keys: [findings, open_questions]
  terminate:
    max_rounds: 8
    max_cost_usd: 2.00
    goal: "{{ scratchpad.findings | len }}"
  supervisor:
    model: mock:supervisor
    prompt: "Decompose the question and route to a member."
  members:
    - id: searcher
      role: research
      type: agent
      tools: [http_get]
      scratchpad: {read: [open_questions], write: [findings]}
    - id: sign_off
      role: human
      type: approval
```

Strategies (declared policy, not an emergent loop): `route`, `plan_then_execute`,
`debate`, `pipeline`. The supervisor's next member is JSON over declared ids;
a hallucinated id is `TeamUnknownMember`. Nested `type: team` members fail at
validate.

## Stop, scratchpad, handoff

`terminate.max_rounds`, `max_cost_usd` / `max_spend`, `max_wall_seconds`, and
`goal` are checked by the engine at round boundaries. Each limit is a distinct
typed error (`TeamRoundsExceeded`, `TeamSpendExceeded`, `TeamWallExceeded`) with
scratchpad and handoffs on the run record. Goal success is `stop: goal`.

Scratchpad keys are declared. Per-member `read` / `write` grants are enforced.
Writes are taint-marked with the writer. Handoffs are `{from, to, reason,
payload, ts}` and are durable checkpoints. A paused approval member resumes
with `--approve <member_id>`. `runs fork` copies `metadata.teams`. Offline
replay uses the existing cassette.

A team never widens the workflow's tool or egress permissions. See
[examples/team_pipeline.yaml](../examples/team_pipeline.yaml).
