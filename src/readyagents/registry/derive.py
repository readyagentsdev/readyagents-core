"""Recompute derived facts from artifacts. Never execute a workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from readyagents.registry.schema import DerivedFacts
from readyagents.trust.digest import digest_bytes
from readyagents.workflow.runner import load_workflow
from readyagents.workflow.schema import WorkflowSpec

_CONNECTORS = {"rest", "sql", "object_storage", "message", "ingest"}


def derive_workflow(
    path: Path,
    *,
    runs: list[Any] | None = None,
    settings: Any = None,
) -> DerivedFacts:
    spec = load_workflow(path)
    digest = digest_bytes(_read_bytes(path))
    node_types = sorted({str(n.type) for n in spec.nodes})
    tools = sorted({str(n.tool) for n in spec.nodes if n.tool})
    connectors = sorted({t for t in tools if t.split(".", 1)[0] in _CONNECTORS or t in _CONNECTORS})
    models = _models(spec)
    hosts = _hosts(spec)
    approvals = any(str(n.type) == "approval" for n in spec.nodes)
    memory = any(str(n.type) in {"memory", "ingest", "knowledge"} for n in spec.nodes)
    memory = memory or bool(spec.memory_scopes)
    budget = None
    if spec.budget and spec.budget.max_cost_usd is not None:
        budget = float(spec.budget.max_cost_usd)
    run_count, spend, fail_rate, first, last, evidence = _run_stats(spec.name, runs or [])
    return DerivedFacts(
        kind="workflow",
        path=str(path),
        name=spec.name,
        version=spec.version,
        digest=digest,
        node_types=node_types,
        tools=tools,
        connectors=connectors,
        models=models,
        egress_hosts=hosts,
        approval_gates=approvals,
        memory_or_knowledge=memory,
        policy=_policy_label(spec),
        budget_max_cost_usd=budget,
        run_count=run_count,
        spend_micros=spend,
        failure_rate=fail_rate,
        health_score=_health_score(spec.name, settings),
        first_run=first,
        last_run=last,
        signed_release=_signed_release(path, digest, settings),
        evidence_runs=evidence,
    )


def derive_package(path: Path) -> DerivedFacts:
    text = _read_bytes(path)
    name = path.parent.name
    version = None
    try:
        raw = yaml.safe_load(text.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError):
        raw = {}
    if isinstance(raw, dict):
        if raw.get("name"):
            name = str(raw["name"])
        if raw.get("version") is not None:
            version = str(raw["version"])
    return DerivedFacts(
        kind="package",
        path=str(path),
        name=name,
        version=version,
        digest=digest_bytes(text),
    )


def derive_release(path: Path) -> DerivedFacts:
    text = _read_bytes(path)
    name = "release"
    version = None
    signed = (path.parent / f"{path.name}.sig").is_file()
    try:
        import json

        raw = json.loads(text.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raw = {}
    if isinstance(raw, dict):
        if raw.get("name"):
            name = str(raw["name"])
        if raw.get("version") is not None:
            version = str(raw["version"])
        signed = signed or bool(raw.get("signed"))
    return DerivedFacts(
        kind="release",
        path=str(path),
        name=name,
        version=version,
        digest=digest_bytes(text),
        signed_release=signed,
    )


def derive_pack(path: Path) -> DerivedFacts:
    """Digest a pack file. Never import or exec it."""
    text = _read_bytes(path)
    return DerivedFacts(
        kind="pack",
        path=str(path),
        name=path.stem,
        version=None,
        digest=digest_bytes(text),
    )


def _read_bytes(path: Path) -> bytes:
    return Path(path).read_bytes()


def _models(spec: WorkflowSpec) -> list[str]:
    found: set[str] = set()
    if spec.default_model:
        found.add(str(spec.default_model))
    for item in spec.fallback_models or []:
        found.add(str(item))
    routing = getattr(spec, "routing", None)
    if routing is not None:
        for attr in ("model", "default_model"):
            value = getattr(routing, attr, None)
            if value:
                found.add(str(value))
    for node in spec.nodes:
        if node.model:
            found.add(str(node.model))
        for item in node.fallback_models or []:
            found.add(str(item))
    return sorted(found)


def _hosts(spec: WorkflowSpec) -> list[str]:
    found: set[str] = set()
    if spec.on_pause_url:
        _add_host(spec.on_pause_url, found)
    for node in spec.nodes:
        _walk_hosts(node.arguments or {}, found)
        for item in getattr(node, "allow", None) or []:
            if isinstance(item, str):
                _add_host(item, found)
        for spec in getattr(node, "notify", None) or []:
            url = getattr(spec, "url", None)
            if isinstance(url, str):
                _add_host(url, found)
    return sorted(found)


def _walk_hosts(value: Any, found: set[str]) -> None:
    if isinstance(value, str):
        _add_host(value, found)
        return
    if isinstance(value, dict):
        for item in value.values():
            _walk_hosts(item, found)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _walk_hosts(item, found)


def _add_host(raw: str, found: set[str]) -> None:
    text = str(raw).strip()
    if "://" in text:
        host = urlparse(text).hostname
        if host:
            found.add(host)
        return
    if text.startswith("http"):
        host = urlparse(f"https://{text}").hostname
        if host:
            found.add(host)


def _policy_label(spec: WorkflowSpec) -> str | None:
    routing = getattr(spec, "routing", None)
    if routing is None:
        return None
    version = getattr(routing, "version", None)
    return f"routing:{version}" if version is not None else "routing"


def _run_stats(name: str, runs: list[Any]) -> tuple[int, int, float, str | None, str | None, int]:
    matched = [
        item for item in runs if getattr(getattr(item, "state", item), "workflow_name", "") == name
    ]
    if not matched:
        return 0, 0, 0.0, None, None, 0
    states = [getattr(item, "state", item) for item in matched]
    spend = sum(int((getattr(s, "usage", None) or {}).get("cost_micros") or 0) for s in states)
    failed = sum(1 for s in states if getattr(s, "status", "") == "failed")
    stamps = [getattr(s, "started_at", None) or "" for s in states]
    stamps = [t for t in stamps if t]
    first = min(stamps) if stamps else None
    last = max(stamps) if stamps else None
    evidence = sum(1 for s in states if (getattr(s, "metadata", None) or {}).get("evidence"))
    return len(states), spend, (failed / len(states) if states else 0.0), first, last, evidence


def _health_score(name: str, settings: Any) -> float | None:
    if settings is None:
        return None
    try:
        from readyagents.health.query import query_health
        from readyagents.run_store import open_run_store

        store = open_run_store(settings)
        try:
            report = query_health(store, workflow=name)
        finally:
            closer = getattr(store, "close", None)
            if callable(closer):
                closer()
        for row in report.workflows:
            if row.name == name:
                return float(row.success_rate)
    except Exception:  # noqa: BLE001
        return None
    return None


def _signed_release(path: Path, digest: str, settings: Any) -> bool:
    if (path.parent / f"{path.name}.sig").is_file():
        return True
    if settings is None:
        return False
    try:
        from readyagents.env.store import EnvStore

        store = EnvStore(settings)
        for name in store.list_names([]):
            for pointer in (store.current(name), store.previous(name), store.candidate(name)):
                if not isinstance(pointer, dict) or not pointer.get("signed"):
                    continue
                if pointer.get("digest") == digest:
                    return True
                pins = pointer.get("pins") or {}
                if isinstance(pins, dict) and digest in {str(v) for v in pins.values()}:
                    return True
    except Exception:  # noqa: BLE001
        return False
    return False
