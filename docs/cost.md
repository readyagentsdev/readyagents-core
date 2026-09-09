# Cost model

ReadyAgents estimates and caps **your** provider bill. It does not invoice,
meter a hosted product, or talk to a billing API. **The provider invoice is
authoritative.** Numbers here are informational.

Without `--estimate`, `--max-spend`, `--max-tokens`, `--label`, or
`readyagents spend`, runs behave as they did before this feature. Existing
`budget.max_tokens` / `budget.max_cost_usd` still use the historical
`check_budget` path.

## Price table

Shipped at `readyagents/cost/prices.json` (versioned, `updated_at` stamped).
Override with `READYAGENTS_PRICES=PATH` for negotiated rates.

Matching order:

1. Exact model identifier (`openai:gpt-4o-mini`)
2. Bare model id after the colon (`gpt-4o-mini`)
3. Longest provider prefix (`openai:`)
4. **Unpriced** — never a silent `$0`

An explicit rate of `0` in an override file means “this model is free by
contract”. An unknown model is a different state: the estimate and the spend
meter mark it unpriced. A `--max-spend` cap cannot prove an unpriced call is
under budget, so that combination **fails closed**.

Prices are USD per million tokens. They go stale; when `updated_at` is older
than `warn_after_days` (default 90) the estimate lists that warning. There is
no network fetch of prices.

## Preflight estimate

```bash
readyagents run PATH --estimate
readyagents run PATH --estimate --json
```

Walks the **same routing** the engine uses (`_next_node`, include expansion,
foreach item counts, parallel branches). It does not execute nodes and does
not open a network connection.

Output is a **range**, not a single number:

| Bound | Assumption |
| --- | --- |
| Floor | One successful primary complete per agent; no retries; no tool rounds; foreach uses the known list length (or 1 if unknown) |
| Ceiling | `max_attempts` × candidate models (primary + fallbacks) × `(1 + max_tool_rounds)` when `tools:` is set; foreach uses `max_items` when the list is unknown |

Token counting:

- Default heuristic: `max(1, len(text) // 4)` for non-empty strings. Documented
  error band **±50%**.
- Optional extra `tokenizer` (`tiktoken`, **not** in `all`): counts with
  `cl100k_base` and marks the estimate `measured`. Band **±10%** vs a vendor
  tokenizer. Core never depends on it.

Error bands are relative to the tokenizer, not to the invoice. Prompt sizes
change at runtime; tool results can dwarf the template; retries may not fire.

## Hard caps

```bash
readyagents run PATH --max-spend 2.50 --max-tokens 200000
```

`--max-spend` and `--max-tokens` apply to the **whole run** and are consulted
**before** each `complete()`. Parallel branches share one meter behind a lock.
A resumed run restores the meter from `metadata.spend` and continues the same
budget.

A single in-flight provider call can still overshoot the estimate of *that*
call (the provider is not changed). The next call is blocked.

When the preflight **ceiling** exceeds a configured cap, the run **refuses to
start** unless `--override-budget` is passed. The override is an audit event
(`budget_override`). Refusal is `budget_refuse`. Default is no cap.

Existing workflow `budget:` (`max_tokens`, `max_cost_usd`) is unchanged:
further LLM work raises `BudgetExceeded` after accumulated usage, as before.

## Runaway guards

Distinct from a budget stop and from `CircuitOpen`:

```bash
readyagents run PATH --max-model-calls 20 --max-run-tool-rounds 40 --max-wall-seconds 120
```

Or on the workflow:

```yaml
runaway:
  max_model_calls: 20
  max_tool_rounds: 40
  max_wall_seconds: 120
```

The typed error is `RunawayGuard`. Default is no guard (inert).

## Spend ledger

Append-only JSONL at `$READYAGENTS_HOME/ledger/spend.jsonl`. Hash-chained with
the same `seq` / `prev_hash` / `entry_hash` scheme as the audit trail.
Restrictive file permissions. One append per terminal run (succeeded, failed,
paused, cancelled). Aggregation uses the latest entry per `run_id`.

```bash
readyagents spend --since 2026-09-01 --by day
readyagents spend --by workflow --json
readyagents spend --by model
readyagents spend --by actor
readyagents spend --by label
```

`--label KEY=VALUE` is stored on the run metadata and in the ledger. Labels
are redacted when redaction is on. The ledger is local; share it only after
review.

## Cache savings

When the optional LLM cache hits, the run records `cache_hits`,
`cache_misses`, and `cache_savings_micros` (the price-table cost of the
cached usage that was not billed). Those fields appear on the run record,
`readyagents runs report`, and the ledger.

## Honest limits

- Estimates are not accurate to the cent.
- Unpriced is not free.
- No hosted billing dashboard, no invoice import, no automatic model
  downgrade, no silent prompt trim, no always-on cost daemon.
