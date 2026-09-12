from __future__ import annotations

from pathlib import Path

from readyagents import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_version_is_not_legacy_020() -> None:
    assert __version__ != "0.2.0"
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{__version__}"' in text


def test_dockerfile_and_compose_exist() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "ENTRYPOINT" in dockerfile
    assert "readyagents" in dockerfile
    assert "readyagents" in compose
    assert "examples/calc_pipeline.yaml" in compose
    assert "smoke:" in makefile
    assert "scripts/smoke.py" in makefile
    smoke = (ROOT / "scripts" / "smoke.py").read_text(encoding="utf-8")
    assert "approval_gate.yaml" in smoke
    assert "fanout_gate.yaml" in smoke
    assert "include_demo.yaml" in smoke
    assert "--dry-run" in smoke
    assert "resume" in smoke
    assert "scripts/smoke.py" in ci


_BANNED_RUNTIME = (
    "HTTPServer",
    "BaseHTTPRequestHandler",
    "ThreadingHTTPServer",
    "uvicorn",
    "FastAPI",
    "Flask(",
    "APScheduler",
    "celery",
    "sqlalchemy",
    "redis.Redis",
    "kafka",
    "psycopg",
    "pymongo",
)


def test_m4_outbound_copies_exist() -> None:
    gate = (ROOT / "docs" / "outbound" / "gate-http-decide.md").read_text(encoding="utf-8")
    assert "decide" in gate.lower()
    assert "HTTP" in gate
    for banned in ("waitlist", "Polar", "Slack", "LinkedIn"):
        assert banned.lower() not in gate.lower(), banned
    assert (ROOT / "examples" / "packs" / "hitl_gate.py").is_file()


# Opt-in foreground Streamable HTTP may import uvicorn inside mcp/http.py only.
# That module is started by `readyagents mcp serve --transport streamable-http`
# and is not an always-on worker, scheduler, or hosted control plane.
# The approval UI is the same class of door: explicit `approvals serve`, stdlib
# HTTP, loopback only, stops when the foreground command stops.
# A2A serve is the same class: explicit `a2a serve`, stops when the process stops.
# Chat serve is the same class: explicit `serve chat`, loopback default, foreground.
_ALLOWED_RUNTIME = {
    ("src/readyagents/mcp/http.py", "uvicorn"),
    ("src/readyagents/a2a/server.py", "uvicorn"),
    ("src/readyagents/approvals/server.py", "HTTPServer"),
    ("src/readyagents/approvals/server.py", "BaseHTTPRequestHandler"),
    ("src/readyagents/approvals/server.py", "ThreadingHTTPServer"),
    ("src/readyagents/studio/server.py", "HTTPServer"),
    ("src/readyagents/studio/server.py", "BaseHTTPRequestHandler"),
    ("src/readyagents/studio/server.py", "ThreadingHTTPServer"),
    ("src/readyagents/sessions/chat.py", "HTTPServer"),
    ("src/readyagents/sessions/chat.py", "BaseHTTPRequestHandler"),
    ("src/readyagents/sessions/chat.py", "ThreadingHTTPServer"),
}


def test_core_src_has_no_always_on_or_control_plane() -> None:
    root = ROOT / "src" / "readyagents"
    hits: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(ROOT).as_posix()
        for token in _BANNED_RUNTIME:
            if token in text and (rel, token) not in _ALLOWED_RUNTIME:
                hits.append(f"{rel}: {token}")
    assert hits == []
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "readyagents" in compose
    assert "postgres" not in compose.lower()
    assert "redis" not in compose.lower()
    assert "worker" not in compose.lower()
