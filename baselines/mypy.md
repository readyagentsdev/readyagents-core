# Mypy baseline

The wheel ships `readyagents/py.typed`. That is a PEP 561 promise: downstream
type checkers should trust these annotations. `mypy` checks `src/readyagents`
on every CI push. Known errors live in [`mypy-baseline.txt`](mypy-baseline.txt)
— one reviewable file, not scattered `# type: ignore` comments.

Generated with mypy 2.3.1 after the two named N-07 fixes: **220 errors in 74
files** (the pre-fix measurement was 223 in 75). Do not pad the file to 223.

`python scripts/typecheck.py` (and the `typecheck` CI job) fails on any error
that is not in that file. It does not fail on baselined ones. If you fix a
baselined error, re-sync:

```bash
mypy src/readyagents --ignore-missing-imports | mypy-baseline sync
```

Do not add `# type: ignore` to hide new debt. Do not loosen annotations. Do
not remove `py.typed`.

## Burn-down order

1. **`union-attr` and `index`** — highest value. Each is a possible runtime
   `NoneType` crash. Independent; fan out.
2. **`attr-defined`** — same class as `RunCoordinator.started_by_kind`: silent
   wrong data if an implicit attribute disappears.
3. **`arg-type` and `assignment`** — mostly genuine, lower severity.
4. The rest.

When a fix reveals a real bug, write the regression test first.
