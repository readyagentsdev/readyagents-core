"""Lockfile pinning: ``readyagents.lock`` maps artifacts to digest-algorithm v1 hashes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from readyagents.atomic import atomic_write_text
from readyagents.errors import TrustError
from readyagents.trust.digest import (
    DIGEST_ALGORITHM,
    DIGEST_VERSION,
    KIND_INCLUDE,
    KIND_MCP,
    KIND_PACK,
    KIND_WORKFLOW,
    digest_pack_bytes,
    inspect_workflow,
    prefixed,
)

LOCK_VERSION = 1
LOCK_NAME = "readyagents.lock"
KIND_ORDER = (KIND_WORKFLOW, KIND_INCLUDE, KIND_PACK, KIND_MCP)


@dataclass
class LockArtifact:
    kind: str
    digest: str | None = None
    path: str | None = None
    name: str | None = None
    surface_digest: str | None = None

    def identity(self) -> str:
        if self.kind == KIND_MCP:
            return f"{self.kind}:{self.name or ''}"
        return f"{self.kind}:{self.path or ''}"

    def pin(self) -> str:
        if self.kind == KIND_MCP:
            return prefixed(self.surface_digest or "")
        return prefixed(self.digest or "")

    def as_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {"kind": self.kind}
        if self.path is not None:
            row["path"] = self.path
        if self.name is not None:
            row["name"] = self.name
        if self.digest is not None:
            row["digest"] = self.digest
        if self.surface_digest is not None:
            row["surface_digest"] = self.surface_digest
        return row


@dataclass
class Lockfile:
    artifacts: list[LockArtifact] = field(default_factory=list)
    version: int = LOCK_VERSION
    digest_algorithm: str = DIGEST_ALGORITHM
    digest_version: int = DIGEST_VERSION
    generated_at: str = ""
    path: Path | None = None

    def by_identity(self) -> dict[str, LockArtifact]:
        return {item.identity(): item for item in self.artifacts}

    def as_dict(self) -> dict[str, Any]:
        artifacts = sorted(
            self.artifacts,
            key=lambda item: (
                KIND_ORDER.index(item.kind) if item.kind in KIND_ORDER else 99,
                item.path or item.name or "",
            ),
        )
        return {
            "version": self.version,
            "digest_algorithm": self.digest_algorithm,
            "digest_version": self.digest_version,
            "generated_at": self.generated_at,
            "artifacts": [item.as_dict() for item in artifacts],
        }


def default_lock_path(workflow: Path | str) -> Path:
    return Path(workflow).resolve().parent / LOCK_NAME


def load_lockfile(path: Path | str) -> Lockfile:
    file = Path(path)
    if not file.is_file():
        raise TrustError(
            f"lockfile not found: {file}",
            artifact=str(file),
            reason="missing_lockfile",
        )
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as extra:
        raise TrustError(
            f"lockfile unreadable: {file}",
            artifact=str(file),
            reason="unreadable",
        ) from extra
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as extra:
        raise TrustError(
            f"lockfile malformed: {file}",
            artifact=str(file),
            reason="malformed",
        ) from extra
    if not isinstance(data, dict):
        raise TrustError(
            f"lockfile malformed: {file}",
            artifact=str(file),
            reason="malformed",
        )
    if data.get("version") != LOCK_VERSION:
        raise TrustError(
            f"unsupported lockfile version {data.get('version')!r}: {file}",
            artifact=str(file),
            reason="malformed",
        )
    raw_items = data.get("artifacts")
    if not isinstance(raw_items, list):
        raise TrustError(
            f"lockfile malformed: {file}",
            artifact=str(file),
            reason="malformed",
        )
    artifacts: list[LockArtifact] = []
    for index, row in enumerate(raw_items):
        if not isinstance(row, dict) or "kind" not in row:
            raise TrustError(
                f"lockfile malformed at artifact {index}: {file}",
                artifact=str(file),
                reason="malformed",
            )
        artifacts.append(
            LockArtifact(
                kind=str(row["kind"]),
                path=str(row["path"]) if row.get("path") is not None else None,
                name=str(row["name"]) if row.get("name") is not None else None,
                digest=str(row["digest"]) if row.get("digest") else None,
                surface_digest=str(row["surface_digest"]) if row.get("surface_digest") else None,
            )
        )
    return Lockfile(
        artifacts=artifacts,
        version=int(data.get("version") or LOCK_VERSION),
        digest_algorithm=str(data.get("digest_algorithm") or DIGEST_ALGORITHM),
        digest_version=int(data.get("digest_version") or DIGEST_VERSION),
        generated_at=str(data.get("generated_at") or ""),
        path=file,
    )


def write_lockfile(lock: Lockfile, path: Path | str) -> Path:
    dest = Path(path)
    dumped = yaml.safe_dump(lock.as_dict(), sort_keys=False, allow_unicode=True)
    atomic_write_text(dest, dumped, encoding="utf-8", newline="\n", restrict=False)
    lock.path = dest
    return dest


def build_lockfile(
    workflow_path: Path | str,
    *,
    pack_specs: Sequence[str] | None = None,
    workspace: Path | None = None,
    mcp_surfaces: Mapping[str, str] | None = None,
    generated_at: str | None = None,
) -> Lockfile:
    source = Path(workflow_path).resolve()
    report = inspect_workflow(source)
    artifacts: list[LockArtifact] = [
        LockArtifact(kind=KIND_WORKFLOW, path=source.name, digest=report.digest)
    ]
    for include in report.includes:
        artifacts.append(LockArtifact(kind=KIND_INCLUDE, path=include.path, digest=include.digest))
    root = workspace if workspace is not None else source.parent
    for spec in pack_specs or ():
        from readyagents.packs.loader import confine_pack_path

        pack_path = confine_pack_path(spec, root)
        data = pack_path.read_bytes()
        rel = _rel(pack_path, source.parent)
        artifacts.append(LockArtifact(kind=KIND_PACK, path=rel, digest=digest_pack_bytes(data)))
    for name, digest in sorted((mcp_surfaces or {}).items()):
        artifacts.append(LockArtifact(kind=KIND_MCP, name=name, surface_digest=prefixed(digest)))
    stamp = generated_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Lockfile(artifacts=artifacts, generated_at=stamp)


def snapshot_mcp_surfaces(workflow: Any, workspace: Path) -> dict[str, str]:
    """Launch declared stdio MCP servers and hash their advertised tools. Local only."""
    servers = getattr(workflow, "mcp_servers", None) or {}
    if not servers:
        return {}
    from readyagents.mcp.client import MCPClient, mcp_available

    if not mcp_available():
        raise TrustError(
            "MCP servers are declared but the optional 'mcp' extra is not installed",
            reason="missing_crypto",
        )
    client = MCPClient(servers, workspace)
    try:
        from readyagents.firewall.mcp_pin import grouped_snapshots

        grouped = grouped_snapshots(client.tools())
        return {name: prefixed(snap.digest) for name, snap in grouped.items()}
    finally:
        client.close()


def diff_lockfile(expected: Lockfile, actual: Lockfile) -> list[dict[str, str]]:
    mismatches: list[dict[str, str]] = []
    want = expected.by_identity()
    have = actual.by_identity()
    for key, item in sorted(want.items()):
        other = have.get(key)
        if other is None:
            mismatches.append(
                {
                    "artifact": key,
                    "reason": "missing",
                    "expected": item.pin(),
                    "actual": "",
                }
            )
            continue
        if item.pin() != other.pin():
            mismatches.append(
                {
                    "artifact": key,
                    "reason": "digest_mismatch",
                    "expected": item.pin(),
                    "actual": other.pin(),
                }
            )
    for key in sorted(set(have) - set(want)):
        mismatches.append(
            {
                "artifact": key,
                "reason": "unexpected",
                "expected": "",
                "actual": have[key].pin(),
            }
        )
    return mismatches


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name
