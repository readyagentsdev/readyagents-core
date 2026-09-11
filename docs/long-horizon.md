# Long-horizon waits

`type: wait` parks a run as **`waiting`** — not `paused` (a human must decide)
and not `running`. Wake is lazy. Core starts **no timer, watcher, or daemon**.
Wake latency is how often you call `readyagents wake` (or the optional
continuous pack's sweep). That is stated, not hidden.

A workflow without wait nodes is unchanged. `approval` / resume / `runs list`
without `--waiting` keep their exact semantics.

## Wait node

```yaml
- id: await_signature
  type: wait
  whichever: first
  until: "72h"                 # required deadline (duration or ISO timestamp)
  for_file: {path: "inbox/{{ id }}.pdf", "on": created}
  for_event: {name: "contract.signed", match: {id: "{{ id }}"}}
  on_deadline: escalate        # fail | continue | escalate | branch
  escalate_to: [account_manager]
  output_key: signal
```

A wait without `until` is a schema error. Unbounded waits are a leak.

Conditions: `until` is the deadline clock. `for_event`, `for_file`, and
`for_run` are optional wakes. `whichever: first` succeeds on the first extra
condition; `all` requires every extra condition before the deadline.
If none of the extras fire in time, `on_deadline` runs.

- `fail` — the run fails
- `continue` — proceed with `default` (never grants an approval)
- `escalate` — reuses the existing approval path (`paused`, not `waiting`)
- `branch` — take `else` / `then`

## Lazy wake

```bash
readyagents wake RUN_ID
readyagents wake --all
readyagents runs list --waiting
readyagents runs timeline RUN_ID
```

Conditions are evaluated when the run is touched: `wake`, `resume`, or a
status/timeline query that loads the record. There is no in-process scheduler.

## Events

```bash
readyagents event contract.signed --payload event.json --signature $HEX
```

Events are HMAC-signed (`READYAGENTS_EVENT_SECRET`) and audited like decisions.
Unsigned or unauthorised events are refused and do not wake. Payloads are
untrusted, taint-marked, and bounded. The same event is idempotent.

## Files, credentials, hygiene

File waits go through the containment helper; a symlink at the watched path is
refused. Brokered credentials are not kept while dormant and are re-brokered
on wake. `runs gc` never deletes a `waiting` run. Long records compact old
node outputs to markers and still replay.

## What this is not

- A scheduler, timer thread, filesystem watcher, or daemon in core
- Hosted control-plane state
- A wake-latency SLA (latency is call frequency)
