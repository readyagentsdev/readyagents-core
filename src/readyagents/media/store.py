"""Content-addressed media store. SHA-256 layout, confined, never base64 in records."""

from __future__ import annotations

import hashlib
from pathlib import Path

from readyagents.atomic import atomic_write_text
from readyagents.errors import MediaError, PathError
from readyagents.paths import resolve_within
from readyagents.permissions import restrict_file


class MediaStore:
    """``root / aa / sha256`` blobs plus an optional cassette sidecar."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._bytes = 0

    def put(self, data: bytes, *, sha256: str | None = None) -> str:
        digest = sha256 or hashlib.sha256(data).hexdigest()
        path = self.path_for(digest)
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            restrict_file(tmp)
            tmp.replace(path)
            restrict_file(path)
        self._bytes += len(data)
        return digest

    def get(self, sha256: str) -> bytes:
        path = self.path_for(sha256)
        if not path.is_file():
            raise MediaError(f"media blob not found: {sha256}")
        return path.read_bytes()

    def has(self, sha256: str) -> bool:
        return self.path_for(sha256).is_file()

    def delete(self, sha256: str) -> None:
        path = self.path_for(sha256)
        if not path.is_file():
            return
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        try:
            path.unlink()
        except OSError:
            return
        self._bytes = max(0, self._bytes - int(size))

    def path_for(self, sha256: str) -> Path:
        digest = str(sha256).strip().lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise MediaError(f"invalid media hash: {sha256!r}")
        dest = self.root / digest[:2] / digest
        try:
            return resolve_within(dest, self.root, what="media blob")
        except PathError as exc:
            raise MediaError(str(exc)) from exc

    def used_bytes(self) -> int:
        return int(self._bytes)

    def copy_hashes_to(self, dest_root: Path, hashes: list[str]) -> None:
        """Copy named blobs into another store root (cassette / evidence sidecar)."""
        other = MediaStore(dest_root)
        for digest in hashes:
            if self.has(digest):
                other.put(self.get(digest), sha256=digest)


def write_hash_manifest(path: Path, hashes: list[str]) -> None:
    lines = "\n".join(sorted(set(hashes))) + ("\n" if hashes else "")
    atomic_write_text(path, lines, encoding="utf-8", newline="\n", restrict=True)
