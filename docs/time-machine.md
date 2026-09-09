# Run Time Machine

Every ReadyAgents run already persists after every node. The time machine
wires that persistence to a content-addressed cassette so a run can be
replayed offline, forked from a node, diffed, and frozen into a
`readyagents eval` fixture.

Recording is **opt-in**. A cassette holds full prompts and completions.

## Worked example (no keys)

```bash
readyagents run examples/calc_pipeline.yaml --record --json
readyagents runs replay RUN_ID --offline --json
readyagents runs freeze RUN_ID --out /tmp/frozen-calc --allow-unsealed
readyagents eval /tmp/frozen-calc/case.yaml
```

`calc_pipeline` has no LLM. The cassette still exists so `--offline` has a
file to load; `calc` nodes are **recomputed**. `now` is sealed only when
recorded.

## Verbs

| Command | What it does |
| --- | --- |
| `readyagents run PATH --record` | Write `$READYAGENTS_HOME/cassettes/<run_id>.json` |
| `readyagents runs replay RUN_ID --offline` | Re-execute from the cassette. No keys, no network |
| `readyagents runs fork RUN_ID --from-node NODE` | New run from that checkpoint. Parent is unchanged |
| `readyagents runs diff A B` | First divergence, redacted diff, usage delta |
| `readyagents runs freeze RUN_ID --out DIR` | `cassette.json` + `case.yaml` + `README.md` |

## Determinism report

`runs replay --offline --json` includes:

```json
"determinism": {
  "sealed": ["draft"],
  "recomputed": ["format"],
  "unsealable": ["stamp"],
  "misses": []
}
```

- **sealed** — served from the cassette
- **recomputed** — deterministic (`transform`, `calc`, `condition`, …)
- **unsealable** — `now` / `http_get` / pack tools without a sealed entry
- **misses** — cassette lookup failed (never a live fallback)

An unclassified pack tool defaults to **unsealable**. A run is never called
deterministic just because the model part was sealed.

## Security

Cassettes are untrusted input: version-checked, size-bounded, never used to
select a code path. Replay is labelled `replay` / `replayed_from` on the new
record. Review a frozen fixture before committing it. See SECURITY.md.
