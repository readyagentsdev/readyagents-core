"""Session persistence beside the existing run store (JSON files, atomic)."""

from __future__ import annotations

import json
from pathlib import Path

from readyagents.atomic import atomic_write_text
from readyagents.config import Settings, get_settings
from readyagents.errors import SessionRefused
from readyagents.sessions.model import Session


def sessions_dir(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    path = settings.home_path() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


class SessionStore:
    def __init__(self, root: Path | None = None, *, settings: Settings | None = None) -> None:
        self.root = Path(root) if root is not None else sessions_dir(settings)

    def path_for(self, session_id: str) -> Path:
        name = str(session_id or "").strip()
        if not name or "/" in name or "\\" in name or ".." in name:
            raise SessionRefused("invalid session id", reason="id")
        return self.root / f"{name}.json"

    def save(self, session: Session) -> None:
        session.touch()
        dest = self.path_for(session.session_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            dest,
            json.dumps(session.as_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def get(self, session_id: str) -> Session:
        path = self.path_for(session_id)
        if not path.is_file():
            raise SessionRefused("session not found", reason="missing")
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise SessionRefused("session record is malformed", reason="malformed")
        return Session.from_dict(raw)

    def list(self, *, limit: int = 50) -> list[Session]:
        rows: list[Session] = []
        files = sorted(self.root.glob("s*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(raw, dict):
                rows.append(Session.from_dict(raw))
            if len(rows) >= max(1, int(limit)):
                break
        return rows

    def delete(self, session_id: str) -> None:
        path = self.path_for(session_id)
        if path.is_file():
            path.unlink()
