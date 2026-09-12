"""Pointers, history, and isolated run-store paths per environment."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from readyagents.atomic import atomic_write_text
from readyagents.config import Settings, get_settings
from readyagents.errors import EnvRefused
from readyagents.workflow.state import utc_now


def env_root(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    path = settings.home_path() / "environments"
    path.mkdir(parents=True, exist_ok=True)
    return path


class EnvStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.root = env_root(self.settings)

    def env_dir(self, name: str, *, create: bool = True) -> Path:
        token = str(name or "").strip()
        if not token or "/" in token or ".." in token:
            raise EnvRefused("invalid environment name", reason="name")
        dest = self.root / token
        if create:
            dest.mkdir(parents=True, exist_ok=True)
        return dest

    def runs_dir(self, name: str) -> Path:
        dest = self.env_dir(name) / "runs"
        dest.mkdir(parents=True, exist_ok=True)
        return dest

    def current(self, name: str) -> dict[str, Any] | None:
        return self._read(self.env_dir(name, create=False) / "current.json")

    def previous(self, name: str) -> dict[str, Any] | None:
        return self._read(self.env_dir(name, create=False) / "previous.json")

    def candidate(self, name: str) -> dict[str, Any] | None:
        return self._read(self.env_dir(name, create=False) / "candidate.json")

    def pointer_named(self, name: str, slot: str) -> dict[str, Any] | None:
        token = str(slot or "current").strip().lower()
        if token == "current":
            return self.current(name)
        if token == "previous":
            return self.previous(name)
        if token == "candidate":
            return self.candidate(name)
        raise EnvRefused(
            f"unknown pointer {slot!r} (current|previous|candidate)",
            reason="pointer",
        )

    def set_current(
        self, name: str, pointer: dict[str, Any], *, keep_previous: bool = True
    ) -> None:
        directory = self.env_dir(name)
        existing = self.current(name)
        if keep_previous and existing:
            self._write(directory / "previous.json", existing)
        pointer = dict(pointer)
        pointer.setdefault("deployed_at", utc_now())
        self._write(directory / "current.json", pointer)
        self.append_history(name, {"event": "deploy", **pointer})

    def set_candidate(self, name: str, pointer: dict[str, Any] | None) -> None:
        path = self.env_dir(name) / "candidate.json"
        if pointer is None:
            if path.is_file():
                path.unlink()
            return
        self._write(path, dict(pointer))

    def rollback(
        self,
        name: str,
        *,
        actor: str | None,
        reason: str,
        retain_failed: bool = True,
    ) -> dict[str, Any]:
        prev = self.previous(name)
        if not prev:
            raise EnvRefused(f"environment {name!r} has no previous release", reason="rollback")
        current = self.current(name)
        directory = self.env_dir(name)
        if retain_failed and current:
            # Manual rollback: keep the abandoned pin as previous so an
            # authorised operator can still inspect or re-enter it.
            self._write(directory / "previous.json", current)
        pointer = dict(prev)
        pointer["rolled_back_at"] = utc_now()
        pointer["rollback_reason"] = reason
        pointer["rollback_actor"] = actor
        self._write(directory / "current.json", pointer)
        self.set_candidate(name, None)
        self.append_history(
            name,
            {
                "event": "rollback",
                "reason": reason,
                "actor": actor,
                "release": pointer.get("digest"),
                "from": (current or {}).get("digest"),
                "retain_failed": retain_failed,
            },
        )
        return pointer

    def append_history(self, name: str, event: dict[str, Any]) -> None:
        path = self.env_dir(name) / "history.jsonl"
        row = dict(event)
        row.setdefault("ts", utc_now())
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def history(self, name: str, *, limit: int = 50) -> list[dict[str, Any]]:
        path = self.env_dir(name, create=False) / "history.jsonl"
        if not path.is_file():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        rows: list[dict[str, Any]] = []
        for line in lines[-max(1, int(limit)) :]:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
        return rows

    def list_names(self, declared: Iterable[str] | None = None) -> list[str]:
        names = {str(item) for item in (declared or []) if str(item).strip()}
        if self.root.is_dir():
            for item in self.root.iterdir():
                if item.is_dir() and not item.name.startswith("."):
                    names.add(item.name)
        return sorted(names)

    def status(self, name: str) -> dict[str, Any]:
        cur = self.current(name)
        prev = self.previous(name)
        cand = self.candidate(name)
        return {
            "environment": name,
            "current": cur,
            "previous": prev,
            "candidate": cand,
            "runs_dir": str(self.runs_dir(name)),
        }

    def _read(self, path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _write(self, path: Path, data: dict[str, Any]) -> None:
        atomic_write_text(
            path,
            json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
