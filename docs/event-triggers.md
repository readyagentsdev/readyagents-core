# Event triggers

A workflow may declare a `triggers:` block: accepted event shapes, the mapping
from event to inputs, a required idempotency key and window, a per-trigger
budget, a concurrency ceiling, and whether a signature is required.

**Core validates and decides. Core starts no listener, webhook server, file
watcher, queue poller, scheduler, thread, or daemon.** Listeners live in the
optional continuous pack, which must call the same start decision
(`readyagents.triggers.decide.decide_trigger` / `fire_webhook` /
`fire_file` / `fire_queue` / `fire_schedule`). Governance cannot drift by source.

Delivery is **at-least-once with idempotent handling**, not exactly-once. A
redelivered event with the same idempotency key inside the window returns the
original run. Outside the window a new run starts.

## Contract

```yaml
triggers:
  - name: support_email
    accepts: {kind: webhook, schema: {type: object, required: [message_id, body]}}
    require_signature: true
    inputs: {ticket_id: "{{ event.message_id }}", text: "{{ event.body }}"}
    idempotency_key: "{{ event.message_id }}"
    idempotency_window: 24h
    budget: {max_cost_usd: 0.20}
    concurrency: 4
    on_ceiling: defer   # drop | defer
```

`on_ceiling: drop` refuses and dead-letters when the concurrency limit is
reached. `defer` queues on a bounded in-process queue (32); when that fills,
the event is dropped and dead-lettered. A firehose degrades predictably.

## CLI

```bash
readyagents triggers list examples/trigger_support.yaml
readyagents triggers show support_email examples/trigger_support.yaml
readyagents triggers test support_email examples/trigger_support.yaml --payload event.json
readyagents triggers events
```

`triggers test` maps without starting a run.

## Security

Events are untrusted. Payloads are taint-marked `source=event` and cannot
cause a denied tool to run. Size, JSON depth, and rate caps apply to the raw
bytes **before** JSON parse. When `require_signature: true`, missing or
tampered HMAC signatures are refused and audited. Dead letters record the
reason and are replayable — replay of a hostile event still refuses.

## Webhook posture

The default is loopback. Core does not bind a public webhook. An
operator-exposed webhook in the optional pack is the operator's decision and
must not be reverse-proxied onto the public internet without their own
authn/authz.

A workflow without `triggers:` is unchanged.
