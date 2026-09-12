"""Discover artifacts inside declared roots only. Never import a pack or run a workflow."""

from __future__ import annotations

from pathlib import Path

from readyagents.config import Settings, get_settings
from readyagents.errors import RegistryRefused
from readyagents.workflow.runner import confine_under


def resolve_roots(
    roots: list[str],
    *,
    settings: Settings | None = None,
) -> list[Path]:
    settings = settings or get_settings()
    workspace = settings.workspace_path()
    out: list[Path] = []
    for raw in roots:
        dest = Path(raw)
        if not dest.is_absolute():
            dest = workspace / dest
        confined = confine_under(dest, workspace, what="registry root")
        if confined.is_dir():
            out.append(confined.resolve())
        elif confined.is_file():
            out.append(confined.parent.resolve())
    return out


def iter_under(root: Path, *, pattern: str) -> list[Path]:
    if not root.is_dir():
        return []
    found: list[Path] = []
    for path in root.rglob(pattern):
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if root.resolve() not in resolved.parents and resolved != root.resolve():
            continue
        if resolved.is_file():
            found.append(resolved)
    return found


def discover(
    roots: list[str],
    *,
    settings: Settings | None = None,
) -> dict[str, list[Path]]:
    """Return workflows/packages/releases/packs under declared roots only."""
    if not roots:
        raise RegistryRefused(
            "no registry roots declared; the registry never scans uninvited",
            reason="no_roots",
        )
    resolved = resolve_roots(roots, settings=settings)
    workflows: list[Path] = []
    packages: list[Path] = []
    releases: list[Path] = []
    packs: list[Path] = []
    for root in resolved:
        for path in iter_under(root, pattern="*.yaml") + iter_under(root, pattern="*.yml"):
            name = path.name.lower()
            if name == "readyagents.pkg.yaml":
                packages.append(path)
                continue
            if name.endswith(".agent.yaml") or name == "config.yaml":
                continue
            if _looks_workflow(path):
                workflows.append(path)
        for path in iter_under(root, pattern="manifest.json"):
            if _looks_release(path):
                releases.append(path)
        for path in iter_under(root, pattern="*pack.py"):
            packs.append(path)
    return {
        "workflow": _unique(workflows),
        "package": _unique(packages),
        "release": _unique(releases),
        "pack": _unique(packs),
    }


def _looks_workflow(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:8000]
    except OSError:
        return False
    if "nodes:" not in text:
        return False
    if "\nname:" not in text and not text.startswith("name:"):
        return False
    return True


def _looks_release(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:2000]
    except OSError:
        return False
    return '"digest"' in text and ("release" in text or "pins" in text)


def _unique(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def as_rel(path: Path, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    try:
        return str(path.resolve().relative_to(settings.workspace_path()))
    except ValueError:
        return str(path)
