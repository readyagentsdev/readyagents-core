"""Scan declared roots and recompute derived facts. Never run or import packs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from readyagents.config import Settings, get_settings
from readyagents.registry.derive import (
    derive_pack,
    derive_package,
    derive_release,
    derive_workflow,
)
from readyagents.registry.discover import as_rel, discover
from readyagents.registry.ids import mint_id
from readyagents.registry.schema import DeclaredAgent, DerivedFacts
from readyagents.registry.store import (
    load_config,
    load_declared,
    load_index,
    save_declared,
    save_index,
)
from readyagents.run_store import open_run_store
from readyagents.run_store.base import RunQuery


@dataclass
class RegistryEntry:
    agent_id: str
    declared: DeclaredAgent
    derived: DerivedFacts

    def as_dict(self, *, redact: bool = True) -> dict[str, Any]:
        derived = self.derived.as_dict()
        if redact:
            derived["egress_hosts"] = ["[redacted-endpoint]" for _ in derived["egress_hosts"]]
            derived["tools"] = [t if not secretish(t) else "[redacted]" for t in derived["tools"]]
        return {
            "agent_id": self.agent_id,
            "declared": self.declared.model_dump(mode="python"),
            "derived": derived,
            "missing": self.declared.missing_fields(),
        }


def secretish(value: str) -> bool:
    text = value.lower()
    return any(token in text for token in ("secret", "token", "password", "api_key", "sk-"))


def scan(
    *,
    settings: Settings | None = None,
    roots: list[str] | None = None,
    authorizer: Any = None,
    actor: str | None = None,
) -> list[RegistryEntry]:
    settings = settings or get_settings()
    if authorizer is not None:
        authorizer.check(actor, "registry.scan", "registry")
    cfg = load_config(settings)
    use_roots = list(roots if roots is not None else cfg.roots)
    found = discover(use_roots, settings=settings)
    index = load_index(settings)
    agents: dict[str, Any] = dict(index.get("agents") or {})
    runs = _load_runs(settings)
    entries: list[RegistryEntry] = []
    seen_ids: set[str] = set()
    for kind, paths in found.items():
        for path in paths:
            derived = _derive(kind, path, runs, settings)
            agent_id = _match_or_mint(path, derived, agents, settings)
            seen_ids.add(agent_id)
            declared = load_declared(agent_id, settings) or DeclaredAgent(agent_id=agent_id)
            if declared.agent_id != agent_id:
                declared = declared.model_copy(update={"agent_id": agent_id})
            save_declared(declared, settings)
            rel = as_rel(path, settings)
            agents[agent_id] = {
                "path": rel,
                "kind": kind,
                "digest": derived.digest,
                "name": derived.name,
            }
            derived.path = rel
            entries.append(RegistryEntry(agent_id=agent_id, declared=declared, derived=derived))
    index["agents"] = agents
    save_index(index, settings)
    return entries


def load_entries(*, settings: Settings | None = None) -> list[RegistryEntry]:
    settings = settings or get_settings()
    index = load_index(settings)
    runs = _load_runs(settings)
    entries: list[RegistryEntry] = []
    workspace = settings.workspace_path()
    for agent_id, row in (index.get("agents") or {}).items():
        if not isinstance(row, dict):
            continue
        rel = str(row.get("path") or "")
        path = (workspace / rel).resolve() if rel else None
        kind = str(row.get("kind") or "workflow")
        declared = load_declared(agent_id, settings) or DeclaredAgent(agent_id=agent_id)
        if path is None or not path.is_file():
            derived = DerivedFacts(
                kind=kind,
                path=rel,
                name=str(row.get("name") or agent_id),
                version=None,
                digest=str(row.get("digest") or ""),
            )
            entries.append(RegistryEntry(agent_id, declared, derived))
            continue
        derived = _derive(kind, path, runs, settings)
        derived.path = rel
        entries.append(RegistryEntry(agent_id, declared, derived))
    return entries


def get_entry(agent_id: str, *, settings: Settings | None = None) -> RegistryEntry | None:
    token = str(agent_id or "").strip()
    for entry in load_entries(settings=settings):
        if entry.agent_id == token:
            return entry
    return None


def _derive(kind: str, path: Path, runs: list[Any], settings: Settings) -> DerivedFacts:
    if kind == "workflow":
        return derive_workflow(path, runs=runs, settings=settings)
    if kind == "package":
        return derive_package(path)
    if kind == "release":
        return derive_release(path)
    return derive_pack(path)


def _match_or_mint(
    path: Path,
    derived: DerivedFacts,
    agents: dict[str, Any],
    settings: Settings,
) -> str:
    rel = as_rel(path, settings)
    workspace = settings.workspace_path()
    for agent_id, row in agents.items():
        if isinstance(row, dict) and row.get("path") == rel:
            return str(agent_id)
    for agent_id, row in agents.items():
        if not isinstance(row, dict):
            continue
        if row.get("digest") != derived.digest:
            continue
        old_rel = str(row.get("path") or "")
        old = workspace / old_rel if old_rel else None
        if old is not None and old.is_file() and old.resolve() != path.resolve():
            # Copy: the original still exists. Mint a new id.
            continue
        return str(agent_id)
    for agent_id, row in agents.items():
        if not isinstance(row, dict):
            continue
        if row.get("name") != derived.name:
            continue
        old_rel = str(row.get("path") or "")
        old = workspace / old_rel if old_rel else None
        if old is None or not old.is_file():
            return str(agent_id)
    return mint_id()


def _load_runs(settings: Settings) -> list[Any]:
    try:
        store = open_run_store(settings)
        try:
            return list(store.list(RunQuery(limit=500)))
        finally:
            closer = getattr(store, "close", None)
            if callable(closer):
                closer()
    except Exception:  # noqa: BLE001
        return []
