# Conversational sessions

**Unreleased on this checkout — not on the 1.9.0 tag.** A session is a
**sequence of durable runs**, not a socket holding state in memory. One-shot
workflows that never converse are unchanged.

```yaml
conversation:
  max_turns: 20
  deadline: 24h
  on_expire: close
  compaction: {max_turns: 10}
nodes:
  - id: greet
    type: converse
    say: "Hi — what's the order number?"
    output_key: reply
```

```bash
readyagents sessions start examples/converse_order.yaml --json
readyagents sessions reply SESSION --text ORD-9 --json
readyagents sessions list|show|close|replay|freeze
readyagents serve chat examples/converse_order.yaml --port 8795 --widget
```

`serve chat` is **loopback by default**, token-protected (`READYAGENTS_CHAT_TOKEN`),
and foreground. Public bind requires `--allow-public-bind` and prints a warning.
There is no hosted chat product, no widget CDN, no telemetry.

## Turns are runs

Each converse pause/resume is checkpointed as an ordinary run in the existing
run store, linked from the session object (`.readyagents/sessions/<id>.json`).
Restart mid-conversation loses nothing. Session ids are unguessable; missing
and unauthorised chat lookups return the same 404.

## Bounds

`max_turns`, conversation `budget`, per-turn `turn_timeout`, and `deadline`
each stop with a **distinct typed reason**. Conversations are not unbounded.
History compaction is declared and records what was dropped.

## Memory

Scope `session:<id>` is first-class. It is **cleared on close** unless
`conversation.memory.promote` names a durable `subject:` scope. Undeclared
promotion is refused. There is no implicit cross-session memory.

## Handoff

`mode: human_agent` is approval-shaped. History is shown as **untrusted,
attributed** content. Human turns record actor, role, and signature status.

## Barge-in and streaming

Barge-in cooperatively cancels an in-flight turn, marks partial assistant
output **superseded**, and continues with the new input. User-facing streams
go through the existing incremental redactor so a secret split across chunks
is not emitted then retracted.

## Replay and freeze

`sessions replay` walks each turn's run. `sessions freeze --out DIR` writes a
multi-turn fixture that `readyagents eval` can run.

## Voice

Partial input, barge-in, and streaming output are the turn contract an
optional voice/telephony pack can drive. **Core has no audio, codec, SIP, or
WebRTC dependency.** No presence, typing indicators, read receipts, or rooms:
one user, one session, plus an optional human agent.
