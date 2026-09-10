"""Atomic text/bytes writes: temp file in the destination dir, then os.replace."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Final
from uuid import uuid4

from readyagents.errors import AtomicWriteError
from readyagents.permissions import restrict_file

_REPLACE_TRIES: Final = 6
_REPLACE_BACKOFF: Final = 0.05
_WINERROR_SHARING: Final = 32


def atomic_write_text(
    path: Path | str,
    text: str,
    *,
    encoding: str = "utf-8",
    newline: str = "\n",
    restrict: bool = True,
) -> Path:
    """Write UTF-8 text atomically. ``newline`` is applied to ``\\n`` in ``text``."""
    payload = text.replace("\n", newline) if newline != "\n" else text
    data = payload.encode(encoding)
    return atomic_write_bytes(path, data, restrict=restrict)


def atomic_write_bytes(
    path: Path | str,
    data: bytes,
    *,
    restrict: bool = True,
) -> Path:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}.{uuid4().hex}.tmp"
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(tmp, flags, 0o600)
        try:
            if restrict:
                restrict_file(tmp)
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        _replace_with_retry(tmp, dest)
    except Exception:
        _unlink_quiet(tmp)
        raise
    finally:
        _unlink_quiet(tmp)
    return dest


def read_text_with_retry(
    path: Path | str,
    *,
    encoding: str = "utf-8",
) -> str:
    """Read UTF-8 text, retrying Windows sharing violations (file in use)."""
    dest = Path(path)
    last: OSError | None = None
    for attempt in range(_REPLACE_TRIES):
        try:
            return dest.read_text(encoding=encoding)
        except OSError as exc:
            last = exc
            if not _is_sharing_violation(exc) or attempt + 1 >= _REPLACE_TRIES:
                raise
            time.sleep(_REPLACE_BACKOFF * (attempt + 1))
    assert last is not None
    raise last


def _replace_with_retry(tmp: Path, dest: Path) -> None:
    last: OSError | None = None
    for attempt in range(_REPLACE_TRIES):
        try:
            os.replace(tmp, dest)
            return
        except OSError as exc:
            last = exc
            if not _is_sharing_violation(exc) or attempt + 1 >= _REPLACE_TRIES:
                break
            time.sleep(_REPLACE_BACKOFF * (attempt + 1))
    _unlink_quiet(tmp)
    name = str(dest)
    raise AtomicWriteError(
        f"Could not atomically replace {name}: the file is in use or locked. "
        f"Close the file and retry."
    ) from last


def _is_sharing_violation(exc: OSError) -> bool:
    winerror = getattr(exc, "winerror", None)
    if winerror == _WINERROR_SHARING:
        return True
    if os.name == "nt" and exc.errno in {13, 11}:
        return True
    return False


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
