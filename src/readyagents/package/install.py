"""Install a workflow package: verify, review, confirm, copy bytes. Never execute."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from readyagents.errors import PackageNeedsConfirm, PackageRefused, TrustError
from readyagents.package.archive import digest_archive
from readyagents.package.extract import extract_archive, open_archive_bytes
from readyagents.package.layout import (
    CATALOG,
    KIND_MEMBER,
    KIND_PACKAGE,
    LOCK_NAME,
    MANIFEST_NAME,
    MAX_URL_BYTES,
)
from readyagents.package.manifest import load_manifest, satisfies_core
from readyagents.package.review import review_tree
from readyagents.package.secrets import scan_tree_for_secret_values
from readyagents.trust.digest import prefixed


def catalog_dir(home: Path) -> Path:
    return Path(home) / CATALOG


def package_dir(home: Path, name: str, version: str) -> Path:
    return catalog_dir(home) / name / version


def install_package(
    source: str | Path,
    *,
    home: Path,
    confirm: bool = False,
    policy: Any | None = None,
    keyring: Any | None = None,
    require_signature: bool = False,
) -> dict[str, Any]:
    blob, origin = _load_source(source, home)
    digest = digest_archive("archive.rapkg", data=blob)
    signature_status = _signature_status(
        source, blob, digest=digest, keyring=keyring, require_signature=require_signature
    )
    staging_parent = catalog_dir(home)
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="ra-pkg-", dir=str(staging_parent)))
    try:
        root = extract_archive(blob, staging)
        scan_tree_for_secret_values(root)
        _verify_member_lock(root)
        manifest = load_manifest(root)
        from readyagents import __version__

        if not satisfies_core(__version__, manifest.requires.readyagents):
            raise PackageRefused(
                f"package requires readyagents {manifest.requires.readyagents}, "
                f"this core is {__version__}",
                reason="conflict",
            )
        review = review_tree(root, digest=digest, signature_status=signature_status, policy=policy)
        if not confirm:
            raise PackageNeedsConfirm(
                "package install requires --confirm; nothing was written",
                review=review,
            )
        dest = package_dir(home, manifest.name, manifest.version)
        if dest.exists():
            import shutil

            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        import shutil

        shutil.copytree(root, dest, symlinks=False)
        from readyagents.package.catalog import upsert

        row = upsert(
            home,
            manifest,
            path=dest,
            digest=digest,
            signature_status=signature_status,
            source=origin,
            review=review,
        )
        return {**row, "review": review}
    finally:
        import shutil

        shutil.rmtree(staging, ignore_errors=True)


def _load_source(source: str | Path, home: Path) -> tuple[bytes, str]:
    raw = str(source).strip()
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"}:
        return _fetch_url(raw, home), raw
    path = Path(raw).expanduser()
    if not path.is_file():
        raise PackageRefused(f"package archive not found: {path}", reason="missing")
    return open_archive_bytes(path), str(path.resolve())


def _fetch_url(url: str, home: Path) -> bytes:
    import urllib.request

    catalog_dir(home).mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
            data = resp.read(MAX_URL_BYTES + 1)
    except OSError as extra:
        raise PackageRefused(f"package URL fetch failed: {extra}", reason="url") from extra
    if len(data) > MAX_URL_BYTES:
        raise PackageRefused("package URL exceeds size cap", reason="too_large")
    return data


def _signature_status(
    source: str | Path,
    blob: bytes,
    *,
    digest: str,
    keyring: Any | None,
    require_signature: bool,
) -> str:
    path = Path(str(source))
    sig = Path(str(source) + ".sig") if path.suffix else None
    if path.is_file():
        from readyagents.trust.sign import signature_path

        sig = signature_path(path)
    if sig is None or not sig.is_file():
        if require_signature:
            raise PackageRefused("unsigned package: signature required", reason="unsigned")
        return "unsigned"
    from readyagents.trust.sign import verify_artifact

    try:
        verify_artifact(path, kind=KIND_PACKAGE, data=blob, keyring=keyring, digest=digest)
        return "signed"
    except TrustError as extra:
        if extra.reason == "unsigned":
            raise PackageRefused(
                "unsigned package: signature required", reason="unsigned"
            ) from extra
        raise PackageRefused("package signature mismatch", reason="forged") from extra


def _verify_member_lock(root: Path) -> None:
    lock_path = root / LOCK_NAME
    if not lock_path.is_file():
        raise PackageRefused("package lockfile missing", reason="missing_lockfile")
    try:
        data = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as extra:
        raise PackageRefused("package lockfile malformed", reason="malformed") from extra
    if not isinstance(data, dict) or not isinstance(data.get("artifacts"), list):
        raise PackageRefused("package lockfile malformed", reason="malformed")
    expected = {
        str(row.get("path")): prefixed(str(row.get("digest") or ""))
        for row in data["artifacts"]
        if isinstance(row, dict) and row.get("kind") == KIND_MEMBER
    }
    actual: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel == LOCK_NAME:
            continue
        actual[rel] = prefixed(_digest_file(path))
    if set(expected) != set(actual):
        raise PackageRefused("package lockfile member set mismatch", reason="digest_mismatch")
    for rel, digest in expected.items():
        if actual.get(rel) != digest:
            raise PackageRefused(f"package member digest mismatch: {rel}", reason="digest_mismatch")
    # Manifest must still parse after lock check.
    if not (root / MANIFEST_NAME).is_file():
        raise PackageRefused("package manifest missing after extract", reason="missing")


def _digest_file(path: Path) -> str:
    from readyagents.trust.digest import digest_bytes

    return digest_bytes(path.read_bytes())
