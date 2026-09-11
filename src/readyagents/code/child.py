"""Stdlib-only sandbox child. Copied into the per-run dir; never imports readyagents."""

from __future__ import annotations

import builtins
import json
import os
import sys
import threading
import time
from pathlib import Path

try:
    import resource as _RESOURCE
except ImportError:
    _RESOURCE = None

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

_CFG: dict = {}
_ALLOWED: set[str] = set()
_NETWORK = False
_SANDBOX = Path(".")
_READ: list[Path] = []
_WRITE: list[Path] = []
_FILE_SIZE = 4_194_304
_NPROC = 8
_FORKS = 0
_REAL_OPEN = builtins.open
_REAL_IMPORT = builtins.__import__


def _cfg_load() -> None:
    global _CFG, _ALLOWED, _NETWORK, _SANDBOX, _READ, _WRITE, _FILE_SIZE, _NPROC
    raw = Path("_sandbox.json").read_text(encoding="utf-8")
    _CFG = json.loads(raw)
    _ALLOWED = set(_CFG.get("allow_imports") or [])
    _NETWORK = bool(_CFG.get("network"))
    _SANDBOX = Path(_CFG.get("sandbox") or ".").resolve()
    _READ = [Path(p).resolve() for p in (_CFG.get("read") or [])]
    _WRITE = [Path(p).resolve() for p in (_CFG.get("write") or [])]
    limits = _CFG.get("limits") or {}
    _FILE_SIZE = int(limits.get("file_size_bytes") or _FILE_SIZE)
    _NPROC = int(limits.get("nproc") or _NPROC)


def _contained(path: Path, root: Path) -> bool:
    try:
        return path == root or path.is_relative_to(root)
    except (OSError, ValueError, AttributeError):
        return False


def _grant_ok(path: Path, *, write: bool) -> bool:
    resolved = path if path.is_absolute() else (_SANDBOX / path)
    try:
        resolved = resolved.resolve()
    except OSError:
        return False
    if _contained(resolved, _SANDBOX):
        return True
    roots = _WRITE if write else (_READ + _WRITE)
    return any(_contained(resolved, root) for root in roots)


def _sandbox_open(file, mode="r", *args, **kwargs):
    path = Path(file if not hasattr(file, "name") or isinstance(file, (str, bytes)) else str(file))
    text_mode = str(mode)
    writing = any(flag in text_mode for flag in "wxa+")
    if not _grant_ok(path, write=writing):
        sys.stderr.write("filesystem grant denied\n")
        raise SystemExit(EXIT_FS)
    handle = _REAL_OPEN(file, mode, *args, **kwargs)
    if writing:
        return _CappedFile(handle)
    return handle


class _CappedFile:
    def __init__(self, inner) -> None:
        self._inner = inner
        self._written = 0

    def write(self, data):
        size = len(data) if isinstance(data, (bytes, bytearray, str)) else 0
        self._written += size
        if self._written > _FILE_SIZE:
            sys.stderr.write("file size limit exceeded\n")
            raise SystemExit(EXIT_FILE)
        return self._inner.write(data)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *args):
        return self._inner.__exit__(*args)


def _count_fork(*_a, **_k):
    global _FORKS
    _FORKS += 1
    sys.stderr.write("process count limit exceeded\n")
    raise SystemExit(EXIT_NPROC)


def _wrap_os(mod) -> None:
    if getattr(mod, "_readyagents_wrapped", False):
        return
    for name in (
        "fork",
        "forkpty",
        "posix_spawn",
        "posix_spawnp",
        "system",
        "popen",
        "execl",
        "execv",
    ):
        if hasattr(mod, name):
            setattr(mod, name, _count_fork)
    mod._readyagents_wrapped = True


def _wrap_subprocess(mod) -> None:
    if getattr(mod, "_readyagents_wrapped", False):
        return
    mod.Popen = _count_fork
    mod.call = _count_fork
    mod.run = _count_fork
    mod._readyagents_wrapped = True


_NET_TOP = {
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
}
_ALWAYS_DENY = {"ctypes", "_ctypes", "cffi"}


def _import(name, globals=None, locals=None, fromlist=(), level=0):
    top = str(name).split(".", 1)[0]
    if top in _ALWAYS_DENY:
        sys.stderr.write(f"import {name!r} is not allowed\n")
        raise SystemExit(EXIT_IMPORT)
    if top in _NET_TOP and not _NETWORK:
        sys.stderr.write("sandbox network is denied\n")
        raise SystemExit(EXIT_NET)
    if top not in _ALLOWED and top not in {"builtins"}:
        sys.stderr.write(f"import {name!r} is not on the allowlist\n")
        raise SystemExit(EXIT_IMPORT)
    mod = _REAL_IMPORT(name, globals, locals, fromlist, level)
    if top == "os":
        _wrap_os(mod)
    if top == "subprocess":
        _wrap_subprocess(mod)
    return mod


def _arm_cpu_watch(seconds: float) -> None:
    """Exit EXIT_CPU when process CPU time hits the declared limit.

    Wall-clock sleep does not count. Used on Windows (no RLIMIT_CPU) and as
    a backup on Unix.
    """
    limit = float(seconds)
    if limit <= 0:
        return

    def _watch() -> None:
        while True:
            time.sleep(0.05)
            if time.process_time() >= limit:
                sys.stderr.write("CPU time limit exceeded\n")
                os._exit(EXIT_CPU)

    threading.Thread(target=_watch, daemon=True).start()


def _rss_bytes() -> int:
    """Current resident set. 0 if the platform cannot measure it."""
    if os.name == "nt":
        return _rss_windows()
    try:
        # Binary + real open: Path.read_text(encoding=) imports encodings.*
        # through the sandbox hook and kills the child with EXIT_IMPORT.
        with _REAL_OPEN("/proc/self/statm", "rb") as fh:
            parts = fh.read().split()
        if len(parts) >= 2:
            return int(parts[1]) * int(os.sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError):
        pass
    if _RESOURCE is None:
        return 0
    try:
        rss = int(_RESOURCE.getrusage(_RESOURCE.RUSAGE_SELF).ru_maxrss)
        # Linux is KB. Darwin is bytes; some CI Pythons report KB (values < 1MiB
        # cannot be a live CPython RSS measured in bytes).
        if sys.platform.startswith("linux"):
            return rss * 1024
        if sys.platform == "darwin" and 0 < rss < 1_000_000:
            return rss * 1024
        return rss
    except Exception:
        return 0


def _rss_windows() -> int:
    try:
        ctypes = _REAL_IMPORT("ctypes")
        wintypes = _REAL_IMPORT("ctypes.wintypes")
    except ImportError:
        return 0

    class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        _fields_ = (
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        )

    counters = PROCESS_MEMORY_COUNTERS_EX()
    counters.cb = ctypes.sizeof(counters)
    try:
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        handle = kernel32.GetCurrentProcess()
        ok = psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
        if ok:
            return int(counters.WorkingSetSize or counters.PrivateUsage)
    except Exception:
        return 0
    return 0


def _arm_mem_watch(memory_mb: float) -> None:
    """Exit EXIT_MEM when RSS reaches the declared megabyte cap.

    macOS cannot lower RLIMIT_AS; Windows has no resource module. This watch
    is the portable memory limit.
    """
    limit = int(float(memory_mb) * 1024 * 1024)
    if limit <= 0:
        return

    def _trip() -> None:
        sys.stderr.write("memory / address-space limit exceeded\n")
        os._exit(EXIT_MEM)

    def _watch() -> None:
        while True:
            time.sleep(0.02)
            if _rss_bytes() >= limit:
                _trip()

    threading.Thread(target=_watch, daemon=True).start()


def main() -> int:
    _cfg_load()
    limits = _CFG.get("limits") or {}
    _arm_cpu_watch(float(limits.get("cpu_seconds") or 0))
    _arm_mem_watch(float(limits.get("memory_mb") or 0))
    builtins.open = _sandbox_open
    builtins.__import__ = _import
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.stderr.write("stdin is not JSON\n")
        return EXIT_NO_RESULT
    if not isinstance(payload, dict):
        payload = {"value": payload}
    ns: dict = {
        "__name__": "__sandbox__",
        "inputs": payload,
        "result": None,
    }
    source = Path("user_code.py").read_text(encoding="utf-8")
    try:
        compiled = compile(source, "user_code.py", "exec")
        exec(compiled, ns, ns)  # noqa: S102 — isolated child
    except MemoryError:
        sys.stderr.write("memory / address-space limit exceeded\n")
        return EXIT_MEM
    except OSError as exc:
        if getattr(exc, "errno", None) == 12:  # ENOMEM
            sys.stderr.write("memory / address-space limit exceeded\n")
            return EXIT_MEM
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        return EXIT_OTHER
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, int):
            return code
        return EXIT_OTHER
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        return EXIT_OTHER
    result = ns.get("result", None)
    if result is None:
        sys.stderr.write("code node must assign result\n")
        return EXIT_NO_RESULT
    try:
        blob = json.dumps(result, ensure_ascii=False)
    except TypeError as exc:
        sys.stderr.write(f"result is not JSON-serialisable: {exc}\n")
        return EXIT_NO_RESULT
    sys.stdout.write(blob)
    sys.stdout.write("\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
