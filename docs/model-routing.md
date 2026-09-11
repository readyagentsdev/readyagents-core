# Model routing

Opt-in policy that maps a node, role, or tag to a model using **declared**
attributes (price table, capability matrix, local vs hosted). Not a hosted
router, not quality inference, and not a benchmark claim.

Without a `routing:` block, model selection is unchanged: `node.model`,
`default_model`, then `fallback_models`.

## Providers

OpenAI, Anthropic, and OpenAI-compatible endpoints stay as they are. Gemini,
Bedrock, and Vertex are optional extras. Missing extra is the same install hint
as OpenAI/Anthropic (`pip install 'readyagentsdev[gemini]'`, `[bedrock]`,
`[vertex]`). They are **not** in `[all]`.

| Extra | Import | Typical keys |
| --- | --- | --- |
| `gemini` | `google.genai` | `GEMINI_API_KEY` / `GOOGLE_API_KEY` |
| `bedrock` | `boto3` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION` |
| `vertex` | `vertexai` | `VERTEX_PROJECT` / `GOOGLE_CLOUD_PROJECT` |

Credentials are loaded only for the provider that was selected. Adding a
Bedrock extra does not grant AWS keys to every node. Routing cannot widen
sovereign egress.

## Capability matrix

`src/readyagents/llm/capabilities.json` ships with the wheel. It records, per
model: context window, tool-calling, structured output, media, streaming, and
whether the model is **local**. Operators may override with
`READYAGENTS_CAPABILITY_MATRIX` or `routing.capability_matrix`. The file is
schema-validated. A malformed override is refused. A stale override (older than
`warn_after_days`) is refused. The bundled file may warn when old; it is not
silently rewritten.

An unsupported request under a routing rule is a typed `CapabilityError` /
`RoutingError` **before** `complete()` — no spend.

## Policy

```yaml
routing:
  version: 1
  rules:
    - match: {node_tag: classify}
      strategy: cheapest_capable
      require: {tool_calling: true}
      pool: ["openai:gpt-4o-mini", "openai:gpt-4o"]
    - match: {taint: untrusted}
      strategy: local_only
      pool: ["ollama:llama3.2"]
    - match: {node: draft}
      pin: "anthropic:claude-sonnet-4-5"
  budgets:
    classify: {max_cost_usd: 0.05}
```

First matching rule wins. Strategies:

- `cheapest_capable` — sort by the shipped price table among models that satisfy
  `require`
- `fastest` — sort by declared `latency_class` on the matrix
- `highest_quality` — sort by declared `quality_class` on the matrix (not
  inferred from outputs, not a benchmark)
- `local_only` — never a hosted provider, including through fallback
- explicit `pin:` — that ref only

A rule that cannot be satisfied is a typed error, never a silent downgrade.

Nodes may set `tags:` and `role:` for `match.node_tag` / `match.role`.

## Sensitivity

`local_only` and `match.taint: untrusted` are residency controls. Tainted or
memory-derived content is never sent to a hosted provider under those rules,
including via `fallback_models`. If taint cannot be determined, it is treated
as untrusted.

## Recording

When a policy fires, the run record stores `metadata.routes` and an additive
`route` object on `node_results`: model used, rule, strategy, fallback, taint.
Cassettes store the same object on the LLM entry. Offline replay reproduces
the route and never falls through to a live `complete()`.

## Route budgets and health

`routing.budgets` keys are node id, tag, or rule id. They apply **on top of**
the run-level cap (`BudgetExceeded` vs `RouteBudgetExceeded`). An open circuit
breaker removes that model from the routing pool rather than retrying into it.

## CLI

```bash
readyagents models list
readyagents models show openai:gpt-4o-mini
readyagents models route workflow.yaml --node draft --explain
```

`list` / `show` / `route` are dry: no provider call, no API key. `route --explain`
uses the same `select_route` function as a real run.

See [configuration](configuration.md) and [cost](cost.md).
