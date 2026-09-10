"""Load installed packs via importlib.metadata entry points and local files."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any
from uuid import uuid4

from readyagents.errors import ConfigError
from readyagents.logging import get_logger
from readyagents.packs.protocol import Pack
from readyagents.replay.cassette import CORE_BUILTIN_TOOLS, SEAL_CLASSES
from readyagents.tools import ToolRegistry

log = get_logger("packs")

ENTRY_POINT_GROUP = "readyagents.packs"


def discover_packs() -> list[Pack]:
    """Load every installed pack. Core runs fine with an empty list."""
    packs: list[Pack] = []
    selected = entry_points().select(group=ENTRY_POINT_GROUP)
    for ep in selected:
        try:
            loaded = ep.load()
            pack = loaded() if callable(loaded) and not _is_pack_instance(loaded) else loaded
            if not _is_pack_instance(pack):
                raise ConfigError(
                    f"Entry point '{ep.name}' did not return a Pack (name/version/register_*)"
                )
            packs.append(pack)
            log.debug("Loaded pack %s %s", pack.name, pack.version)
        except ConfigError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(f"Failed to load pack '{ep.name}': {exc}") from exc
    return packs


def _is_pack_instance(obj: Any) -> bool:
    return (
        hasattr(obj, "name")
        and hasattr(obj, "version")
        and callable(getattr(obj, "register_nodes", None))
        and callable(getattr(obj, "register_tools", None))
        and callable(getattr(obj, "register_workflows", None))
    )


def collect_pack_tools(packs: list[Pack] | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    for pack in packs if packs is not None else discover_packs():
        for tool in pack.register_tools() or []:
            registry.register(tool)
    return registry


def collect_pack_nodes(packs: list[Pack] | None = None) -> dict[str, Any]:
    handlers: dict[str, Any] = {}
    for pack in packs if packs is not None else discover_packs():
        for type_name, handler in (pack.register_nodes() or {}).items():
            handlers[type_name] = handler
    return handlers


def collect_pack_secrets(packs: list[Pack] | None = None) -> list[Any]:
    backends: list[Any] = []
    for pack in packs if packs is not None else discover_packs():
        fn = getattr(pack, "register_secrets", None)
        if not callable(fn):
            continue
        backends.extend(list(fn() or []))
    return backends


def collect_pack_authorizers(packs: list[Pack] | None = None) -> list[Any]:
    authorizers: list[Any] = []
    for pack in packs if packs is not None else discover_packs():
        fn = getattr(pack, "register_authorizers", None)
        if not callable(fn):
            continue
        authorizers.extend(list(fn() or []))
    return authorizers


def collect_pack_observers(packs: list[Pack] | None = None) -> list[Any]:
    """Optional observers. Old packs without the method still load."""
    observers: list[Any] = []
    for pack in packs if packs is not None else discover_packs():
        fn = getattr(pack, "register_observers", None)
        if not callable(fn):
            continue
        observers.extend(list(fn() or []))
    return observers


def collect_pack_seals(
    packs: list[Pack] | None = None,
    extra_tools: ToolRegistry | Sequence[Any] | None = None,
) -> dict[str, str]:
    """Optional cassette seals. Old packs without the method still load.

    Core builtin names are ignored. First declaration wins. Invalid values
    raise ConfigError at collect time.
    """
    out: dict[str, str] = {}
    for pack in packs if packs is not None else discover_packs():
        fn = getattr(pack, "register_tool_seals", None)
        if callable(fn):
            mapping = fn() or {}
            if not isinstance(mapping, dict) and not hasattr(mapping, "items"):
                raise ConfigError(
                    f"Pack {getattr(pack, 'name', pack)!r} register_tool_seals "
                    "must return a mapping"
                )
            for raw_name, raw_klass in dict(mapping).items():
                _record_seal(out, str(raw_name), raw_klass)
        for tool in pack.register_tools() or []:
            declared = getattr(tool, "determinism", None)
            if declared:
                _record_seal(out, str(tool.name), declared)
    for tool in _iter_extra_tools(extra_tools):
        declared = getattr(tool, "determinism", None)
        if declared:
            _record_seal(out, str(getattr(tool, "name", "")), declared)
    return out


def _iter_extra_tools(extra_tools: ToolRegistry | Sequence[Any] | None) -> list[Any]:
    if extra_tools is None:
        return []
    if isinstance(extra_tools, ToolRegistry):
        return list(extra_tools.as_dict().values())
    return list(extra_tools)


def _record_seal(out: dict[str, str], name: str, raw_klass: Any) -> None:
    name = name.strip()
    if not name:
        raise ConfigError("Tool seal name must be a non-empty string")
    klass = str(raw_klass).strip()
    if klass not in SEAL_CLASSES:
        raise ConfigError(
            f"Invalid tool seal {klass!r} for {name!r}; "
            "expected recomputed, sealable, or unsealable"
        )
    if name in CORE_BUILTIN_TOOLS:
        return
    if name in out:
        return
    out[name] = klass


def collect_pack_specs(flags: Sequence[str] | None = None, *, env: str | None = None) -> list[str]:
    """Combine READYAGENTS_PACK (pathsep or comma) with repeatable --pack flags."""
    out: list[str] = []
    raw_env = os.environ.get("READYAGENTS_PACK") if env is None else env
    if raw_env:
        normalized = raw_env.replace(",", os.pathsep)
        for part in normalized.split(os.pathsep):
            piece = part.strip()
            if piece:
                out.append(piece)
    for flag in flags or ():
        text = str(flag).strip()
        if text:
            out.append(text)
    return out


def confine_pack_path(raw: str | Path, root: Path) -> Path:
    """Resolve ``raw`` and refuse anything outside ``root`` (symlink-aware)."""
    from readyagents.errors import PathError
    from readyagents.paths import resolve_within

    try:
        resolved = resolve_within(raw, root, what="Pack path")
    except PathError as extra:
        raise ConfigError(str(extra)) from extra
    if not resolved.is_file():
        raise ConfigError(f"Pack file not found: {raw}")
    if resolved.suffix.lower() != ".py":
        raise ConfigError(f"Pack path must be a Python file: {raw}")
    return resolved


def load_pack_file(
    raw: str | Path,
    *,
    root: Path,
    require_signed: bool = False,
    keyring: Any | None = None,
    source: bytes | None = None,
) -> Pack:
    """Import a local pack module confined under ``root``.

    Bytes are read once and executed from that buffer so a swap between
    digest and import cannot change what runs. Verification, when requested,
    happens before ``exec``.
    """
    path = confine_pack_path(raw, root)
    data = source if source is not None else path.read_bytes()
    if require_signed:
        from readyagents.trust.sign import verify_artifact

        verify_artifact(path, kind="pack", data=data, keyring=keyring)
    return _exec_pack_bytes(data, path)


def _exec_pack_bytes(data: bytes, path: Path) -> Pack:
    """Compile and exec ``data`` as ``path``. Never re-reads the file."""
    import types

    mod_name = f"_readyagents_pack_{path.stem}_{uuid4().hex[:8]}"
    module = types.ModuleType(mod_name)
    module.__file__ = str(path)
    sys.modules[mod_name] = module
    try:
        code = compile(data, str(path), "exec", dont_inherit=True)
        exec(code, module.__dict__)
    except Exception as extra:
        sys.modules.pop(mod_name, None)
        raise ConfigError(f"Failed to load pack '{path}': {extra}") from extra
    getter = getattr(module, "get_pack", None)
    if callable(getter):
        loaded = getter()
        if _is_pack_instance(loaded):
            log.debug("Loaded local pack %s %s from %s", loaded.name, loaded.version, path)
            return loaded
        raise ConfigError(f"get_pack() in {path} did not return a Pack")
    if _is_pack_instance(module):
        return module  # type: ignore[return-value]
    raise ConfigError(f"Pack {path} needs get_pack() or a Pack instance")


def load_local_packs(specs: Sequence[str], *, root: Path) -> list[Pack]:
    return [load_pack_file(spec, root=root) for spec in specs]
