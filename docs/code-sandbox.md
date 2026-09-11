# Sandboxed `type: code`

**Unreleased on this checkout — not on the 1.9.0 tag.** Python only. The default
`subprocess` tier defends against accident and careless code. It is **not**
hostile-code-proof. Hostile workloads need the optional `container` pack and
operator-level isolation. ReadyAgents Core does not bundle a container runtime.

```yaml
- id: reshape
  type: code
  isolation: subprocess
  source: |
    result = {"total": sum(inputs["rows"])}
  inputs: {rows: "{{ rows }}"}
  output_schema: {type: object, properties: {total: {type: number}}}
  limits: {cpu_seconds: 5, memory_mb: 512, wall_seconds: 15, output_bytes: 1048576}
  network: false
```

Or take source from another node with `source_from: draft_code`. Inputs are a
JSON object on stdin; the child must assign `result` to a JSON-serialisable
value written to stdout. A schema mismatch is a typed error, not `None`.

## Isolation

| Tier | What it is |
| --- | --- |
| `subprocess` (default) | Separate interpreter, minimal env (no inherited secrets), closed parent fds on POSIX, cwd confined to a per-run temp dir, rlimits where `resource` exists. |
| `container` | Optional pack using the operator's runtime. Core never vendors Docker/Podman. A required container tier **fails closed** if the pack/runtime is missing. |

`readyagents doctor` reports whether OS rlimits are available.

- **`wall_seconds`** is the parent `communicate` timeout. It is not CPU time:
  `time.sleep` under wall with CPU remaining succeeds.
- **`cpu_seconds`** is `RLIMIT_CPU` where the OS accepts it, plus a child
  `process_time` watchdog (sleep does not count).
- **`memory_mb`** is `RLIMIT_AS` on Linux, plus a child RSS watchdog everywhere.
  macOS does not let a process lower `RLIMIT_AS`; Windows has no `resource`.
- **`file_size_bytes`** is `RLIMIT_FSIZE` where the OS accepts it, plus a capped
  `open` in the child.
- **`nproc`** is spawn wrappers (`os.fork` / `os.system` / `subprocess`), not a
  kernel process-count rlimit.

On Windows, CPU / address-space / nproc are not OS rlimits; the child watches
and parent wall/output caps still apply.

## Defaults

- **No network.** `network: true` only allows importing network modules; SSRF
  pinning still belongs to the parent process tools, not a guarantee inside the
  child.
- **Import allowlist** at import time. Default is a stdlib subset (json, math,
  datetime, …) minus process/network/filesystem escape modules (`os`,
  `subprocess`, `ctypes`, `socket`, …). Defence in depth, not the only boundary.
- **Filesystem:** the child may write inside its temp dir. Extra read/write
  roots must stay under the workspace (`resolve_within`). Traversal and
  out-of-grant paths fail closed.
- **Limits:** `cpu_seconds`, `memory_mb`, `wall_seconds`, `output_bytes`,
  `file_size_bytes`, `nproc` — each maps to a distinct typed error.

## Recording and approval

Executed source, stdin, stdout, stderr, exit status, outputs, and the tier
actually used are recorded (source redacted). `readyagents run --record` then
`readyagents runs replay --offline` replays the node **without** executing
code.

Policy `nodes.<id>.require_approval` on a `source_from` node pauses with the
**full** generated source, labelled untrusted attributed text — not an
instruction.

See [examples/code_reshape.yaml](../examples/code_reshape.yaml).
