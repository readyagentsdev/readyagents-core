# Platform support

ReadyAgents Core is tested on **Linux, macOS, and Windows** across **Python 3.11–3.14**.
CI is the oracle: `ubuntu-latest` runs 3.11–3.14; macOS and Windows run the oldest and newest
Python (3.11 and 3.14) as of 2026-09-09 to bound runner time. A separate job installs the
**wheel** (not the clone) on each OS and walks `readyagents new` / `validate` / `run --dry-run` /
`doctor`.

## What is verified

- Workspace sandbox: `resolve_within` resolve-then-compare, with runtime-probed case sensitivity
- Atomic run-record / cache / `write_file` via temp+`os.replace` in the destination directory
- SQLite run-store with WAL (local disk only)
- Loopback MCP HTTP and the localhost approval UI
- MCP stdio child spawn **without** shell interpretation (`.cmd` / `.bat` / `cmd.exe` refused)
- Keyless smoke (`python scripts/smoke.py` / `make smoke`)

## Known limitations

- **Windows file modes.** `chmod 0o600` is close to a no-op. Restrictive permissions are
  **not enforceable** through the standard library. `readyagents doctor` reports
  `home.permissions_enforceable`. Do not store API keys or PII expecting POSIX modes to hide them
  on Windows. No `pywin32` ACL helper is bundled.
- **Network filesystems.** SQLite on SMB, NFS, or cloud-sync folders is unsupported (locking).
- **Symlinks / junctions.** Creation may need Developer Mode on Windows. Containment always
  resolves first; a reparse point that `is_symlink()` misses is still compared after resolve.
- **Path forms.** Reserved device names, ADS (`file:stream`), trailing dots/spaces, UNC and
  `\\?\` prefixes (unless the workspace root is one), drive-relative `C:file.txt`, and 8.3 names
  that resolve outside the root are refused. Fail closed; no sandbox rule is relaxed for a platform.
- Platforms not in CI (BSD, Termux, WSL1, 32-bit) are not claimed.

## Reporting a platform bug

Attach `readyagents doctor --json` output to an
[I-ran-this](https://github.com/readyagentsdev/readyagents-core/issues/new?template=i-ran-this.md)
issue. That report is read-only: no network, no LLM, no run record.
