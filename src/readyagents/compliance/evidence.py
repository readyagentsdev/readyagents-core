"""Evidence pack for a run. ReadyAgents produces evidence, not certification."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from readyagents import __version__
from readyagents.audit import read_audit_events, verify_audit_file
from readyagents.compliance.decision import project_decisions
from readyagents.compliance.graph import render_mermaid
from readyagents.errors import ConfigError
from readyagents.workflow.schema import WorkflowSpec
from readyagents.workflow.state import RunState

PACK_README = """# Evidence pack

This directory is a **local evidence pack** produced by ReadyAgents Core.

It is **not** a certificate, **not** a compliance attestation, and **not** legal
advice. ReadyAgents produces reconstructible evidence of a run. Whether that
evidence is sufficient for EU AI Act Articles 12, 13, or 14 is an operator
and counsel decision.

## What this pack contains

- `run.json` — the canonical run record (inputs, per-node outputs, usage).
- `decisions.json` — a projection of each executed node (not a second store).
- `audit.jsonl` — the run's append-only audit slice. Hash chaining is
  tamper-*evident*, not tamper-proof. A local attacker who can rewrite the
  whole file can rewrite the chain.
- `workflow.yaml` — the workflow source used for this pack, content-hashed.
- `graph.mmd` — deterministic Mermaid routing (no execution).
- `evidence.html` — self-contained rendering (no network fetches).
- `manifest.json` — per-file SHA-256, tool version, chain anchor.

## What it does not prove

- That a human *meaningfully* oversaw the system (Article 14 is operational).
- That logs were retained for six months if the operator deleted them.
- That prompts or outputs were never copied elsewhere.
- Legal compliance or certification of any kind.

Redaction ran at pack time. Treat this directory as sensitive: it may still
contain model prompts and outputs.
"""


def write_evidence_pack(
    dest: Path,
    *,
    state: RunState,
    workflow: WorkflowSpec | None,
    workflow_text: str,
    audit_dir: Path,
    redactor: Any = None,
    force: bool = False,
    sign_secret: str | None = None,
) -> Path:
    dest = Path(dest)
    if dest.exists() and not force:
        raise ConfigError(f"Refusing to overwrite existing path: {dest} (pass --force)")
    dest.mkdir(parents=True, exist_ok=True)
    if redactor is None:
        from readyagents.policy import Redactor

        redactor = Redactor()

    record = state.to_record()
    if redactor is not None:
        record = redactor.redact(record)
    audit_rows = read_audit_events(audit_dir, state.run_id)
    if redactor is not None:
        audit_rows = redactor.redact(audit_rows)
    decisions = [
        row.as_dict() for row in project_decisions(state, workflow, audit_rows, redactor=redactor)
    ]
    mermaid = render_mermaid(workflow) if workflow is not None else "flowchart LR\n"
    html = render_evidence_html(state, decisions)
    files = {
        "run.json": json.dumps(record, indent=2, ensure_ascii=False) + "\n",
        "decisions.json": json.dumps(decisions, indent=2, ensure_ascii=False) + "\n",
        "audit.jsonl": "".join(
            json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in audit_rows
        ),
        "workflow.yaml": workflow_text if workflow_text.endswith("\n") else workflow_text + "\n",
        "graph.mmd": mermaid if mermaid.endswith("\n") else mermaid + "\n",
        "evidence.html": html,
        "README.md": PACK_README if PACK_README.endswith("\n") else PACK_README + "\n",
    }
    hashes: dict[str, str] = {}
    for name, text in files.items():
        (dest / name).write_text(text, encoding="utf-8")
        hashes[name] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    chain_anchor = None
    audit_path = Path(audit_dir) / f"{state.run_id}.jsonl"
    if audit_path.is_file():
        report = verify_audit_file(audit_path)
        if audit_rows:
            chain_anchor = audit_rows[-1].get("entry_hash")
        _ = report
    manifest = {
        "tool": "readyagents",
        "version": __version__,
        "generated_at": datetime.now(UTC).isoformat(),
        "run_id": state.run_id,
        "workflow": state.workflow_name,
        "files": hashes,
        "chain_anchor": chain_anchor,
        "coverage": {
            "nodes": len(state.results),
            "audit_events": len(audit_rows),
            "claim": "evidence",
            "not": ["compliance", "certification"],
        },
        "warning": "This pack may contain recorded model prompts and outputs.",
    }
    manifest_text = json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    (dest / "manifest.json").write_text(manifest_text, encoding="utf-8")
    if sign_secret:
        from readyagents.decisions.signing import sign_body

        sig = sign_body(sign_secret, manifest_text.encode("utf-8"))
        (dest / "manifest.json.sig").write_text(sig + "\n", encoding="utf-8")
    return dest


def render_evidence_html(state: RunState, decisions: list[dict[str, Any]]) -> str:
    import html as html_lib

    esc = html_lib.escape
    rows = []
    for item in decisions:
        human = item.get("human") or {}
        rows.append(
            "<tr>"
            f"<td><code>{esc(str(item.get('node_id')))}</code></td>"
            f"<td>{esc(str(item.get('node_type')))}</td>"
            f"<td>{esc(str(item.get('status')))}</td>"
            f"<td>{esc(str(item.get('attempts')))}</td>"
            f"<td><pre>{esc(_preview(item.get('output')))}</pre></td>"
            f"<td>{esc(str(human.get('decision') or '—'))}</td>"
            f"<td>{esc(str(human.get('outcome') or '—'))}</td>"
            f"<td>{esc(str(human.get('actor') or '—'))}</td>"
            "</tr>"
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="referrer" content="no-referrer"/>
<title>Evidence {esc(state.run_id)}</title>
<style>
body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 2rem;
  color: #111; background: #fff; }}
h1 {{ font-size: 1.2rem; }}
.warn {{ background: #fff4d6; padding: .75rem 1rem; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border-bottom: 1px solid #ddd; text-align: left; padding: .4rem; vertical-align: top; }}
pre {{ white-space: pre-wrap; margin: 0; font-size: .85rem; }}
footer {{ margin-top: 2rem; color: #555; font-size: .85rem; }}
</style>
</head>
<body>
<h1>Evidence pack for run <code>{esc(state.run_id)}</code></h1>
<p class="warn">This is evidence, not compliance or certification.
It may contain prompts and outputs.</p>
<p>workflow={esc(state.workflow_name)} status={esc(state.status)}</p>
<table>
<thead><tr><th>Node</th><th>Type</th><th>Status</th><th>Attempts</th><th>Output</th><th>Decision</th><th>Outcome</th><th>Actor</th></tr></thead>
<tbody>
{"".join(rows) or "<tr><td colspan='8'>No decisions.</td></tr>"}
</tbody>
</table>
<footer>Generated by ReadyAgents Core {esc(__version__)}.
Self-contained — no network resources.</footer>
</body>
</html>
"""


def _preview(value: Any, limit: int = 400) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text
