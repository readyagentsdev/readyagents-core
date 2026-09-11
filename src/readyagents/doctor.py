"""Read-only environment diagnostic. No network, no LLM, no run record."""

from __future__ import annotations

import platform
import socket
import sqlite3
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

from readyagents import __version__
from readyagents.config import get_settings
from readyagents.paths import filesystem_case_sensitive
from readyagents.permissions import permissions_enforceable


def run_doctor() -> dict[str, Any]:
    """Return the stable ``readyagents doctor --json`` envelope."""
    settings = get_settings()
    home = settings.home_path()
    workspace = settings.workspace_path()
    findings: list[dict[str, str]] = []

    home_writable = _writable_dir(home)
    if not home_writable:
        findings.append(
            {
                "id": "home-unwritable",
                "message": f"READYAGENTS_HOME is not writable: {home}",
                "remedy": "Set READYAGENTS_HOME to a directory you can write, then retry.",
            }
        )

    perm_ok = False
    try:
        home.mkdir(parents=True, exist_ok=True)
        perm_ok = permissions_enforceable(home)
    except OSError:
        perm_ok = False
    loopback = _loopback_bindable()
    if not loopback:
        findings.append(
            {
                "id": "loopback",
                "message": "Could not bind 127.0.0.1.",
                "remedy": "Check that another process is not blocking loopback and that "
                "the user may bind local ports.",
            }
        )

    extras = {
        "openai": _can_import("openai"),
        "anthropic": _can_import("anthropic"),
        "mcp": _can_import("mcp"),
        "tokenizer": _can_import("tiktoken"),
        "jwt": _can_import("jwt"),
        "sign": _can_import("cryptography"),
    }
    sqlite_wal = _sqlite_wal()
    if not sqlite_wal:
        findings.append(
            {
                "id": "sqlite-wal",
                "message": "SQLite WAL mode is unavailable.",
                "remedy": "Use the JSON run-store (READYAGENTS_RUN_STORE=json) or upgrade SQLite.",
            }
        )

    install = _install_location()
    payload = {
        "ok": not findings,
        "command": "doctor",
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "readyagents": {"version": __version__, "install": install},
        "extras": extras,
        "home": {
            "path": str(home),
            "writable": home_writable,
            "permissions_enforceable": perm_ok,
        },
        "filesystem": {
            "case_sensitive": filesystem_case_sensitive(workspace),
            "workspace": str(workspace),
        },
        "loopback": {"bindable": loopback},
        "run_store": {
            "backend": settings.run_store,
            "sqlite_wal": sqlite_wal,
            "database": str(settings.run_db_path()),
        },
        "findings": findings,
        "sovereign": {
            "would_succeed": bool(loopback),
            "loopback_bindable": loopback,
            "os_sandbox": False,
        },
        "local_models": {
            "ollama": {
                "present": _loopback_port_open(11434),
                "endpoint": "loopback:11434",
            }
        },
        "code_sandbox": _code_sandbox_report(),
    }
    if not loopback:
        findings.append(
            {
                "id": "sovereign",
                "message": "Sovereign mode would fail: loopback is not bindable.",
                "remedy": "Restore 127.0.0.1 bind permission, then retry readyagents doctor.",
            }
        )
        payload["ok"] = False
        payload["findings"] = findings
        payload["sovereign"]["would_succeed"] = False
    return payload


def format_doctor(report: dict[str, Any]) -> str:
    lines = [
        f"ReadyAgents {report['readyagents']['version']}  "
        f"{report['platform']['system']} {report['platform']['release']} "
        f"{report['platform']['machine']}",
        f"Python {report['python']['version']} ({report['python']['implementation']})",
        f"Install {report['readyagents']['install']}",
        "Extras: "
        + ", ".join(
            f"{name}={'yes' if ok else 'no'}" for name, ok in sorted(report["extras"].items())
        ),
        f"HOME {report['home']['path']} writable={report['home']['writable']} "
        f"permissions_enforceable={report['home']['permissions_enforceable']}",
        f"Workspace {report['filesystem']['workspace']} "
        f"case_sensitive={report['filesystem']['case_sensitive']}",
        f"Loopback bindable={report['loopback']['bindable']}",
        (
            f"Run-store {report['run_store']['backend']} "
            f"sqlite_wal={report['run_store']['sqlite_wal']}"
        ),
        (
            "Sovereign would_succeed="
            f"{report.get('sovereign', {}).get('would_succeed')} "
            "(in-process guard, not an OS sandbox)"
        ),
        (
            "Local models ollama_present="
            f"{report.get('local_models', {}).get('ollama', {}).get('present')}"
        ),
        _format_code_sandbox(report.get("code_sandbox") or {}),
    ]
    if report["findings"]:
        lines.append("Findings:")
        for item in report["findings"]:
            lines.append(f"- {item['message']}")
            lines.append(f"  remedy: {item['remedy']}")
    else:
        lines.append("No problems found.")
    return "\n".join(lines)


def _code_sandbox_report() -> dict[str, Any]:
    unix = sys.platform != "win32"
    resource_ok = False
    if unix:
        try:
            import resource  # noqa: F401

            resource_ok = True
        except ImportError:
            resource_ok = False
    os_rlimits: list[str] = []
    if resource_ok:
        os_rlimits.append("cpu_seconds")
        if sys.platform.startswith("linux"):
            os_rlimits.append("memory_mb")
        os_rlimits.append("file_size_bytes")
    if not resource_ok:
        platform_note = (
            "Windows has no resource module; OS rlimits for CPU/address-space/nproc "
            "are absent. The child still enforces cpu_seconds via process_time, "
            "memory_mb via RSS, file_size_bytes via capped open, and nproc via spawn "
            "wrappers. wall_seconds and output_bytes are enforced by the parent. "
            "subprocess isolation is accident-grade, not hostile-code-proof."
        )
    elif sys.platform == "darwin":
        platform_note = (
            "macOS does not let a process lower RLIMIT_AS; memory_mb is a child RSS "
            "watchdog, not an OS address-space cap. RLIMIT_CPU and RLIMIT_FSIZE apply."
        )
    else:
        platform_note = None
    return {
        "subprocess": True,
        "container_pack": False,
        "resource_module": resource_ok,
        "os_rlimits": os_rlimits,
        "always": [
            "wall_seconds",
            "output_bytes",
            "import_allowlist",
            "filesystem_grants",
            "cpu_process_time",
            "memory_rss",
            "file_size_capped_open",
            "nproc_spawn_wrappers",
        ],
        "windows_note": platform_note if not resource_ok else None,
        "platform_note": platform_note,
        "honesty": "subprocess defends against accident and careless code, not hostile code.",
    }


def _format_code_sandbox(block: dict[str, Any]) -> str:
    rlimits = ",".join(block.get("os_rlimits") or []) or "none"
    note = block.get("platform_note") or block.get("windows_note") or block.get("honesty") or ""
    return (
        f"Code sandbox subprocess=yes resource_module={block.get('resource_module')} "
        f"os_rlimits={rlimits}. {note}"
    ).strip()


def _can_import(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def _writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".ra_doctor_write"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _loopback_bindable() -> bool:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0))
        finally:
            sock.close()
        return True
    except OSError:
        return False


def _loopback_port_open(port: int) -> bool:
    """Loopback probe only. Never prints a private hostname or secret."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.05)
    try:
        sock.connect(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _sqlite_wal() -> bool:
    import tempfile

    handle = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
    handle.close()
    path = handle.name
    try:
        conn = sqlite3.connect(path)
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()
        conn.close()
        return bool(mode) and str(mode[0]).lower() == "wal"
    except sqlite3.Error:
        return False
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(path + suffix).unlink(missing_ok=True)
            except OSError:
                pass


def _install_location() -> str:
    try:
        dist = metadata.distribution("readyagentsdev")
        return str(dist.locate_file(""))
    except Exception:  # noqa: BLE001
        return str(Path(sys.modules["readyagents"].__file__ or "").resolve().parent)
