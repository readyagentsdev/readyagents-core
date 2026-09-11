"""Subprocess isolation for ``type: code``. No secrets, closed fds, confined cwd."""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
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
    cpu = float(bound["cpu_seconds"])
    timeout = min(wall, cpu)
    cpu_is_tighter = cpu <= wall
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
    try:
        stdout, stderr = proc.communicate(input=stdin_blob, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(proc)
        leftover = proc.communicate()
        stdout, stderr = leftover[0] or b"", leftover[1] or b""
        if cpu_is_tighter:
            raise CodeCpuLimitExceeded(node_id) from None
        raise CodeWallLimitExceeded(node_id) from None
    out_limit = int(bound["output_bytes"])
    if len(stdout) + len(stderr) > out_limit:
        raise CodeOutputLimitExceeded(node_id)
    code = int(proc.returncode or 0)
    text_out = stdout.decode("utf-8", "replace")
    text_err = stderr.decode("utf-8", "replace")
    _raise_exit(node_id, code, text_err)
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


def _raise_exit(node_id: str, code: int, stderr: str) -> None:
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
        if hasattr(signal, "SIGKILL") and sig == signal.SIGKILL:
            # Linux escalates RLIMIT_CPU from SIGXCPU to SIGKILL.
            raise CodeCpuLimitExceeded(node_id)
        if hasattr(signal, "SIGSEGV") and sig == signal.SIGSEGV:
            raise CodeMemoryLimitExceeded(node_id)
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
