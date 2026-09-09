"""Restrictive file/dir permissions with honest per-platform enforceability."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

FILE_MODE = 0o600
DIR_MODE = 0o700


@dataclass(frozen=True)
class PermissionOutcome:
    path: Path
    requested: int
    applied: int | None
    enforceable: bool


def permissions_enforceable(root: Path) -> bool:
    """True when chmod bits other than owner are actually cleared on ``root``."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    probe = root / f".ra_perm_{uuid4().hex}"
    try:
        probe.write_bytes(b"x")
        os.chmod(probe, FILE_MODE)
        mode = stat.S_IMODE(probe.stat().st_mode)
        return (mode & 0o177) == 0
    except OSError:
        return False
    finally:
        probe.unlink(missing_ok=True)


def restrict_file(path: Path) -> PermissionOutcome:
    return _restrict(Path(path), FILE_MODE)


def restrict_dir(path: Path) -> PermissionOutcome:
    return _restrict(Path(path), DIR_MODE)


def _restrict(path: Path, requested: int) -> PermissionOutcome:
    applied: int | None = None
    try:
        os.chmod(path, requested)
        applied = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return PermissionOutcome(path=path, requested=requested, applied=None, enforceable=False)
    enforceable = (applied & 0o177) == 0 if requested == FILE_MODE else (applied & 0o077) == 0
    if not enforceable:
        # Windows chmod is often a no-op; do not pretend owner-only bits stuck.
        return PermissionOutcome(path=path, requested=requested, applied=applied, enforceable=False)
    return PermissionOutcome(path=path, requested=requested, applied=applied, enforceable=True)
