# ReadyAgents Core 0.12.0

**Cross-platform CI, `readyagents doctor`, and a portable smoke path.**

Linux was never the only target, but the install and first-run story is now honest on Windows and macOS too: CI runs the matrix across Python 3.11–3.14 on three OSes, a wheel-install job exercises the pip-only first-run flow, and `readyagents doctor` reports platform, Python, extras, workspace writability, permission enforceability, filesystem case sensitivity, loopback availability, and the resolved run-store backend. `python scripts/smoke.py` (and `make smoke`) runs the keyless example set without a POSIX shell. Sandbox path containment and permission claims are accurate per platform, including Windows junctions.

Packs are waitlisted, not for sale.

## Try it

```bash
pip install readyagentsdev==0.12.0
readyagents doctor
python -m readyagents  # or: readyagents new my-flow
```

Or clone https://github.com/readyagentsdev/readyagents-core and `pip install -e .`.
