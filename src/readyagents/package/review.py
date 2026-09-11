"""Capability review payload. Local policy always wins; extras are named, not granted."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from readyagents.errors import PackageRefused
from readyagents.package.layout import MANIFEST_NAME
from readyagents.package.manifest import PackageManifest, load_manifest


def review_tree(
    root: Path,
    *,
    digest: str,
    signature_status: str,
    policy: Any | None = None,
) -> dict[str, Any]:
    manifest = load_manifest(root)
    tools, hosts, approvals = inspect_entry(root, manifest)
    declared_tools = sorted(set(tools) | set(manifest.requires.connectors))
    declared_hosts = sorted(hosts)
    extra_tools, extra_hosts = policy_extras(declared_tools, declared_hosts, policy)
    return {
        "name": manifest.name,
        "version": manifest.version,
        "description": manifest.description,
        "entry": manifest.entry,
        "digest": digest,
        "signature": signature_status,
        "tools": declared_tools,
        "hosts": declared_hosts,
        "secrets": list(manifest.secrets),
        "inputs": list(manifest.inputs),
        "budget": _budget(manifest),
        "approvals": approvals,
        "requires": manifest.requires.model_dump(),
        "policy": manifest.policy,
        "fixtures": manifest.fixtures,
        "constrained": bool(extra_tools or extra_hosts),
        "policy_diff": {
            "extra_tools": extra_tools,
            "extra_hosts": extra_hosts,
        },
    }


def inspect_entry(root: Path, manifest: PackageManifest) -> tuple[list[str], list[str], list[str]]:
    entry = root / manifest.entry
    if not entry.is_file():
        raise PackageRefused(f"package entry not found: {manifest.entry}", reason="missing")
    try:
        data = yaml.safe_load(entry.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as extra:
        raise PackageRefused(
            f"package entry unreadable: {manifest.entry}", reason="malformed"
        ) from extra
    if not isinstance(data, dict):
        raise PackageRefused("package entry must be a mapping", reason="malformed")
    tools: set[str] = set()
    hosts: set[str] = set()
    approvals: list[str] = []
    for node in _iter_nodes(data):
        kind = str(node.get("type") or "").strip().lower()
        if kind == "tool":
            name = str(node.get("tool") or "").strip()
            if name:
                tools.add(name)
        elif kind == "agent":
            for name in node.get("tools") or []:
                token = str(name).strip()
                if token:
                    tools.add(token)
        elif kind == "approval":
            nid = str(node.get("id") or "").strip()
            if nid:
                approvals.append(nid)
        args = node.get("arguments") if isinstance(node.get("arguments"), dict) else {}
        for key in ("url", "host", "base_url"):
            raw = args.get(key)
            if isinstance(raw, str):
                host = _host_of(raw)
                if host:
                    hosts.add(host)
    if manifest.policy:
        policy_path = root / manifest.policy
        if policy_path.is_file():
            hosts.update(_hosts_from_policy_file(policy_path))
            tools.update(_tools_from_policy_file(policy_path))
    return sorted(tools), sorted(hosts), approvals


def policy_extras(
    tools: list[str],
    hosts: list[str],
    policy: Any | None,
) -> tuple[list[str], list[str]]:
    if policy is None:
        return [], []
    default = str(getattr(policy, "default", "allow") or "allow")
    allowed_tools: set[str] = set()
    rules = getattr(policy, "tools", None) or {}
    if isinstance(rules, dict):
        allowed_tools = {str(k) for k in rules}
    extra_tools: list[str] = []
    if default == "deny":
        extra_tools = [name for name in tools if name not in allowed_tools]
    extra_hosts: list[str] = []
    egress = getattr(policy, "egress", None)
    allow_hosts = getattr(egress, "allow_hosts", None) if egress is not None else None
    if allow_hosts:
        permitted = {str(h).lower() for h in allow_hosts}
        extra_hosts = [h for h in hosts if h.lower() not in permitted]
    return extra_tools, extra_hosts


def permission_widening(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    prev_tools = set(previous.get("tools") or [])
    prev_hosts = set(previous.get("hosts") or [])
    prev_secrets = set(previous.get("secrets") or [])
    new_tools = sorted(set(current.get("tools") or []) - prev_tools)
    new_hosts = sorted(set(current.get("hosts") or []) - prev_hosts)
    new_secrets = sorted(set(current.get("secrets") or []) - prev_secrets)
    prev_budget = (previous.get("budget") or {}).get("max_cost_usd")
    new_budget = (current.get("budget") or {}).get("max_cost_usd")
    budget_up = False
    if new_budget is not None and (prev_budget is None or float(new_budget) > float(prev_budget)):
        budget_up = True
    widened = bool(new_tools or new_hosts or new_secrets or budget_up)
    return {
        "widened": widened,
        "new_tools": new_tools,
        "new_hosts": new_hosts,
        "new_secrets": new_secrets,
        "budget_increased": budget_up,
    }


def _budget(manifest: PackageManifest) -> dict[str, Any]:
    if manifest.budget is None:
        return {}
    return manifest.budget.model_dump(exclude_none=True)


def _iter_nodes(document: dict[str, Any]):
    for node in document.get("nodes") or []:
        if isinstance(node, dict):
            yield from _iter_node_tree(node)


def _iter_node_tree(node: dict[str, Any]):
    yield node
    for branch in node.get("branches") or []:
        if isinstance(branch, dict):
            yield from _iter_node_tree(branch)
    body = node.get("body")
    if isinstance(body, dict):
        yield from _iter_node_tree(body)


def _host_of(raw: str) -> str | None:
    text = raw.strip()
    if not text or "{{" in text:
        return None
    if "://" in text:
        rest = text.split("://", 1)[1]
        host = rest.split("/")[0].split("@")[-1].split(":")[0]
        return host or None
    if "." in text and " " not in text:
        return text.split("/")[0]
    return None


def _hosts_from_policy_file(path: Path) -> set[str]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return set()
    if not isinstance(data, dict):
        return set()
    found: set[str] = set()
    egress = data.get("egress") if isinstance(data.get("egress"), dict) else {}
    for host in egress.get("allow_hosts") or []:
        if isinstance(host, str) and host.strip():
            found.add(host.strip())
    tools = data.get("tools") if isinstance(data.get("tools"), dict) else {}
    for rule in tools.values():
        if not isinstance(rule, dict):
            continue
        for host in rule.get("allow_hosts") or []:
            if isinstance(host, str) and host.strip():
                found.add(host.strip())
    return found


def _tools_from_policy_file(path: Path) -> set[str]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return set()
    raw_tools = data.get("tools") if isinstance(data, dict) else None
    tools = raw_tools if isinstance(raw_tools, dict) else {}
    return {str(k) for k in tools}


def manifest_path(root: Path) -> Path:
    return Path(root) / MANIFEST_NAME
