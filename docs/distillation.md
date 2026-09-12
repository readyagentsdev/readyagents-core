# Distillation and local adapters

**Unreleased on this checkout — not on the 1.9.0 tag.** Distillation turns
consented run corrections into a **small adapter for one node**, evaluates it
on **your** frozen fixtures (including a holdout the trainer never saw), and
promotes it into routing only after measured thresholds. Core **never trains**
and has **no GPU extra**. Without a distillation plan and a promoted adapter,
model selection is byte-identical.

This is not a quality claim, not a benchmark, and not a leaderboard. The only
defensible numbers are the scores on the operator's own fixture suite.

```bash
readyagents distill plan --node classify --json
readyagents distill dataset --node classify --split 80/10/10 --out datasets/classify --yes
readyagents distill train --dataset datasets/classify --base qwen-2.5-7b --sign-key key.pem
readyagents distill evaluate --adapter adp_… --against evals/classify.yaml --dataset datasets/classify
readyagents distill promote --adapter adp_… --node classify --against evals/classify.yaml --dataset datasets/classify
readyagents models adapters list --json
```

## Plan

`distill plan --node NODE` reads recorded runs and feedback. It reports example
count, diversity, current cost/latency from the ledger and run store, and an
estimated saving **only** when both the incumbent and a local model are priced
in the shipped table — otherwise the saving is unnamed, not invented.

Verdicts:

- `viable`
- `not_enough_data`
- `task_too_broad`

Hardware: training runs on the operator's machine through an optional pack.
Core will not download a base model.

## Dataset

Build uses the same consent rule as feedback export: the **recorded** data
policy on the run, never a CLI flag that admits an unconsented row. Redaction
is re-verified at build; a residual secret drops the record. Splits are
deterministic from `--seed` and always include a named **holdout**. The
manifest records a reproducible dataset hash.

## Training pack

`readyagents distill train` orchestrates. If the training pack is absent the
error is `DistillTrainMissing` (`reason=pack_missing`). Core modules on that
path do not import a trainer. Provider-hosted tuning is **refused in sovereign
mode** (`DistillSovereignHosted`) and warned loudly otherwise.

## Evaluate, promote, demote

Evaluation is like-for-like on the same suite and the same scorer. A report or
promote **without a holdout score is refused**. Comparison covers accuracy,
determinism, trajectory, latency, and cost. A planted training secret must not
come back from the adapter (memorisation canary).

Adapters are versioned, hashed, and signed. Unsigned adapters refuse to load.
Promotion is threshold-gated (parity, frozen-fixture regression, latency, cost)
and, when roles are declared, approval-gated with the comparison attached.
The adapter is a routing target for **that node only**, with automatic fallback
to the incumbent. It does not widen tools or egress.

When the fixture suite changes, promoted adapters are re-scored and **demoted
automatically** below threshold with a recorded reason. Spend-before and
spend-after are `distill_delta` rows in the existing ledger.
