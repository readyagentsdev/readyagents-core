"""Subprocess isolation for ``type: code``. No secrets, closed fds, confined cwd."""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from readyagents.code.protocol import (
    EXIT_CPU,
    EXIT_FILE,
    EXIT_FS,
    EXIT_IMPORT,
    EXIT_MEM,
    EXIT_NET,
    EXIT_NO_RESULT,
    EXIT_NPROC,
    EXIT_OUTPUT,
    ISOLATION_CONTAINER,
    merge_limits,
    normalize_isolation,
)
from readyagents.errors import (
    CodeContainerUnavailable,
    CodeCpuLimitExceeded,
    CodeError,
    CodeFileSizeLimitExceeded,
    CodeFilesystemDenied,
    CodeImportDenied,
    CodeMemoryLimitExceeded,
    CodeNetworkDenied,
    CodeOutputLimitExceeded,
    CodeProcessLimitExceeded,
    CodeSchemaError,
    CodeWallLimitExceeded,
)

_CHILD = Path(__file__).with_name("child.py")


def spawn_sandboxed(
    *,
    node_id: str,
    source: str,
    inputs: dict[str, Any],
    isolation: str | None,
    require_isolation: str | None,
    limits: dict[str, Any] | None,
    allow_imports: list[str] | None,
    network: bool,
    read_roots: list[Path],
    write_roots: list[Path],
    workspace: Path,
) -> dict[str, Any]:
    """Run ``source`` in a child interpreter. The only Popen site for code nodes."""
    tier = normalize_isolation(isolation)
    required = normalize_isolation(require_isolation) if require_isolation else tier
    if required == ISOLATION_CONTAINER or tier == ISOLATION_CONTAINER:
        raise CodeContainerUnavailable(node_id)
    bound = merge_limits(limits)
    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    sandbox = Path(tempfile.mkdtemp(prefix="ra-code-", dir=str(root)))
    try:
        return _run_subprocess(
            node_id=node_id,
            source=source,
            inputs=inputs,
            bound=bound,
            allow_imports=allow_imports,
            network=network,
            read_roots=read_roots,
            write_roots=write_roots,
            sandbox=sandbox,
        )
    finally:
        _cleanup(sandbox)


def _run_subprocess(
    *,
    node_id: str,
    source: str,
    inputs: dict[str, Any],
    bound: dict[str, int],
    allow_imports: list[str] | None,
    network: bool,
    read_roots: list[Path],
    write_roots: list[Path],
    sandbox: Path,
) -> dict[str, Any]:
    import subprocess

    from readyagents.code.protocol import DEFAULT_ALLOW_IMPORTS

    allowed = list(allow_imports) if allow_imports else list(DEFAULT_ALLOW_IMPORTS)
    config = {
        "allow_imports": allowed,
        "network": bool(network),
        "sandbox": str(sandbox.resolve()),
        "read": [str(p) for p in read_roots],
        "write": [str(p) for p in write_roots],
        "limits": bound,
    }
    (sandbox / "_sandbox.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    (sandbox / "user_code.py").write_text(source, encoding="utf-8")
    (sandbox / "_bootstrap.py").write_text(_CHILD.read_text(encoding="utf-8"), encoding="utf-8")
    env = _minimal_env(sandbox)
    stdin_blob = json.dumps(inputs, ensure_ascii=False).encode("utf-8")
    wall = float(bound["wall_seconds"])
    preexec = None
    if os.name != "nt":
        preexec = _unix_preexec(sandbox, bound)
    kwargs: dict[str, Any] = {
        "args": [sys.executable, "-I", "-S", "-u", str(sandbox / "_bootstrap.py")],
        "cwd": str(sandbox),
        "env": env,
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "start_new_session": os.name != "nt",
    }
    if os.name != "nt":
        kwargs["close_fds"] = True
        kwargs["preexec_fn"] = preexec
    proc = subprocess.Popen(**kwargs)  # noqa: S603
    started = time.monotonic()
    mem_bytes = int(bound["memory_mb"]) * 1024 * 1024
    stdout, stderr = _wait_child(
        proc,
        stdin_blob=stdin_blob,
        wall=wall,
        mem_bytes=mem_bytes,
        node_id=node_id,
    )
    elapsed = time.monotonic() - started
    out_limit = int(bound["output_bytes"])
    if len(stdout) + len(stderr) > out_limit:
        raise CodeOutputLimitExceeded(node_id)
    code = int(proc.returncode or 0)
    text_out = stdout.decode("utf-8", "replace")
    text_err = stderr.decode("utf-8", "replace")
    _raise_exit(
        node_id,
        code,
        text_err,
        cpu_seconds=float(bound["cpu_seconds"]),
        elapsed=elapsed,
    )
    if not text_out.strip():
        raise CodeSchemaError(node_id, "code node produced empty stdout")
    try:
        payload = json.loads(text_out)
    except json.JSONDecodeError as exc:
        raise CodeSchemaError(node_id, f"code stdout is not JSON: {exc}") from exc
    return {
        "output": payload,
        "stdout": text_out,
        "stderr": text_err,
        "exit": code,
        "tier": "subprocess",
        "sandbox": str(sandbox),
    }


def _raise_exit(
    node_id: str,
    code: int,
    stderr: str,
    *,
    cpu_seconds: float = 0.0,
    elapsed: float = 0.0,
) -> None:
    if code in {0, None}:
        return
    mapping = {
        EXIT_IMPORT: CodeImportDenied,
        EXIT_FS: CodeFilesystemDenied,
        EXIT_NET: CodeNetworkDenied,
        EXIT_OUTPUT: CodeOutputLimitExceeded,
        EXIT_CPU: CodeCpuLimitExceeded,
        EXIT_MEM: CodeMemoryLimitExceeded,
        EXIT_FILE: CodeFileSizeLimitExceeded,
        EXIT_NPROC: CodeProcessLimitExceeded,
        EXIT_NO_RESULT: CodeSchemaError,
    }
    cls = mapping.get(int(code))
    if cls is CodeSchemaError:
        raise cls(node_id, stderr.strip() or "code node did not assign a JSON result")
    if cls is not None:
        raise cls(node_id)
    if int(code) < 0:
        sig = -int(code)
        if hasattr(signal, "SIGXCPU") and sig == signal.SIGXCPU:
            raise CodeCpuLimitExceeded(node_id)
        if hasattr(signal, "SIGXFSZ") and sig == signal.SIGXFSZ:
            raise CodeFileSizeLimitExceeded(node_id)
        if hasattr(signal, "SIGSEGV") and sig == signal.SIGSEGV:
            raise CodeMemoryLimitExceeded(node_id)
        if hasattr(signal, "SIGKILL") and sig == signal.SIGKILL:
            # Immediate SIGKILL is usually RLIMIT_AS; CPU rlimit fires near cpu_seconds.
            if elapsed < max(0.4, float(cpu_seconds) * 0.4):
                raise CodeMemoryLimitExceeded(node_id)
            raise CodeCpuLimitExceeded(node_id)
    raise CodeError(node_id, stderr.strip() or f"code child exited {code}")


def _unix_preexec(sandbox: Path, bound: dict[str, int]):
    def _apply() -> None:
        os.chdir(sandbox)
        try:
            import resource
        except ImportError:
            return
        cpu = max(1, int(bound["cpu_seconds"]))
        mem = max(32, int(bound["memory_mb"])) * 1024 * 1024
        fsize = max(64, int(bound["file_size_bytes"]))
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        except (ValueError, OSError):
            pass
        as_limit = getattr(resource, "RLIMIT_AS", None)
        if as_limit is not None:
            try:
                resource.setrlimit(as_limit, (mem, mem))
            except (ValueError, OSError):
                pass
        try:
            resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
        except (ValueError, OSError):
            pass
        try:
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except (ValueError, OSError):
            pass

    return _apply


def _minimal_env(sandbox: Path) -> dict[str, str]:
    env = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "HOME": str(sandbox),
    }
    if os.name == "nt":
        for key in ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATHEXT", "COMSPEC"):
            value = os.environ.get(key)
            if value:
                env[key] = value
        env["TEMP"] = str(sandbox)
        env["TMP"] = str(sandbox)
    return env


def _wait_child(
    proc: Any,
    *,
    stdin_blob: bytes,
    wall: float,
    mem_bytes: int,
    node_id: str,
) -> tuple[bytes, bytes]:
    """Wait with wall-clock timeout; poll child RSS so memory is not billed as wall.

    A single communicate(timeout=wall) cannot see RSS. Zero-filled pages on
    Darwin stay compressed, so the child thread may also miss the cap.
    """
    import subprocess

    started = time.monotonic()
    try:
        if proc.stdin is not None:
            proc.stdin.write(stdin_blob)
            proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    proc.stdin = None
    while True:
        remaining = float(wall) - (time.monotonic() - started)
        if remaining <= 0:
            _kill(proc)
            proc.communicate()
            raise CodeWallLimitExceeded(node_id) from None
        try:
            stdout, stderr = proc.communicate(timeout=min(0.2, remaining))
            return stdout or b"", stderr or b""
        except subprocess.TimeoutExpired:
            rss = _child_rss_bytes(proc.pid)
            if mem_bytes and rss >= mem_bytes:
                _kill(proc)
                proc.communicate()
                raise CodeMemoryLimitExceeded(node_id) from None


def _child_rss_bytes(pid: int | None) -> int:
    if not pid:
        return 0
    if sys.platform == "darwin":
        return _darwin_rss(pid)
    if sys.platform.startswith("linux"):
        try:
            parts = Path(f"/proc/{pid}/statm").read_text(encoding="ascii").split()
            return int(parts[1]) * int(os.sysconf("SC_PAGE_SIZE"))
        except (OSError, ValueError, IndexError):
            return 0
    if os.name == "nt":
        return _windows_child_rss(pid)
    return 0


def _darwin_rss(pid: int) -> int:
    try:
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

        class proc_taskinfo(ctypes.Structure):
            _fields_ = (
                ("pti_virtual_size", ctypes.c_uint64),
                ("pti_resident_size", ctypes.c_uint64),
                ("pti_total_user", ctypes.c_uint64),
                ("pti_total_system", ctypes.c_uint64),
                ("pti_threads_user", ctypes.c_uint64),
                ("pti_threads_system", ctypes.c_uint64),
                ("pti_policy", ctypes.c_int32),
                ("pti_faults", ctypes.c_int32),
                ("pti_pageins", ctypes.c_int32),
                ("pti_cow_faults", ctypes.c_int32),
                ("pti_messages_sent", ctypes.c_int32),
                ("pti_messages_received", ctypes.c_int32),
                ("pti_syscalls_mach", ctypes.c_int32),
                ("pti_syscalls_unix", ctypes.c_int32),
                ("pti_csw", ctypes.c_int32),
                ("pti_threadnum", ctypes.c_int32),
                ("pti_numrunning", ctypes.c_int32),
                ("pti_priority", ctypes.c_int32),
            )

        libc.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        libc.proc_pidinfo.restype = ctypes.c_int
        info = proc_taskinfo()
        n = libc.proc_pidinfo(int(pid), 4, 0, ctypes.byref(info), ctypes.sizeof(info))
        if n >= ctypes.sizeof(info):
            return int(info.pti_resident_size)
    except Exception:
        pass
    try:
        import subprocess

        out = subprocess.check_output(  # noqa: S603
            ["ps", "-o", "rss=", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return int(out.strip().split()[0]) * 1024
    except Exception:
        return 0


def _windows_child_rss(pid: int) -> int:
    try:
        import ctypes
        from ctypes import wintypes
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

    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_VM_READ = 0x0010
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            int(pid),
        )
        if not handle:
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return 0
        try:
            counters = PROCESS_MEMORY_COUNTERS_EX()
            counters.cb = ctypes.sizeof(counters)
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize or counters.PrivateUsage)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return 0
    return 0


def _kill(proc: Any) -> None:
    try:
        if os.name != "nt" and proc.pid:
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def _cleanup(sandbox: Path) -> None:
    import shutil

    try:
        shutil.rmtree(sandbox, ignore_errors=True)
    except OSError:
        pass
