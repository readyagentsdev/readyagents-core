from __future__ import annotations

from pathlib import Path

from readyagents.permissions import (
    FILE_MODE,
    permissions_enforceable,
    restrict_dir,
    restrict_file,
)


def test_restrict_file_posix_or_honest_windows(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("x", encoding="utf-8")
    outcome = restrict_file(path)
    assert outcome.requested == FILE_MODE
    enforceable = permissions_enforceable(tmp_path)
    assert outcome.enforceable is enforceable or outcome.enforceable is False
    if enforceable:
        assert outcome.applied is not None
        assert (outcome.applied & 0o177) == 0


def test_restrict_dir(tmp_path: Path) -> None:
    outcome = restrict_dir(tmp_path)
    assert outcome.requested == 0o700
    assert outcome.path == tmp_path
