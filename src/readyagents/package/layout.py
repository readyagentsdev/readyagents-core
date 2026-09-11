"""Frozen package layout, kinds, and caps. Shared by build, install, and index.

A workflow package is **not** a Python ``kind: pack`` file. The archive kind is
``package``; the static catalog index kind is ``package_index``.
"""

from __future__ import annotations

from readyagents.trust.digest import KIND_INDEX, KIND_PACKAGE

__all__ = [
    "KIND_INDEX",
    "KIND_PACKAGE",
    "KIND_MEMBER",
    "MANIFEST_NAME",
    "LOCK_NAME",
    "OVERLAY_NAME",
    "ARCHIVE_SUFFIX",
    "INDEX_NAME",
    "CATALOG",
]

MANIFEST_NAME = "readyagents.pkg.yaml"
LOCK_NAME = "readyagents.lock"
OVERLAY_NAME = "overlay.yaml"
ARCHIVE_SUFFIX = ".rapkg"
INDEX_NAME = "index.json"
KIND_MEMBER = "package_member"

CATALOG = "packages"

MAX_ARCHIVE_BYTES = 10_485_760  # 10 MiB
MAX_MEMBER_BYTES = 1_048_576
MAX_FILES = 256
MAX_DEPTH = 8
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_URL_BYTES = 10_485_760

# Deterministic zip: stored, DOS epoch, Unix file attrs, sorted names.
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
FILE_ATTR = 0o644 << 16
DIR_ATTR = 0o755 << 16
