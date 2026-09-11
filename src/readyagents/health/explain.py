"""Root-cause bundle: confined, redacted, warned, audited. Not a hosted service."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from readyagents.atomic import atomic_write_text
from readyagents.audit import append_audit_event
from readyagents.errors import ConfigError
from readyagents.health.fingerprint import (
    FailureFingerprint,
    fingerprint_from_result,
    normalize_message,
)
from readyagents.health.query import find_runs_for_fingerprint
from readyagents.paths import resolve_within
from readyagents.policy import Redactor
from readyagents.replay.diff import diff_runs
from readyagents.replay.freeze import freeze_run
from readyagents.replay.record import known_secret_values
from readyagents.run_store.base import RunStore
from readyagents.workflow.state import RunState, utc_now

EXPLAIN_WARNING = (
    "WARNING: this bundle contains recorded prompts, errors, and node inputs. "
    "Review it before sharing. Failure data is diagnostic data."
)


def write_explain_bundle(
    fingerprint_id: str,
    store: RunStore,
    *,
    out_dir: Path,
    workspace: Path,
    settings: Any | None = None,
    secrets: list[str] | None = None,
    redactor: Any | None = None,
    auditor: Any | None = None,
    force: bool = False,
    confirm: bool = False,
) -> Path:
    if not confirm:
        raise ConfigError("health explain requires --yes (the bundle is diagnostic data)")
    dest = resolve_within(out_dir, workspace, what="health explain output")
    if dest.exists() and any(dest.iterdir()) and not force:
        raise ConfigError(f"Refusing to overwrite existing path: {dest} (pass --force)")
    dest.mkdir(parents=True, exist_ok=True)
    scrubber = redactor if redactor is not None else Redactor(literals=list(secrets or []))
    extra = list(secrets or [])
    if settings is not None:
        extra.extend(known_secret_values(settings))
    failing = find_runs_for_fingerprint(store, fingerprint_id, secrets=extra, redactor=scrubber)
    if not failing:
        raise ConfigError(f"No runs match fingerprint {fingerprint_id}")
    fp = _fingerprint_on(failing[0], fingerprint_id, secrets=extra, redactor=scrubber)
    last_ok = _last_success(store, fp.node_id, failing[0].workflow_name)
    cassette_excerpt = _cassette_excerpt(failing[0], fp.node_id, scrubber)
    node_inputs = _node_inputs(failing[0], fp.node_id, scrubber)
    error_text = _error_text(failing[0], fp.node_id, scrubber, secrets=extra)
    delta = None
    if last_ok is not None:
        delta = diff_runs(last_ok, failing[0], redactor=scrubber)
    files = {
        "README.md": _readme(fp),
        "fingerprint.json": json.dumps(fp.as_dict(), indent=2, ensure_ascii=False) + "\n",
        "runs.json": json.dumps(
            [
                {
                    "run_id": state.run_id,
                    "status": state.status,
                    "started_at": state.started_at,
                    "finished_at": state.finished_at,
                    "errors": [
                        normalize_message(str(item), secrets=extra, redactor=scrubber)
                        for item in (state.errors or [])
                    ],
                }
                for state in failing
            ],
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        "node_inputs.json": json.dumps(node_inputs, indent=2, ensure_ascii=False) + "\n",
        "error.txt": error_text + ("\n" if not error_text.endswith("\n") else ""),
        "cassette.json": json.dumps(cassette_excerpt, indent=2, ensure_ascii=False) + "\n",
        "last_success.diff.json": json.dumps(delta or {}, indent=2, ensure_ascii=False) + "\n",
    }
    for name, body in files.items():
        atomic_write_text(dest / name, body, encoding="utf-8", newline="\n")
    if auditor is not None:
        auditor(
            "health_explain",
            fingerprint=fingerprint_id,
            path=str(dest),
            runs=[state.run_id for state in failing],
        )
    else:
        home = getattr(settings, "home_path", lambda: workspace)()
        from readyagents.audit import audit_dir_for

        append_audit_event(
            audit_dir_for(Path(home)),
            {
                "event": "health_explain",
                "run_id": failing[0].run_id,
                "fingerprint": fingerprint_id,
                "path": str(dest),
            },
        )
    return dest


def offer_freeze(state: RunState) -> str:
    return f"readyagents runs freeze {state.run_id} --out fixtures/{state.run_id}"


def discover_fixtures(root: Path, *, cap: int = 64) -> dict[str, str]:
    """Bounded scan for fingerprint.json sidecars next to freeze fixtures."""
    found: dict[str, str] = {}
    if not root.is_dir():
        return found
    scanned = 0
    for path in root.rglob("fingerprint.json"):
        scanned += 1
        if scanned > cap:
            break
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        ident = ""
        if isinstance(data, dict):
            ident = str(data.get("id") or "")
        if ident and ident not in found:
            found[ident] = str(path.parent)
    return found


def freeze_from_run(
    state: RunState,
    *,
    out_dir: Path,
    workspace: Path,
    settings: Any | None = None,
) -> Path:
    from readyagents.replay.cassette import Cassette

    raw = (state.metadata or {}).get("cassette")
    if isinstance(raw, str) and raw.strip() and Path(raw).is_file():
        cassette = Cassette.load(raw)
    else:
        cassette = Cassette.new(run_id=state.run_id, workflow=state.workflow_name)
    return freeze_run(
        state,
        cassette,
        out_dir=out_dir,
        workspace=workspace,
        secrets=known_secret_values(settings) if settings is not None else None,
        allow_unsealed=True,
    )


def _fingerprint_on(
    state: RunState,
    fingerprint_id: str,
    *,
    secrets: list[str] | None,
    redactor: Any,
) -> FailureFingerprint:
    for result in state.results:
        fp = fingerprint_from_result(result, secrets=secrets, redactor=redactor)
        if fp is not None and fp.id == fingerprint_id:
            return fp
    raise ConfigError(f"Fingerprint {fingerprint_id} not on run {state.run_id}")


def _last_success(store: RunStore, node_id: str, workflow: str) -> RunState | None:
    from readyagents.run_store.base import RunQuery

    for stored in store.list(RunQuery(workflow=workflow, limit=500)):
        if stored.state.status != "succeeded":
            continue
        if any(row.node_id == node_id and row.status == "ok" for row in stored.state.results):
            return stored.state
    return None


def _cassette_excerpt(state: RunState, node_id: str, redactor: Any) -> dict[str, Any]:
    from readyagents.replay.cassette import Cassette

    raw = (state.metadata or {}).get("cassette")
    if not isinstance(raw, str) or not Path(raw).is_file():
        return {}
    cassette = Cassette.load(raw)
    entries = {
        key: redactor.redact(dict(entry))
        for key, entry in cassette.entries.items()
        if node_id in key or str((entry or {}).get("node_id") or "") == node_id
    }
    return redactor.redact({"run_id": cassette.run_id, "entries": entries})


def _node_inputs(state: RunState, node_id: str, redactor: Any) -> dict[str, Any]:
    return redactor.redact(
        {"run_id": state.run_id, "node_id": node_id, "inputs": dict(state.inputs or {})}
    )


def _error_text(
    state: RunState, node_id: str, redactor: Any, *, secrets: list[str] | None = None
) -> str:
    raw = ""
    for row in state.results:
        if row.node_id == node_id and row.error:
            raw = str(row.error)
            break
    if not raw and state.errors:
        raw = str(state.errors[0])
    return normalize_message(raw, secrets=secrets, redactor=redactor)


def _readme(fp: FailureFingerprint) -> str:
    return (
        f"# Root-cause bundle `{fp.id}`\n\n"
        f"{EXPLAIN_WARNING}\n\n"
        f"- class: `{fp.klass}`\n"
        f"- node: `{fp.node_id}`\n"
        f"- written: {utc_now()}\n\n"
        "This is local diagnostic data. It is not a hosted reliability service "
        "and it does not predict failures.\n"
    )
