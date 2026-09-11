"""Frozen contract for ``type: code``: JSON stdin/stdout, limits, allowlist."""

from __future__ import annotations

from typing import Any

EXIT_OK = 0
EXIT_NO_RESULT = 10
EXIT_IMPORT = 11
EXIT_FS = 12
EXIT_NET = 13
EXIT_OUTPUT = 14
EXIT_CPU = 15
EXIT_MEM = 16
EXIT_FILE = 17
EXIT_NPROC = 18
EXIT_OTHER = 19

DEFAULT_CPU_SECONDS = 5
DEFAULT_MEMORY_MB = 512
DEFAULT_WALL_SECONDS = 15
DEFAULT_OUTPUT_BYTES = 1_048_576
DEFAULT_FILE_SIZE_BYTES = 4_194_304
DEFAULT_NPROC = 8

# Stdlib minus process / network / filesystem escape hatches.
DEFAULT_ALLOW_IMPORTS: tuple[str, ...] = (
    "abc",
    "array",
    "base64",
    "bisect",
    "calendar",
    "collections",
    "contextlib",
    "copy",
    "csv",
    "dataclasses",
    "datetime",
    "decimal",
    "enum",
    "fractions",
    "functools",
    "hashlib",
    "heapq",
    "hmac",
    "itertools",
    "json",
    "keyword",
    "math",
    "numbers",
    "operator",
    "pprint",
    "random",
    "re",
    "statistics",
    "string",
    "struct",
    "textwrap",
    "time",
    "types",
    "typing",
    "unicodedata",
    "warnings",
    "zoneinfo",
)

ALWAYS_DENY_IMPORTS: tuple[str, ...] = (
    "ctypes",
    "_ctypes",
    "cffi",
)

NETWORK_IMPORTS: tuple[str, ...] = (
    "socket",
    "ssl",
    "http",
    "urllib",
    "ftplib",
    "smtplib",
    "poplib",
    "imaplib",
    "nntplib",
    "xmlrpc",
    "asyncio",
    "requests",
    "httpx",
    "aiohttp",
)

ISOLATION_SUBPROCESS = "subprocess"
ISOLATION_CONTAINER = "container"


def normalize_isolation(raw: str | None) -> str:
    value = (raw or ISOLATION_SUBPROCESS).strip().lower()
    if value not in {ISOLATION_SUBPROCESS, ISOLATION_CONTAINER}:
        return ISOLATION_SUBPROCESS
    return value


def merge_limits(raw: dict[str, Any] | None) -> dict[str, int]:
    data = dict(raw or {})
    cpu = int(data.get("cpu_seconds") or DEFAULT_CPU_SECONDS)
    memory = int(data.get("memory_mb") or DEFAULT_MEMORY_MB)
    wall = int(data.get("wall_seconds") or DEFAULT_WALL_SECONDS)
    output = int(data.get("output_bytes") or DEFAULT_OUTPUT_BYTES)
    file_size = int(data.get("file_size_bytes") or data.get("file_size") or DEFAULT_FILE_SIZE_BYTES)
    nproc = int(data.get("nproc") or data.get("process_count") or DEFAULT_NPROC)
    return {
        "cpu_seconds": max(1, cpu),
        "memory_mb": max(32, memory),
        "wall_seconds": max(1, wall),
        "output_bytes": max(64, output),
        "file_size_bytes": max(64, file_size),
        "nproc": max(1, nproc),
    }
