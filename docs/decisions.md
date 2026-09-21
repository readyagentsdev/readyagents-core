# Typed decisions
> **Tier:** experimental — ships and is tested; shape may change. See [stability](stability.md).

A `decide` node asks typed questions about a state and gets probabilities
back. When it is confident, the run continues. When it is not, it can stop at
an approval gate. That is a control-flow decision a language model cannot
express, because a language model cannot tell you, in a number your code can
read, that it is unsure.

## What a decider is

A **decider** is not an LLM. It does not generate text. You declare the
answer space in advance (`noul`, `choice`, `score`), so a decider cannot
return an undeclared value. There is no JSON to parse and no parse-failure
mode that silently unlabels a batch.

ReadyAgents ships two implementations:

| Name | Needs a key | What it is |
| --- | --- | --- |
| `jev` | `TYPESAFE_API_KEY` | TypeSafe System One over HTTP |
| `shim` | whatever the configured LLM needs | Keyless fallback so the **workflow format** stays runnable |

They are not equivalent. The shim has no calibrated confidence.

The Python names live at `readyagents.decide` (`get_decider`, `Question`,
`Decision`). They are **not** in the top-level `readyagents` public contract
while this extra is experimental.

### CLI name

The CLI already has `readyagents decide` for **external approval injection**
(a signed file that unpauses a gate). The new node type is also called
`decide`. They live in different namespaces (a CLI verb vs a YAML `type:`).
**We keep `type: decide`.** Renaming after this ships would be a breaking
change. The CLI verb is unchanged.

## `type: decide`

```yaml
- id: triage
  type: decide
  state: "{{message}}"
  questions:
    department:
      type: choice
      instructions: "Which team should handle this"
      criteria:
        billing: "Payment or subscription issues"
        technical: "Bugs or integration problems"
        sales: "Pricing or account questions"
    is_urgent:
      type: noul
      instructions: "The message conveys urgency or time-sensitivity"
  min_confidence: 0.85
  on_low_confidence: human_review
  route_on: department
  routes:
    billing: billing_queue
    technical: page_oncall
    sales: sales_inbox
  default: human_review
```

Worked example: [`examples/decide_triage.yaml`](../examples/decide_triage.yaml).
Omit `decider:` and the registry uses `shim`. That keyless path is not
calibration: it reaches `human_review` and completes after
`--approve human_review`. Mechanism cases live in
[`examples/eval/decide_triage/`](../examples/eval/decide_triage/README.md).

`readyagents validate` catches question-shape errors and `routes` keys that
are not declared criteria — no key and no network required.

## Question types

| `type` | `criteria` | Answer |
| --- | --- | --- |
| `noul` | omitted, or `{true, false}` descriptions | probability the statement is true (0–1) |
| `choice` | **mapping** `{key: description}`, 2–255 options | one declared key |
| `score` | **list** of ordered level descriptions, 2–255 | fractional expected level |

The mapping-vs-list asymmetry is the vendor's. The schema validator enforces
it. `score` is **not** directly routable; read the value in a `condition`
node.

## Routing

Two modes, mutually exclusive:

- **Choice:** `route_on` + `routes` + `default`. Unknown values take `default`.
- **Boolean:** `route_on` pointing at a `noul` question, plus `then` / `else`,
  optional `threshold` (default 0.5).

`min_confidence` + `on_low_confidence` (a node id, typically `approval`, or
`fail`) is evaluated **first**. A low-confidence answer never silently takes
a `routes:` branch.

## Confidence

**Confidence is a margin, not P(correct).** It measures distance from the
decision boundary, rescaled to 0–1. It does not mean "this answer is right
with probability X".

For a `noul` at threshold `t=0.5`:

- `noul=0.01` → confidence `0.98`
- `noul=0.99` → confidence `0.98`
- `noul=0.5` → confidence `0.0`

A `False` with probability `0.01` is *far* from a coin flip, so the margin is
high. That is not "98% chance the answer is correct".

Thresholds must be calibrated **per action**, not set globally. The harness
is [`examples/eval/decide_triage/`](../examples/eval/decide_triage/README.md):

1. Write 20–50 labelled cases from your own data.
2. Run the suite at several thresholds. The CLI cannot sweep thresholds. A
   shell loop is enough: copy the workflow, set `min_confidence`, and run
   `readyagents eval` on each copy.

   ```bash
   for t in 0.50 0.70 0.85 0.95; do
     echo "min_confidence=$t"
     # set min_confidence on a copy of the workflow, then:
     # readyagents eval examples/eval/decide_triage
   done
   ```

3. Read the tradeoff: raising `min_confidence` sends more tickets to a human
   and fewer wrong ones through.
4. Pick the threshold for the cost of being wrong on **that action** — low if
   the step is reversible, high if it is irreversible.

A keyless pass of that suite proves the mechanism (the human gate fires, and
route labels match the deterministic shim heuristic only on workflows that
omit `min_confidence`). It does not validate a threshold. With `min_confidence`
set, the shim treats every answer as low-confidence, so the loop above cannot
tune the shim.

**`min_confidence` is a quality control, not a security control.** An injected
state can produce a confident wrong label. Never use a decide node as the
sole gate on an irreversible action. Defence in depth, not a solution to
prompt injection.

## Deciders

- `decider: jev` (default model **pinned** `jev-1.13.0`). Aliases
  `jev-latest` / `jev-preview` are accepted but warn when `min_confidence` is
  set, because aliases move.
- `decider: shim` uses `ctx.llm`. Parse failures and out-of-vocabulary values
  **raise**; they do not become silent `None`.
- Omit `decider:`: `shim` when `TYPESAFE_API_KEY` is unset, `jev` when it is.
  An **explicit** `decider: jev` never falls back.

The `shim` decider has no calibrated confidence. A node with `min_confidence`
running on the shim always takes its low-confidence path. That degrades
**toward the human**, not past them.

## Offline replay

`decide` is `sealed` in the cassette. Record before you can replay offline.
The cassette stores **answers**, not the successor. Editing `routes:` or
raising `min_confidence` and replaying takes the new branch. See
[time-machine.md](time-machine.md).

## Cost

TypeSafe list price at time of writing: **$0.042 per million input tokens.
Output tokens are free.** Early pricing is subsidised. The provider invoice
is authoritative; these figures are not a ReadyAgents measurement. Classify
remainder with a decider is **one request per row**. See [cost.md](cost.md).

There is no decider result cache in this release. The cassette already
replays exact decisions.

## Sovereign mode

`decider: jev` is a hosted call and is refused under `--sovereign`.
`decider: shim` over a local model (for example Ollama) is the
sovereign-compatible path. See [sovereign.md](sovereign.md).

## Limits and honesty

- 64k tokens per request; 32k budget for state + longest question; 255 options
- Text only. No local weights. Hosted API only.
- **Vulnerable to adversarial text injection.** Requires deterministic checks
  alongside guard logic.
- TypeSafe reports large speed and cost ratios versus LLMs on its own
  evaluations. Those figures are self-tested and not independently verified.
  ReadyAgents does not repeat them as measurements.
- Do not write "never hallucinates". The true claim is: the answer space is
  declared in advance, so a decider cannot return an undeclared value.

## Open questions (unverified)

These need a live `TYPESAFE_API_KEY`. They are implemented defensively, not
guessed:

- **Q4 — error body shape and rate-limit headers.** Non-2xx bodies are treated
  as opaque. `DecideError` carries HTTP status and a truncated, redacted
  excerpt. We do not pattern-match on error text.
- **Q5 — is `confidence` ever returned for `noul`?** Treated as optional.
  When omitted, `Answer.effective_confidence()` derives the margin.

## Calibrating with `readyagents eval`

Use the four steps in [Confidence](#confidence). The shipped suite is
[`examples/eval/decide_triage/`](../examples/eval/decide_triage/README.md).
Point `readyagents eval` at your own labelled cases when you have a real
decider. This page does not invent a global number, and a keyless pass does
not validate a threshold.

## Configuration

`TYPESAFE_API_KEY`, `TYPESAFE_BASE_URL` (default `https://api.typesafe.ai`),
`TYPESAFE_SYSTEMONE_PATH` (default `/v1/systemone`). See
[configuration.md](configuration.md).
