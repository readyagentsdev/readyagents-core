"""Content-addressed signed releases. Pointers, not copies of infrastructure."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from readyagents.atomic import atomic_write_text
from readyagents.config import Settings, get_settings
from readyagents.env.schema import EnvironmentSpec
from readyagents.env.store import EnvStore
from readyagents.errors import EnvRefused, TrustError
from readyagents.trust.digest import KIND_RELEASE, digest_bytes, digest_canonical, inspect_workflow
from readyagents.workflow.state import utc_now


def releases_dir(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    path = settings.home_path() / "releases"
    path.mkdir(parents=True, exist_ok=True)
    return path


def pin_release(
    workflow: Path | str,
    *,
    env: str,
    spec: EnvironmentSpec | None = None,
    settings: Settings | None = None,
    actor: str | None = None,
    sign_key: Path | str | None = None,
    require_signed: bool = False,
) -> dict[str, Any]:
    """Snapshot workflow + includes + policy + prompts + lockfile; optionally sign."""
    settings = settings or get_settings()
    source = Path(workflow)
    report = inspect_workflow(source)
    pins: dict[str, Any] = {
        "workflow": report.digest,
        "includes": {item.path: item.digest for item in report.includes},
        "policy": _file_digest(_resolve(spec.policy if spec else None, source)),
        "prompts": _prompts_digest(source.parent),
        "lockfile": _file_digest(source.parent / "readyagents.lock"),
    }
    digest = digest_canonical(pins)
    folder = releases_dir(settings) / digest.replace(":", "_")
    folder.mkdir(parents=True, exist_ok=True)
    _write_snapshot(folder, report, source)
    policy_src = _resolve(spec.policy if spec else None, source)
    if policy_src and policy_src.is_file():
        (folder / "policy.yaml").write_bytes(policy_src.read_bytes())
    manifest = {
        "digest": digest,
        "pins": pins,
        "env": env,
        "workflow": source.name,
        "created_at": utc_now(),
        "actor": actor,
    }
    if sign_key:
        manifest["signed"] = True
    manifest_path = folder / "manifest.json"
    blob = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    atomic_write_text(manifest_path, blob, encoding="utf-8", newline="\n")
    if sign_key:
        from readyagents.trust.sign import sign_artifact

        sign_artifact(manifest_path, key=sign_key, kind=KIND_RELEASE)
    elif require_signed:
        raise EnvRefused("release must be signed", reason="unsigned")
    pointer = {
        "digest": digest,
        "path": str(folder),
        "workflow": source.name,
        "actor": actor,
        "pins": pins,
    }
    if sign_key:
        pointer["signed"] = True
    return pointer


def verify_release(
    pointer: dict[str, Any],
    *,
    settings: Settings | None = None,
    require_signed: bool = False,
) -> dict[str, Any]:
    folder = Path(str(pointer.get("path") or ""))
    manifest = folder / "manifest.json"
    if not manifest.is_file():
        raise EnvRefused("release manifest missing", reason="missing")
    sig = manifest.parent / f"{manifest.name}.sig"
    was_signed = bool(pointer.get("signed"))
    if was_signed and not sig.is_file():
        raise EnvRefused("signed release is missing its signature", reason="tamper")
    if require_signed and not sig.is_file():
        raise EnvRefused("release must be signed", reason="unsigned")
    if sig.is_file():
        from readyagents.trust.keyring import load_keyring
        from readyagents.trust.sign import verify_artifact

        try:
            home = (settings or get_settings()).home_path()
            verify_artifact(manifest, kind=KIND_RELEASE, keyring=load_keyring(home=home))
        except TrustError as extra:
            raise EnvRefused(f"tampered or untrusted release: {extra}", reason="tamper") from extra
    data = json.loads(manifest.read_text(encoding="utf-8"))
    pins = data.get("pins") or {}
    expected = digest_canonical(pins)
    if expected != data.get("digest"):
        raise EnvRefused("release digest mismatch", reason="tamper")
    pointer_digest = pointer.get("digest")
    if pointer_digest and data.get("digest") != pointer_digest:
        raise EnvRefused("release pointer does not match manifest digest", reason="tamper")
    expected_folder = str(pointer_digest or data.get("digest") or "").replace(":", "_")
    if expected_folder and folder.name != expected_folder:
        raise EnvRefused("release path does not match digest", reason="tamper")
    live = _pins_from_snapshot(folder)
    if live != pins:
        raise EnvRefused("release snapshot does not match pinned hashes", reason="tamper")
    return data


def workflow_path_for(pointer: dict[str, Any]) -> Path:
    folder = Path(str(pointer.get("path") or ""))
    dest = folder / "workflow.yaml"
    if not dest.is_file():
        raise EnvRefused("pinned workflow missing from release", reason="missing")
    return dest


def deploy(
    workflow: Path | str,
    env: str,
    *,
    spec: EnvironmentSpec | None = None,
    settings: Settings | None = None,
    actor: str | None = None,
    sign_key: Path | str | None = None,
    as_candidate: bool = False,
) -> dict[str, Any]:
    pointer = pin_release(
        workflow, env=env, spec=spec, settings=settings, actor=actor, sign_key=sign_key
    )
    store = EnvStore(settings)
    if as_candidate:
        store.set_candidate(env, pointer)
        store.append_history(env, {"event": "candidate", **pointer})
    else:
        store.set_current(env, pointer)
    return pointer


def diff_releases(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Complete pin diff between two pointers. Missing side is null."""
    a = _manifest_or_empty(left, settings=settings)
    b = _manifest_or_empty(right, settings=settings)
    pins_a = dict(a.get("pins") or {})
    pins_b = dict(b.get("pins") or {})
    keys = sorted(set(pins_a) | set(pins_b))
    changed = {key: {"from": pins_a.get(key), "to": pins_b.get(key)} for key in keys}
    return {
        "from": a.get("digest"),
        "to": b.get("digest"),
        "pins": changed,
        "complete": True,
    }


def _manifest_or_empty(
    pointer: dict[str, Any] | None, *, settings: Settings | None
) -> dict[str, Any]:
    if not pointer:
        return {}
    folder = Path(str(pointer.get("path") or ""))
    manifest = folder / "manifest.json"
    if not manifest.is_file():
        return {"digest": pointer.get("digest"), "pins": pointer.get("pins") or {}}
    try:
        return verify_release(pointer, settings=settings)
    except EnvRefused:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"digest": pointer.get("digest"), "pins": pointer.get("pins") or {}}
        return data if isinstance(data, dict) else {}


def _write_snapshot(folder: Path, report: Any, source: Path) -> None:
    (folder / "workflow.yaml").write_text(
        report.source_text or source.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    for item in _all_includes(report.includes):
        dest = folder / item.path
        dest.parent.mkdir(parents=True, exist_ok=True)
        text = item.source_text
        if not text and Path(item.resolved).is_file():
            text = Path(item.resolved).read_text(encoding="utf-8")
        dest.write_text(text, encoding="utf-8")
    prompts = source.parent / "prompts"
    if prompts.is_dir():
        dest_p = folder / "prompts"
        if dest_p.exists():
            shutil.rmtree(dest_p)
        shutil.copytree(prompts, dest_p)
    lock = source.parent / "readyagents.lock"
    if lock.is_file():
        (folder / "readyagents.lock").write_bytes(lock.read_bytes())


def _all_includes(entries: list[Any]) -> list[Any]:
    out: list[Any] = []
    for item in entries:
        out.append(item)
        nested = getattr(item, "includes", None) or []
        if nested:
            out.extend(_all_includes(list(nested)))
    return out


def _pins_from_snapshot(folder: Path) -> dict[str, Any]:
    workflow = folder / "workflow.yaml"
    if not workflow.is_file():
        raise EnvRefused("pinned workflow missing from release", reason="missing")
    report = inspect_workflow(workflow)
    return {
        "workflow": report.digest,
        "includes": {item.path: item.digest for item in report.includes},
        "policy": _file_digest(folder / "policy.yaml"),
        "prompts": _prompts_digest(folder),
        "lockfile": _file_digest(folder / "readyagents.lock"),
    }


def _file_digest(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    return digest_bytes(path.read_bytes())


def _prompts_digest(folder: Path) -> str | None:
    root = folder / "prompts"
    if not root.is_dir():
        return None
    parts: list[str] = []
    for item in sorted(root.rglob("*")):
        if item.is_file():
            parts.append(f"{item.relative_to(root)}:{digest_bytes(item.read_bytes())}")
    if not parts:
        return None
    return digest_bytes("\n".join(parts).encode("utf-8"))


def _resolve(raw: str | None, workflow: Path) -> Path | None:
    if not raw:
        return None
    path = Path(raw)
    if path.is_absolute():
        return path
    return (workflow.parent / path).resolve()
