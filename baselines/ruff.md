# Ruff S / SIM / PTH / RUF — deferred findings

`select` includes `S`, `SIM`, `PTH`, and `RUF` on top of `E,F,I,UP,B`.
`ruff check src tests` is green because remaining hits are recorded here
and ignored in `[tool.ruff.lint]` / per-file-ignores — one reviewable
place, not a spray of `# noqa` in source. Auto-fixable hits from this
pass were applied.

## Global ignore (style nits and bandit false positives)

| Code | Why deferred |
|---|---|
| SIM102, SIM103, SIM105, SIM108, SIM115, SIM117, SIM222, SIM401 | Style. Burn down after mypy `union-attr`. |
| RUF001, RUF003, RUF005, RUF007, RUF012, RUF015, RUF034, RUF043, RUF046, RUF059 | Style / unicode / unpack. Same burn-down. |
| S101 | Invariant `assert` in src; tests use assert as the tool. |
| S104 | Literals in bind-all **refusal** checks (`0.0.0.0`). |
| S105, S106, S107 | Env-var names (`token_env=...`) and error strings (`"invalid token"`), not secrets. |
| S110, S112 | `except: pass/continue` — triage with mypy `union-attr`. |
| PTH101, PTH105, PTH123, PTH208 | `os.*` → `Path` migration. Independent of the type gate. |

## Per-file security codes (reviewed, not silent)

| File | Code | Why not a drive-by fix |
|---|---|---|
| `src/readyagents/__init__.py` | RUF022 | `__all__` leads with `__version__` to match `docs/stability.md`. |
| `src/readyagents/packs/loader.py` | S102 | Pack import uses `exec` by design. |
| `src/readyagents/skills/install.py` | S202 | `tarfile.extractall` is confined; filter rewrite is a later change. |
| `src/readyagents/mcp/probe.py` | S310 | `urlopen` after scheme allowlist. |
| `src/readyagents/simulate/generate.py` | S311 | Simulation RNG, not cryptography. |
| `src/readyagents/sovereign/bundle.py` | S603 | `subprocess` of a pinned interpreter. |
| `src/readyagents/code/runner.py` | S607 | Partial executable path for the code node. |

Tests additionally ignore S102/S108/S310/S603/S607 (fixtures, tmp paths, subprocess helpers).
