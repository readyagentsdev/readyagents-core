# Benchmark harness

`readyagents bench run` produces **reproducible offline numbers** for the
engine and the workflow — not a claim about model intelligence.

```bash
readyagents bench run --offline --json
readyagents bench run --offline --out results.json
readyagents bench compare results.json --baseline baselines/bench_offline.json
readyagents bench compare --models mock:a,mock:b --scenario classify --json
readyagents bench compare --workflows a.yaml,b.yaml --input text=hello --json
```

A repository that never runs bench is unchanged.

## Offline by default

The shipped suite lives in `examples/bench/`. Each scenario includes a
**synthetic** cassette. Offline runs use cassette replay, spend `$0`, and
install a socket guard so they cannot talk to the network.

Offline wall-clock is **engine work**, labelled `offline_engine`. Live
end-to-end timing (opt-in `--live`) is labelled `live_e2e`. The two are
never mixed into one number.

## Scenarios

| name | shape |
| --- | --- |
| classify | single-shot classification |
| research | multi-step tools (calc + json_get) |
| approval | approval-gate round trip |
| foreach | foreach batch |
| team | multi-agent team pipeline |
| document | document pages over synthetic JSON |

## Compare and CI

Committed baseline: `baselines/bench_offline.json`. `bench compare` fails on
metric drift (tokens, cost, nodes, tools, success). Wall-clock uses a
declared percent **and** a minimum absolute delta so CI noise is not a
regression.

`--models a,b` runs the same scenario(s) once per ref with `default_model`
bound into the run. The reported `model` is the engine's
`metadata.model.model`, not a CLI sticker. `--workflows a.yaml,b.yaml`
runs both files through eval with the same `--input KEY=VALUE` mapping
and prints side-by-side metrics.

Live mode is refused when `CI` is set unless `--allow-ci-live`. GitHub
Actions runs the offline suite on every push/PR next to
`scripts/bench_batch.py`.

## Method statement

Every JSON result includes hardware, OS, Python, package version, cassette
digests, `offline` vs `live`, and a `reproduce` command.

This is not a hosted leaderboard and it does not compare other frameworks.
