"""Offline install bundle: wheels + manifest checksums for pip --no-index."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

from readyagents import __version__
from readyagents.atomic import atomic_write_text
from readyagents.errors import ConfigError

MANIFEST_NAME = "manifest.json"
_PEP503 = re.compile(r"[-_.]+")


def _pep503_name(name: str) -> str:
    return _PEP503.sub("-", name.strip().lower()).strip("-")


def _requirement_name(spec: str) -> str:
    raw = spec.strip()
    for sep in ("[", ">", "<", "=", "!", "~", ";", " "):
        if sep in raw:
            raw = raw.split(sep, 1)[0]
    return raw.strip()


def _runtime_requirement_names(root: Path) -> list[str]:
    pyproject = Path(root) / "pyproject.toml"
    if not pyproject.is_file():
        raise ConfigError(f"bundle: pyproject.toml not found under {root}")
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    raw = data.get("project", {}).get("dependencies") or []
    names = [_requirement_name(str(item)) for item in raw if str(item).strip()]
    if not names:
        raise ConfigError(f"bundle: no runtime dependencies declared in {pyproject}")
    return names


def _artifact_dist(filename: str) -> str:
    base = filename
    lower = base.lower()
    for suffix in (".tar.gz", ".whl", ".zip", ".tar.bz2"):
        if lower.endswith(suffix):
            base = base[: -len(suffix)]
            break
    parts = base.split("-")
    dist: list[str] = []
    for part in parts:
        if part and part[0].isdigit():
            break
        dist.append(part)
    return _pep503_name("-".join(dist) if dist else parts[0])


def _missing_runtime_wheels(filenames: list[str], requirements: list[str]) -> list[str]:
    present = {_artifact_dist(name) for name in filenames}
    missing: list[str] = []
    for req in requirements:
        if _pep503_name(req) not in present:
            missing.append(req)
    return missing


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def write_bundle(
    dest: Path,
    *,
    python: str | None = None,
    platform: str | None = None,
    project: Path | None = None,
) -> dict[str, Any]:
    """Write wheels into dest plus a checksum manifest.

    One target platform/Python per invocation. pip may be used; the caller
    supplies network when collecting third-party wheels.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    root = Path(project) if project is not None else Path.cwd()
    py = python or f"{sys.version_info.major}.{sys.version_info.minor}"
    plat = platform or sys.platform
    build_cmd = [
        sys.executable,
        "-m",
        "build",
        "--wheel",
        "--outdir",
        str(dest),
        str(root),
    ]
    proc = subprocess.run(build_cmd, check=False, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise ConfigError(
            f"bundle: could not build the project wheel: {proc.stderr or proc.stdout}"
        ) from None
    requirements = _runtime_requirement_names(root)
    download = [
        sys.executable,
        "-m",
        "pip",
        "download",
        *requirements,
        "-d",
        str(dest),
        "--disable-pip-version-check",
    ]
    if python:
        download.extend(["--python-version", python])
    if platform:
        download.extend(["--platform", platform, "--only-binary=:all:"])
    try:
        got = subprocess.run(download, check=False, capture_output=True, text=True, timeout=300)
    except OSError as extra:
        raise ConfigError(
            "bundle: pip is not available (python -m pip). Install pip, then retry."
        ) from extra
    except subprocess.TimeoutExpired as extra:
        raise ConfigError("bundle: pip download timed out") from extra
    if got.returncode != 0:
        raise ConfigError(f"bundle: pip download failed: {got.stderr or got.stdout}") from None
    files: list[dict[str, str]] = []
    for path in sorted(dest.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        if path.suffix not in {".whl", ".gz", ".zip"} and ".tar" not in path.name:
            continue
        files.append({"name": path.name, "digest": sha256_file(path)})
    if not files:
        raise ConfigError(f"bundle: no wheels written under {dest}")
    missing = _missing_runtime_wheels([row["name"] for row in files], requirements)
    if missing:
        raise ConfigError("bundle: missing runtime dependency wheels: " + ", ".join(missing))
    payload = {
        "version": 1,
        "readyagents_version": __version__,
        "python": py,
        "platform": plat,
        "files": files,
        "install": f"pip install --no-index --find-links {dest} readyagentsdev",
    }
    atomic_write_text(
        dest / MANIFEST_NAME,
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return payload


def verify_manifest(dest: Path) -> dict[str, Any]:
    folder = Path(dest)
    manifest_path = folder / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ConfigError(f"bundle manifest missing: {manifest_path}")
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = data.get("files")
    if not isinstance(files, list) or not files:
        raise ConfigError("bundle manifest has no files")
    for row in files:
        name = str(row.get("name") or "")
        expected = str(row.get("digest") or "")
        path = folder / name
        if not path.is_file():
            raise ConfigError(f"bundle missing file: {name}")
        actual = sha256_file(path)
        if actual != expected:
            raise ConfigError(f"bundle checksum mismatch for {name}")
    return data
