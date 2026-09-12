# Environments, versioning, and safe rollout

**Unreleased on this checkout — not on the 1.9.0 tag.** An environment is a
**declared configuration bundle** (routing, budget, policy, secret *scope*,
run-store path), not a server. A release is a **content-addressed pin** of the
workflow, its includes, prompts, policy, and lockfile. There is no hosted
deployment service, no cluster, no orchestrator, and no traffic router.

`readyagents run PATH` with no `--env` still runs the working copy. Repos that
never declare `readyagents.env.yaml` are unchanged.

```yaml
# readyagents.env.yaml
version: 1
environments:
  dev:    {policy: policy/dev.yaml,  budget: {max_cost_usd: 1.00}, secrets: dev}
  staging:
    policy: policy/staging.yaml
    budget: {max_cost_usd: 5.00}
    routing: routing/staging.yaml
  prod:
    policy: policy/prod.yaml
    budget: {max_cost_usd: 50.00}
    secrets: prod
    gates:
      eval: {suite: evals/prod.yaml, must_pass: true}
      fixtures: {no_regression: true}
      benchmark: {tolerance: "10%"}
      health: {min_score: 0.95, window: 100}
      approval: {roles: [release_manager]}
    canary: {percent: 10}
    shadow: {budget: {max_cost_usd: 1.00}}
    rollback:
      "on": {error_rate_above: 0.05, window: 50}
```

Secret **values** (`sk-`, `ghp_`, `AKIA`, PEM blocks, password-like keys) are
refused at validation. Scopes and paths are allowed.

```bash
readyagents env deploy examples/calc_pipeline.yaml --env staging --json
readyagents run examples/calc_pipeline.yaml --env staging --json --no-persist
readyagents promote examples/calc_pipeline.yaml --from staging --to prod --approve promote
readyagents env status --json
readyagents env history --env prod --json
readyagents env diff --env prod --from previous --to current --json
readyagents rollback --env prod --approve rollback --reason manual
```

## Pinned releases

`env deploy` snapshots the workflow (and includes, `prompts/`, policy, lockfile)
under `$READYAGENTS_HOME/releases/<digest>/` and points
`$READYAGENTS_HOME/environments/<name>/current.json` at that digest.
`run --env NAME` executes the snapshot, not the file you pass. Editing the
working copy cannot change a deployed run.

Manifests may be signed (`--sign-key` PEM, kind `release`). A tampered manifest
or a snapshot whose bytes no longer match the pins is a typed refuse
(`reason=tamper`). `--require-signed` refuses an unsigned pin.

Every run records `metadata.environment` and `metadata.release`.

## Promotion gates

`promote --from --to` copies the **source current pointer** onto the target. It
does not re-pin the working copy. Declared gates each fail with a **distinct
typed reason**:

| Gate | Error |
| --- | --- |
| eval suite not green | `EnvGateEval` (`reason=eval`) |
| frozen-fixture regression | `EnvGateFixtures` (`reason=fixtures`) |
| benchmark delta outside tolerance | `EnvGateBenchmark` (`reason=benchmark`) |
| health score below threshold | `EnvGateHealth` (`reason=health`) |
| missing approval | `ApprovalRequired` on node `promote` |

Approval shows the **complete pin diff** and uses the existing signed-decision /
RBAC path (`--approve promote`). There is no `--force` that skips gates.

## Canary and shadow

Canary: a declared percent of run ids use the **candidate** pointer.
Assignment is `sha256(run_id)[:8] % 100 < percent` — the same id always lands
on the same version. The serving channel is recorded as
`metadata.release_channel`.

Shadow: every run **additionally** executes the candidate. Its output is never
used. It has its own budget and its cost is `metadata.shadow_cost_micros` on
the **primary** record, plus a `shadow` history event. Shadow doubles provider
spend — opt in, separately metered, loud.

`env deploy --candidate` sets the candidate without moving current.

## Rollback

`rollback --env` restores the previous pointer atomically, records the reason,
and **clears the candidate**. It never rolls forward automatically.

YAML 1.1 treats unquoted `on` as boolean `true`; the loader accepts that
spelling as the rollback guard map. Quoted `"on":` is equivalent.

Declared guards (`error_rate_above`, `health_below`, `cost_per_run_above`,
`latency_ms_above`) are evaluated **when a run happens**, over that
environment's isolated store. There is no always-on watcher: a bad release
keeps serving until the next run touches the guard. A guard revert restores
the previous pin and does **not** install the failed pin as previous, so
leftover windowed failures cannot roll it forward. Later runs keep the
rolled-back pin; they do not auto-promote the failed candidate back. Manual
rollback still retains the abandoned pin as previous (authorised re-entry).

When `gates.approval.roles` is set, **manual** rollback requires the same
approval class as promote (`--approve rollback`). Guard-triggered rollback is
the declared guard, not a human bypass.

## Isolation

Each environment has its own routing overlay, budget ceiling, policy file,
secret scope name, and run-store directory (default
`$READYAGENTS_HOME/environments/<name>/runs`). Those must not leak across
names. The default `$READYAGENTS_HOME/runs` path is unchanged when `--env` is
omitted.

## Honesty limits

- Not a hosted deploy, cluster, orchestrator, or blue/green fabric.
- Not automatic promotion without gates.
- Rollback guards are lazy (no daemon).
- Shadow doubles cost; it is not free comparison.
- Signing proves the pin was produced with a trusted key, not that the agent
  is safe.
