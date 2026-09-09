from __future__ import annotations

from pathlib import Path

import pytest

from readyagents.atomic import atomic_write_text
from readyagents.errors import AtomicWriteError
from readyagents.permissions import FILE_MODE, restrict_file
from readyagents.workflow.state import RunState, persist_run


def test_round_trip_utf8_and_no_temp_residue(tmp_path: Path) -> None:
    dest = tmp_path / "out.json"
    atomic_write_text(dest, '{"n": "café"}\n', encoding="utf-8", newline="\n")
    assert dest.read_text(encoding="utf-8") == '{"n": "café"}\n'
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []


def test_failure_removes_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "out.txt"

    def boom(src: Path, dst: Path) -> None:  # noqa: ARG001
        raise OSError("replace failed")

    monkeypatch.setattr("readyagents.atomic.os.replace", boom)
    with pytest.raises(AtomicWriteError, match="out.txt"):
        atomic_write_text(dest, "hello\n")
    assert list(tmp_path.glob("*.tmp")) == []
    assert not dest.exists()


def test_sharing_violation_retries_then_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "locked.txt"
    dest.write_text("old", encoding="utf-8")
    calls = {"n": 0}

    def sharing(src: Path, dst: Path) -> None:  # noqa: ARG001
        calls["n"] += 1
        err = OSError("sharing violation")
        err.winerror = 32  # type: ignore[attr-defined]
        err.errno = 13
        raise err

    monkeypatch.setattr("readyagents.atomic.os.replace", sharing)
    monkeypatch.setattr("readyagents.atomic._REPLACE_BACKOFF", 0)
    with pytest.raises(AtomicWriteError, match="locked.txt"):
        atomic_write_text(dest, "new\n")
    assert calls["n"] >= 2
    assert list(tmp_path.glob("*.tmp")) == []


def test_temp_not_world_readable_before_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "secret.txt"
    seen: list[int] = []

    real_replace = __import__("os").replace

    def wrap(src: Path, dst: Path) -> None:
        import stat

        mode = stat.S_IMODE(Path(src).stat().st_mode)
        seen.append(mode)
        real_replace(src, dst)

    monkeypatch.setattr("readyagents.atomic.os.replace", wrap)
    atomic_write_text(dest, "secret\n")
    assert seen
    outcome = restrict_file(dest)
    if outcome.enforceable:
        assert (seen[0] & 0o177) == 0
        assert (seen[0] & FILE_MODE) == FILE_MODE or seen[0] == FILE_MODE


def test_persist_run_uses_atomic_helper(tmp_path: Path) -> None:
    state = RunState.start("demo", {"a": 1})
    path = persist_run(state, tmp_path)
    assert path.is_file()
    assert path.read_text(encoding="utf-8").endswith("\n")
    assert list(tmp_path.glob("*.tmp")) == []
