---
name: I ran this
about: Tell us you ran the core. Not a feature request. Not a launch signup.
title: "I ran this: "
labels: ["run-report"]
---

## What I ran

- Command:
- Python version:
- OS:
- Install path: `pip install readyagentsdev` / clone / other

## Keyless smoke (Current 2.0.4)

Preferred (PyPI):

```bash
pip install readyagentsdev
readyagents new f --from-example calc_pipeline
readyagents run f/workflow.yaml
```

From a clone of this repo, the same graph is `readyagents run examples/calc_pipeline.yaml`.

Did the keyless smoke work?

- [ ] Yes
- [ ] No

If no, paste the last 20 lines of output.

## Approval → resume (optional)

Exit **2** means paused for approval — not a crash. Resume with `readyagents resume <run_id> --approve <gate>`.

```bash
readyagents new g --from-example approval_gate
readyagents run g/workflow.yaml          # expect exit 2 / paused
readyagents resume <run_id> --approve gate
```

- [ ] I hit an approval pause (exit 2) and resumed with `--approve`
- [ ] Skipped

## Anything else I tried

(optional)

## What broke or felt wrong

(optional — one thing is enough)
