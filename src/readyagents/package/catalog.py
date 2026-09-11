"""Local package catalog. No marketplace, no auto-update."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from readyagents.atomic import atomic_write_text
from readyagents.errors import PackageNeedsConfirm, PackageRefused
from readyagents.package.install import install_package, package_dir
from readyagents.package.layout import CATALOG, OVERLAY_NAME
from readyagents.package.manifest import PackageManifest, version_tuple
from readyagents.package.review import permission_widening
from readyagents.permissions import restrict_file

INDEX_NAME = "index.json"


def catalog_dir(home: Path) -> Path:
    return Path(home) / CATALOG


def overlay_path(home: Path, name: str) -> Path:
    return catalog_dir(home) / name / OVERLAY_NAME


def load_index(home: Path) -> dict[str, Any]:
    path = catalog_dir(home) / INDEX_NAME
    if not path.is_file():
        return {"packages": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"packages": {}}
    if not isinstance(raw, dict):
        return {"packages": {}}
    if not isinstance(raw.get("packages"), dict):
        raw["packages"] = {}
    return raw


def save_index(home: Path, index: dict[str, Any]) -> None:
    dest = catalog_dir(home)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / INDEX_NAME
    atomic_write_text(
        path,
        json.dumps(index, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
        restrict=True,
    )
    restrict_file(path)


def list_records(home: Path) -> list[dict[str, Any]]:
    rows = list((load_index(home).get("packages") or {}).values())
    return [row for row in rows if isinstance(row, dict)]


def get_record(home: Path, name: str) -> dict[str, Any]:
    row = (load_index(home).get("packages") or {}).get(name)
    if not isinstance(row, dict):
        raise PackageRefused(f"package not installed: {name}", reason="missing")
    return row


def upsert(
    home: Path,
    manifest: PackageManifest,
    *,
    path: Path,
    digest: str,
    signature_status: str,
    source: str,
    review: dict[str, Any],
) -> dict[str, Any]:
    row = {
        "name": manifest.name,
        "version": manifest.version,
        "description": manifest.description,
        "entry": manifest.entry,
        "digest": digest,
        "signature_status": signature_status,
        "source": source,
        "path": str(path),
        "fixtures": manifest.fixtures,
        "tools": list(review.get("tools") or []),
        "secrets": list(manifest.secrets),
        "budget": review.get("budget") or {},
        "constrained": bool(review.get("constrained")),
    }
    index = load_index(home)
    packages = index.setdefault("packages", {})
    packages[manifest.name] = row
    save_index(home, index)
    return row


def remove_name(home: Path, name: str) -> None:
    index = load_index(home)
    packages = index.setdefault("packages", {})
    if name not in packages:
        raise PackageRefused(f"package not installed: {name}", reason="missing")
    packages.pop(name, None)
    save_index(home, index)
    folder = catalog_dir(home) / name
    if folder.is_dir():
        shutil.rmtree(folder)


def load_overlay(home: Path, name: str) -> dict[str, Any]:
    path = overlay_path(home, name)
    if not path.is_file():
        return {}
    import yaml

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def save_overlay(home: Path, name: str, overlay: dict[str, Any]) -> Path:
    dest = overlay_path(home, name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    import yaml

    atomic_write_text(
        dest,
        yaml.safe_dump(overlay, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
        restrict=True,
    )
    return dest


def upgrade_package(
    name: str,
    source: str | Path,
    *,
    home: Path,
    confirm: bool = False,
    policy: Any | None = None,
    keyring: Any | None = None,
) -> dict[str, Any]:
    current = get_record(home, name)
    overlay = load_overlay(home, name)
    previous_review = {
        "tools": list(current.get("tools") or []),
        "hosts": [],
        "secrets": list(current.get("secrets") or []),
        "budget": current.get("budget") or {},
    }
    # Peek without confirm: install_package will refuse; capture review.
    try:
        install_package(source, home=home, confirm=False, policy=policy, keyring=keyring)
    except PackageNeedsConfirm as extra:
        review = extra.review or {}
    else:
        raise PackageRefused("upgrade preview did not require confirmation", reason="malformed")
    if review.get("name") != name:
        raise PackageRefused(
            f"upgrade name mismatch: installed {name} archive {review.get('name')}",
            reason="conflict",
        )
    widening = permission_widening(previous_review, review)
    review = {**review, "upgrade": widening, "from_version": current.get("version")}
    if widening["widened"] and not confirm:
        raise PackageNeedsConfirm(
            "upgrade widens permissions and requires --confirm; nothing was written",
            review=review,
        )
    if not confirm:
        raise PackageNeedsConfirm(
            "package upgrade requires --confirm; nothing was written",
            review=review,
        )
    # Preserve overlay across tree replace.
    result = install_package(source, home=home, confirm=True, policy=policy, keyring=keyring)
    if overlay:
        save_overlay(home, name, overlay)
        result["overlay"] = overlay
    old_version = str(current.get("version") or "")
    new_version = str(result.get("version") or "")
    if old_version and new_version and old_version != new_version:
        stale = package_dir(home, name, old_version)
        if stale.is_dir():
            shutil.rmtree(stale)
    result["from_version"] = old_version
    result["version_cmp"] = _cmp(old_version, new_version)
    result["review"] = {**(result.get("review") or {}), "upgrade": widening}
    return result


def _cmp(old: str, new: str) -> str:
    try:
        a, b = version_tuple(old), version_tuple(new)
    except (TypeError, ValueError):
        return "unknown"
    if b > a:
        return "newer"
    if b < a:
        return "older"
    return "same"
