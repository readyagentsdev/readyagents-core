"""Workspace path containment. Resolve first, compare second, always.

Case sensitivity is probed per root at runtime (cached), never inferred from
``sys.platform``. Forbidden Windows forms are rejected before resolution.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Final
from uuid import uuid4

from readyagents.errors import PathError

_RESERVED_STEMS: Final = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_SHORT_NAME: Final = re.compile(r"(?i)^[^/\\]*~[0-9]{1,2}(\.|$)")
_DRIVE_RELATIVE: Final = re.compile(r"^[A-Za-z]:(?![\\/])")
_UNC_PREFIXES: Final = ("\\\\", "//")
_EXTENDED_PREFIXES: Final = ("\\\\?\\", "//?/", "\\\\.\\", "//./")
_MAX_PATH: Final = 4096
_MAX_PATH_WIN: Final = 259

_case_cache: dict[str, bool] = {}
_case_overrides: dict[str, bool] = {}
_case_lock = Lock()


def filesystem_case_sensitive(root: Path) -> bool:
    """Return whether ``root``'s filesystem distinguishes case. Cached per root."""
    resolved = Path(root).expanduser()
    try:
        key = str(resolved.resolve())
    except OSError:
        key = str(resolved)
    with _case_lock:
        if key in _case_overrides:
            return _case_overrides[key]
        cached = _case_cache.get(key)
        if cached is not None:
            return cached
    probed = _probe_case_sensitive(resolved)
    with _case_lock:
        _case_cache[key] = probed
    return probed


@contextmanager
def force_case_sensitive(root: Path, value: bool) -> Iterator[None]:
    """Test hook: force the case-sensitivity probe result for ``root``."""
    resolved = Path(root).expanduser()
    try:
        key = str(resolved.resolve())
    except OSError:
        key = str(resolved)
    with _case_lock:
        _case_overrides[key] = value
    try:
        yield
    finally:
        with _case_lock:
            _case_overrides.pop(key, None)


def resolve_within(
    candidate: Path | str,
    root: Path | str,
    *,
    must_exist: bool = False,
    what: str = "path",
) -> Path:
    """Resolve ``candidate`` and return it only if it is contained in ``root``."""
    raw = str(candidate)
    if "\x00" in raw or not raw.strip("\n\r\t"):
        raise PathError(f"{what} must be a path under {root}")
    _reject_forbidden_forms(raw, Path(root), what=what)
    root_path = Path(root).expanduser()
    try:
        root_resolved = root_path.resolve()
    except OSError as exc:
        raise PathError(f"{what} root is not resolvable: {root}") from exc
    text = raw.strip("\n\r\t")
    path = Path(text)
    if _DRIVE_RELATIVE.match(text.replace("\\", "/")) or _DRIVE_RELATIVE.match(text):
        raise PathError(f"{what} drive-relative path is not allowed: {raw}")
    if not path.is_absolute():
        path = root_resolved / path
    if _path_length(path) > _max_allowed(path):
        raise PathError(f"{what} exceeds the path-length limit: {raw}")
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise PathError(f"{what} could not be resolved: {raw}") from exc
    if must_exist and not resolved.exists():
        raise PathError(f"{what} not found: {raw}")
    if not _contained(resolved, root_resolved):
        raise PathError(
            f"{what} is outside the workspace: {raw} "
            f"(resolved to {resolved}, must stay under {root_resolved})"
        )
    short = _short_name_component(text)
    if short and not _contained(resolved, root_resolved):
        raise PathError(f"{what} 8.3 short name resolves outside the workspace: {raw}")
    return resolved


def contained(candidate: Path, root: Path) -> bool:
    """True if already-resolved ``candidate`` is inside already-resolved ``root``."""
    return _contained(Path(candidate), Path(root))


def _contained(resolved: Path, root: Path) -> bool:
    try:
        if resolved == root or resolved.is_relative_to(root):
            return True
    except (ValueError, OSError):
        pass
    if filesystem_case_sensitive(root):
        return False
    return _is_relative_casefold(resolved, root)


def _is_relative_casefold(resolved: Path, root: Path) -> bool:
    rp = [part.casefold() for part in resolved.parts]
    bp = [part.casefold() for part in root.parts]
    return len(rp) >= len(bp) and rp[: len(bp)] == bp


def _probe_case_sensitive(root: Path) -> bool:
    root.mkdir(parents=True, exist_ok=True)
    name = f".ra_cs_{uuid4().hex}a"
    probe = root / name
    other = root / (name[:-1] + "A")
    try:
        probe.write_bytes(b"x")
        return not other.exists()
    except OSError:
        return os.name != "nt"
    finally:
        probe.unlink(missing_ok=True)


def _reject_forbidden_forms(raw: str, root: Path, *, what: str) -> None:
    text = raw.strip("\n\r\t")
    if _is_extended(text) and not _is_extended(str(root)):
        raise PathError(f"{what} extended-length prefix is not allowed: {raw}")
    if _is_unc(text) and not _is_unc(str(root)):
        raise PathError(f"{what} UNC path is not allowed: {raw}")
    if _DRIVE_RELATIVE.match(text) or _DRIVE_RELATIVE.match(text.replace("\\", "/")):
        raise PathError(f"{what} drive-relative path is not allowed: {raw}")
    for component in _components(text):
        stem = component.split(".", 1)[0].upper()
        if stem in _RESERVED_STEMS or component.upper().split(".")[0] in _RESERVED_STEMS:
            if _is_reserved_component(component):
                raise PathError(f"{what} reserved device name is not allowed: {raw}")
        if ":" in component and not _is_drive_component(component):
            raise PathError(f"{what} alternate data stream is not allowed: {raw}")
        if component.endswith((" ", ".")) and component not in {".", ".."}:
            raise PathError(
                f"{what} trailing dot or space in a path component is not allowed: {raw}"
            )


def _is_reserved_component(component: str) -> bool:
    name = component.split(":")[0]
    stem = name.split(".")[0].upper()
    return stem in _RESERVED_STEMS


def _is_drive_component(component: str) -> bool:
    return len(component) == 2 and component[1] == ":" and component[0].isalpha()


def _components(raw: str) -> list[str]:
    cleaned = raw.replace("\\", "/")
    if len(cleaned) >= 2 and cleaned[1] == ":":
        cleaned = cleaned[2:]
    parts = [p for p in cleaned.split("/") if p and p not in {".", ".."}]
    return parts


def _is_unc(text: str) -> bool:
    if _is_extended(text):
        rest = text[4:] if text.startswith("\\\\?\\") or text.startswith("//?/") else text
        return rest.startswith("UNC\\") or rest.startswith("UNC/") or rest.startswith("\\")
    return text.startswith("\\\\") or text.startswith("//")


def _is_extended(text: str) -> bool:
    upper = text[:4]
    return any(text.startswith(prefix) or upper.startswith(prefix) for prefix in _EXTENDED_PREFIXES)


def _short_name_component(raw: str) -> bool:
    return any(_SHORT_NAME.match(part) for part in _components(raw))


def _path_length(path: Path) -> int:
    return len(str(path))


def _max_allowed(path: Path) -> int:
    text = str(path)
    if os.name == "nt" and not _is_extended(text):
        return _MAX_PATH_WIN
    return _MAX_PATH
