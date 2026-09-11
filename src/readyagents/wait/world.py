"""Build a WaitWorld from disk. Lazy; no watcher."""

from __future__ import annotations

import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from readyagents.errors import PathError, WaitPathDenied
from readyagents.paths import resolve_within
from readyagents.wait.evaluate import WaitWorld


def load_world(ctx: Any, state: Any, record: Any = None) -> WaitWorld:
    from readyagents.wait.events import list_events

    home = getattr(ctx, "pin_home", None)
    if home is None:
        from readyagents.config import get_settings

        home = get_settings().home_path()
    workspace = Path(getattr(ctx, "workflow_dir", None) or Path.cwd())
    events = list_events(Path(home))
    pending = state.pending if isinstance(state.pending, dict) else {}
    wait = pending.get("wait") if isinstance(pending.get("wait"), dict) else {}
    spec = wait.get("for_file") if isinstance(wait.get("for_file"), dict) else {}
    if not spec and record is not None:
        spec = getattr(record, "for_file", None) or {}
    if not isinstance(spec, dict):
        spec = {}
    files: dict[str, dict[str, Any]] = {}
    raw_path = str(spec.get("path") or "").strip()
    if raw_path:
        files[raw_path] = inspect_file(raw_path, workspace)
    runs: dict[str, str] = {}
    run_spec = wait.get("for_run") if isinstance(wait.get("for_run"), dict) else {}
    if not run_spec and record is not None:
        run_spec = getattr(record, "for_run", None) or {}
    if not isinstance(run_spec, dict):
        run_spec = {}
    other = str(run_spec.get("run_id") or run_spec.get("id") or "").strip()
    if other:
        runs[other] = _run_status(ctx, other)
    return WaitWorld(events=events, files=files, runs=runs)


def inspect_file(raw_path: str, workspace: Path) -> dict[str, Any]:
    workspace = Path(workspace)
    text = str(raw_path).strip()
    declared = Path(text)
    if not declared.is_absolute():
        declared = workspace / declared
    try:
        mode = declared.lstat().st_mode
    except FileNotFoundError:
        mode = None
    except OSError as exc:
        raise WaitPathDenied(str(exc)) from exc
    if mode is not None and stat.S_ISLNK(mode):
        raise WaitPathDenied(f"wait file is a symlink: {raw_path}")
    try:
        path = resolve_within(Path(raw_path), workspace, what="wait file")
    except PathError as exc:
        raise WaitPathDenied(str(exc)) from exc
    if path.is_symlink():
        raise WaitPathDenied(f"wait file is a symlink: {raw_path}")
    exists = path.exists()
    mtime = ""
    if exists:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
    return {"exists": exists, "mtime": mtime, "symlink": False, "path": str(path)}


def _run_status(ctx: Any, run_id: str) -> str:
    store = getattr(ctx, "run_store", None)
    if store is None:
        try:
            from readyagents.config import get_settings
            from readyagents.run_store import open_run_store

            settings = get_settings()
            opened = open_run_store(settings)
            try:
                return str(opened.get(run_id, allow_prefix=True).state.status)
            finally:
                closer = getattr(opened, "close", None)
                if callable(closer):
                    closer()
        except Exception:  # noqa: BLE001
            return ""
    try:
        return str(store.get(run_id, allow_prefix=True).state.status)
    except Exception:  # noqa: BLE001
        return ""
