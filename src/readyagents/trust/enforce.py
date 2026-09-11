"""Opt-in supply-chain enforcement: resolve → digest → verify → execute."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from readyagents.errors import TrustError
from readyagents.trust.digest import (
    DIGEST_ALGORITHM,
    DIGEST_VERSION,
    KIND_INCLUDE,
    KIND_MCP,
    KIND_PACK,
    KIND_SKILL,
    KIND_WORKFLOW,
    digest_pack_bytes,
    include_source_map,
    inspect_workflow,
    prefixed,
)
from readyagents.trust.lock import (
    Lockfile,
    default_lock_path,
    diff_lockfile,
    load_lockfile,
    skill_lock_artifacts,
)

LOCK_GATE_NODE = "supply_chain_lock"
FILE_LOCK_KINDS = (KIND_WORKFLOW, KIND_INCLUDE, KIND_PACK, KIND_SKILL)
_APPROVE = {"approve", "approved", "yes", "true", "accept", "ok"}


@dataclass
class ArtifactStatus:
    kind: str
    digest: str
    path: str | None = None
    name: str | None = None
    signature: str = "unsigned"

    def as_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "kind": self.kind,
            "digest": self.digest,
            "signature": self.signature,
        }
        if self.path is not None:
            row["path"] = self.path
        if self.name is not None:
            row["name"] = self.name
        return row


@dataclass
class TrustReport:
    artifacts: list[ArtifactStatus] = field(default_factory=list)
    require_signed: bool = False
    frozen: bool = False
    lock_mismatches: list[dict[str, str]] = field(default_factory=list)
    pack_buffers: dict[str, bytes] = field(default_factory=dict)
    include_buffers: dict[str, str] = field(default_factory=dict)
    workflow_source: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "digest_algorithm": DIGEST_ALGORITHM,
            "digest_version": DIGEST_VERSION,
            "require_signed": self.require_signed,
            "frozen": self.frozen,
            "artifacts": [item.as_dict() for item in self.artifacts],
        }
        if self.lock_mismatches:
            payload["lock_mismatch"] = list(self.lock_mismatches)
        return payload


def resolve_enforcement(
    *,
    require_signed: bool = False,
    frozen: bool = False,
    policy: Any = None,
    stored: dict[str, Any] | None = None,
) -> tuple[bool, bool, str]:
    """Flags, then policy, then a stored run cannot drop enforcement."""
    required = bool(require_signed)
    lock_frozen = bool(frozen)
    on_mismatch = "allow"
    if policy is not None:
        required = required or bool(getattr(policy, "require_signed", False))
        lock_frozen = lock_frozen or bool(getattr(policy, "frozen", False))
        on_mismatch = str(getattr(policy, "on_lock_mismatch", "allow") or "allow")
    if stored:
        raw_supply = stored.get("supply_chain")
        supply = raw_supply if isinstance(raw_supply, dict) else stored
        if isinstance(supply, dict):
            if supply.get("require_signed"):
                required = True
            if supply.get("frozen"):
                lock_frozen = True
    return required, lock_frozen, on_mismatch


def evaluate_workflow(
    workflow_path: Path | str,
    *,
    workspace: Path,
    pack_specs: Sequence[str] | None = None,
    require_signed: bool = False,
    frozen: bool = False,
    on_lock_mismatch: str = "allow",
    keyring: Any | None = None,
    lock_path: Path | str | None = None,
    mcp_surfaces: dict[str, str] | None = None,
    run_id: str | None = None,
    check_lock: bool = True,
    source_text: str | None = None,
    require_resolved_includes: bool = False,
    skill_home: Path | str | None = None,
) -> TrustReport:
    source = Path(workflow_path)
    enforce = bool(require_signed or frozen)
    report = inspect_workflow(
        source,
        source=source_text,
        require_resolved_includes=require_resolved_includes or enforce,
    )
    statuses: list[ArtifactStatus] = [
        ArtifactStatus(kind=KIND_WORKFLOW, path=source.name, digest=report.digest)
    ]
    for include in report.includes:
        statuses.append(ArtifactStatus(kind=KIND_INCLUDE, path=include.path, digest=include.digest))
    if require_signed:
        statuses[0].signature = _verify_or_raise(
            source, kind=KIND_WORKFLOW, keyring=keyring, digest=report.digest
        )
    else:
        statuses[0].signature = _peek_signature(source, kind=KIND_WORKFLOW)

    pack_buffers: dict[str, bytes] = {}
    from readyagents.packs.loader import confine_pack_path

    for spec in pack_specs or ():
        pack_path = confine_pack_path(spec, workspace)
        data = pack_path.read_bytes()
        pack_buffers[str(pack_path)] = data
        status = ArtifactStatus(
            kind=KIND_PACK,
            path=_rel(pack_path, source.parent),
            digest=digest_pack_bytes(data),
        )
        if require_signed:
            status.signature = _verify_or_raise(
                pack_path, kind=KIND_PACK, keyring=keyring, data=data
            )
        else:
            status.signature = _peek_signature(pack_path, kind=KIND_PACK)
        statuses.append(status)

    for name, digest in sorted((mcp_surfaces or {}).items()):
        statuses.append(
            ArtifactStatus(kind=KIND_MCP, name=name, digest=prefixed(digest), signature="n/a")
        )

    if skill_home is not None:
        home = Path(skill_home)
    else:
        from readyagents.config import get_settings

        home = get_settings().home_path()
    for item in skill_lock_artifacts(report.document, home=home, missing="skip"):
        status = ArtifactStatus(
            kind=KIND_SKILL,
            path=item.path,
            name=item.name,
            digest=item.digest or "",
        )
        skill_md = home / (item.path or "") / "SKILL.md"
        if require_signed:
            status.signature = _verify_or_raise(
                skill_md, kind=KIND_SKILL, keyring=keyring, digest=item.digest
            )
        else:
            status.signature = _peek_signature(skill_md, kind=KIND_SKILL)
        statuses.append(status)

    result = TrustReport(
        artifacts=statuses,
        require_signed=require_signed,
        frozen=frozen,
        pack_buffers=pack_buffers,
        include_buffers=include_source_map(report),
        workflow_source=report.source_text,
    )
    if check_lock:
        apply_lock(
            result,
            source,
            frozen=frozen,
            on_lock_mismatch=on_lock_mismatch,
            lock_path=lock_path,
        )
    return result


def apply_lock(
    result: TrustReport,
    source: Path | str,
    *,
    frozen: bool = False,
    on_lock_mismatch: str = "allow",
    lock_path: Path | str | None = None,
    kinds: Sequence[str] | None = None,
) -> None:
    dest = Path(lock_path) if lock_path is not None else default_lock_path(source)
    if frozen and not dest.is_file():
        raise TrustError(
            f"missing lockfile (frozen): {dest}",
            artifact=str(dest),
            reason="missing_lockfile",
        )
    if not dest.is_file():
        return
    expected = load_lockfile(dest)
    actual = _lock_from_report(result, generated_at=expected.generated_at)
    if kinds is not None:
        allowed = set(kinds)
        expected = Lockfile(
            artifacts=[item for item in expected.artifacts if item.kind in allowed],
            version=expected.version,
            digest_algorithm=expected.digest_algorithm,
            digest_version=expected.digest_version,
            generated_at=expected.generated_at,
            path=expected.path,
        )
        actual = Lockfile(
            artifacts=[item for item in actual.artifacts if item.kind in allowed],
            generated_at=expected.generated_at,
        )
    mismatches = diff_lockfile(expected, actual)
    if not mismatches:
        return
    result.lock_mismatches = mismatches
    summary = ", ".join(item["artifact"] for item in mismatches)
    if frozen or on_lock_mismatch == "deny":
        raise TrustError(
            f"lockfile mismatch for {source}: {summary}",
            artifact=str(source),
            reason="digest_mismatch",
        )


def lock_gate_pending(
    result: TrustReport,
    *,
    on_lock_mismatch: str,
    decisions: Mapping[str, str] | None = None,
) -> bool:
    if not result.lock_mismatches or on_lock_mismatch != "gate":
        return False
    raw = str((decisions or {}).get(LOCK_GATE_NODE) or "").strip().lower()
    return raw not in _APPROVE


def _verify_or_raise(
    path: Path,
    *,
    kind: str,
    keyring: Any,
    data: bytes | None = None,
    digest: str | None = None,
) -> str:
    from readyagents.trust.sign import verify_artifact

    verified = verify_artifact(path, kind=kind, data=data, keyring=keyring, digest=digest)
    return str(verified.get("signature") or "verified")


def _peek_signature(path: Path, *, kind: str) -> str:
    """Record status without importing the crypto extra on the unsigned default path."""
    from readyagents.trust.sign import signature_path

    dest = signature_path(path)
    if dest.is_file():
        return "present"
    return "unsigned"


def _lock_from_report(result: TrustReport, *, generated_at: str = "") -> Lockfile:
    from readyagents.trust.lock import LockArtifact

    items = []
    for row in result.artifacts:
        if row.kind == KIND_MCP:
            items.append(LockArtifact(kind=KIND_MCP, name=row.name, surface_digest=row.digest))
        elif row.kind == KIND_SKILL:
            items.append(
                LockArtifact(kind=KIND_SKILL, path=row.path, name=row.name, digest=row.digest)
            )
        else:
            items.append(LockArtifact(kind=row.kind, path=row.path, digest=row.digest))
    return Lockfile(artifacts=items, generated_at=generated_at)


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name
