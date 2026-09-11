# Simulation and adversarial testing

`readyagents simulate` generates candidate inputs from a workflow's **own
declarations** (`required_inputs`, input defaults, condition expressions,
approval nodes, foreach items, routing). It scores them with the existing
eval harness and freezes distinct failures as offline fixtures.

This is not a hosted simulator and it does not claim exhaustive coverage.
A repository that never runs simulate is unchanged.

## Deterministic first

```bash
readyagents simulate flow.yaml --seed 42 --cases 64 --json
readyagents simulate flow.yaml --seed 42 --deterministic-only
```

No model, no spend. The same seed yields the same suite. Generators emit
boundary values, empty and maximal strings, wrong types, Unicode and control
characters, injection strings from the firewall corpus, and combinations aimed
at declared branches.

## Score, coverage, dry-run

Cases are scored with `run_eval` (status, `expect_nodes`, tools, usage when a
frozen case records them). The coverage report lists **reached** and
**unreached** condition branches, approval outcomes, foreach shapes, and error
paths. It does not publish a completeness percentage.

Side-effecting tools (`write_file`, `http_get`) default to **dry-run**.
`--live-side-effects` requires a policy that allows those tools; without a
permitting policy the command is refused.

## Cluster, freeze, CI

Failures cluster by **shape**, not by raw input. One representative per
cluster is kept. With `--out DIR`, each distinct cluster is frozen (`case.yaml`
+ cassette) after secret-shaped generated content is redacted. Re-run with
`readyagents eval DIR/fail-*/case.yaml`.

```bash
readyagents simulate flow.yaml --out sims/ --fail-on new-failure
```

`--fail-on new-failure` exits non-zero only when a **new** cluster key appears.
A previously recorded class may still fail and still pass CI.

## Opt-in model personas

```bash
readyagents simulate flow.yaml --model openai:gpt-4o-mini --personas hostile,confused --max-spend 1.00
```

Metered and capped like other spend. Refused in `--sovereign` mode. Sending
prompts and schemas to a provider is a disclosure — that is why this path is
opt-in.
