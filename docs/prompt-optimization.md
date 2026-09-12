# Prompt optimization

`readyagents optimize` evolves a node's prompt against **your own eval
suite**. It is not a hosted optimizer, not model fine-tuning, and it does
not claim parity with GEPA, DSPy, or any named published method. What it
claims is a measured pass-rate delta on the suite you passed.

A repository that never runs `optimize` is unchanged. Literal `prompt:`
fields keep working. User workflow YAML is never rewritten.

```bash
readyagents optimize flow.yaml --eval evals/suite.yaml --max-iterations 12 --max-spend 3.00 --min-improvement 0.05 --json
readyagents prompts list flow.yaml
readyagents prompts history flow.yaml --id draft --json
readyagents prompts diff flow.yaml --id draft --left 1 --right 2
readyagents prompts rollback flow.yaml --id draft
```

## Loop

1. Score the current prompt with `readyagents eval` (determinism, trajectory,
   usage ceilings included). No LLM-as-judge in core.
2. Reflect on **failing cases and their diffs** after redaction.
3. Propose N candidate prompts (this is the only step that constructs a
   provider and spends).
4. Score each candidate the same way.
5. Keep the best only if it clears `--min-improvement` **and** regresses
   nothing.
6. Persist the iteration so a killed run resumes.

Where a case has a cassette, **baseline** scoring replays it offline at
**zero cost** and does not construct a provider. A mutated candidate prompt
cannot replay that cassette (the key includes the prompt), so candidates
are scored with the generation provider and that spend counts under
`--max-spend`. `--max-iterations`, `--max-spend`, and
`--max-wall-seconds` each stop with a distinct typed reason
(`iterations` / `spend` / `wall`). Spend is persisted so `--resume` does
not re-pay completed work. Reflection sees redacted failing-case **diffs**
(inputs, expected vs actual), not name/reason only. The JSON `held_out.score`
is the last scored or adopted hold-out pass rate, not the pre-candidate
baseline snapshot.

## Registry

Prompts are stored beside the workflow as `{stem}.prompts.json`: id, version,
content hash, text. A literal prompt is auto-registered on first optimize (or
`prompts list`). Address by id and version. Changing the text changes the
hash. The YAML file bytes do not change.

## Promotion, hold-out, rollback

Nothing is adopted silently. Promotion requires the declared minimum
improvement. `--require-approval` records a payload with the prompt diff and
the score delta and does not switch the active version.

A **held-out set is mandatory**. Pass `--hold-out SUITE` or include at least
two cases (the last third is held out). The held-out score is always in the
JSON. A held-out regression refuses promotion.

A candidate that improves the train metric but fails a frozen fixture is
rejected and the fixture **name** is in `regressions`. `--frozen SUITE` adds
fixtures; cassette-backed train/hold-out cases are treated as frozen too.

`prompts rollback` restores the previous version's text and content hash
exactly. It still does not rewrite the workflow YAML.

## Safety and overfitting

Redaction applies to everything the reflection model sees. A secret-shaped
string in an eval case blocks that case (recorded reason) and is not sent.

A candidate prompt is untrusted model output: stored as JSON text, never
parsed or executed as workflow configuration, length-bounded.

Overfitting is the real failure mode. A missing hold-out is an error, not a
warning. Deltas are on **your** suite, not a public leaderboard.

## What this is not

- No weight updates or training-data export.
- No hosted optimization service and no telemetry.
- No automatic adoption without the gate.
- No extra dependency — stdlib plus the operator's own provider.
