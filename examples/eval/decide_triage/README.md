# decide_triage eval suite

Keyless mechanism suite for `type: decide`. Run:

```bash
readyagents eval examples/eval/decide_triage
```

A keyless pass proves the mechanism, not calibration. It does not tune or
validate a threshold.

- Cases on `route.yaml` omit `min_confidence`. The expected branch is the
  deterministic shim heuristic (token overlap; equal scores keep the first
  declared option). Those labels are not a judgement that the message "really"
  belongs to that department.
- Cases on `../../decide_triage.yaml` set `min_confidence`. The shim then
  treats every answer as low-confidence, so the expected outcome is the human
  gate, including messages that look like a clear department and messages that
  are deliberately ambiguous. Several cases are ambiguous on purpose.

Use your own labelled cases and a real decider when you pick a threshold.
The procedure is in `docs/decisions.md` (Confidence).
