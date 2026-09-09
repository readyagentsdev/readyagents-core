from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path

from readyagents.approvals.view import (
    ASSET_SCRIPT_URL,
    ASSET_STYLE_URL,
    VIEW_FIELDS,
    approval_view,
    escape_html,
    is_approval_pause,
)
from readyagents.policy import Redactor
from readyagents.workflow.state import RunState

ASSETS = Path(__file__).resolve().parents[1] / "src" / "readyagents" / "approvals" / "assets"

EMAIL = "alice@example.com"
SK_KEY = "sk-abcdefghijk123"


def _paused(
    *,
    prompt: str = "Ship it?",
    actor: str | None = "local-user",
    pending_type: str = "approval",
    status: str = "paused",
    pending_node: str | None = "gate",
    inputs: dict | None = None,
) -> RunState:
    state = RunState.start(
        "approval-gate",
        inputs or {"payload": "secret-input", "email": EMAIL},
        metadata={"actor": actor} if actor is not None else {},
    )
    state.status = status
    state.pending_node = pending_node
    state.pending = {
        "type": pending_type,
        "prompt": prompt,
        "node_id": pending_node,
    }
    state.node_outputs = {"draft": "secret-output"}
    state.output_keys = {"summary": "secret-output"}
    state.record(
        "prep",
        "secret-output",
        node_type="transform",
        tool_rounds=[{"args": "secret-tool"}],
    )
    return state


def test_approval_view_omits_inputs_outputs_node_results() -> None:
    state = _paused(prompt=f"Publish for {EMAIL} with {SK_KEY}?")
    view = approval_view(state, revision=3)
    assert list(view) == list(VIEW_FIELDS)
    assert "inputs" not in view
    assert "outputs" not in view
    assert "node_outputs" not in view
    assert "node_results" not in view
    assert "tool_rounds" not in view
    assert "secrets" not in view
    assert "payload" not in json.dumps(view)
    assert "secret-input" not in json.dumps(view)
    assert "secret-output" not in json.dumps(view)
    assert "secret-tool" not in json.dumps(view)
    assert view["run_id"] == state.run_id
    assert view["workflow"] == "approval-gate"
    assert view["status"] == "paused"
    assert view["node_id"] == "gate"
    assert view["started_at"] == state.started_at
    assert view["revision"] == 3


def test_approval_view_redacts_email_and_sk_keys_by_default() -> None:
    state = _paused(
        prompt=f"Publish for {EMAIL} using {SK_KEY}?",
        actor=f"ops+{EMAIL}",
    )
    view = approval_view(state, revision=1)
    dumped = json.dumps(view)
    assert EMAIL not in dumped
    assert SK_KEY not in dumped
    assert "[redacted]" in view["prompt"]
    assert "[redacted]" in view["actor"]
    custom = Redactor(literals=["Ship it?"], replacement="[gone]")
    plain = _paused(prompt="Ship it?", actor="local-user")
    overridden = approval_view(plain, revision=2, redactor=custom)
    assert overridden["prompt"] == "[gone]"
    passed = approval_view(_paused(actor=None), revision=2, actor=EMAIL)
    assert EMAIL not in passed["actor"]
    assert "[redacted]" in passed["actor"]


def test_is_approval_pause_true_only_for_approval_pauses() -> None:
    assert is_approval_pause(_paused())
    paused_no_node = _paused(pending_node=None)
    assert is_approval_pause(paused_no_node)
    assert not is_approval_pause(_paused(pending_type="condition"))
    assert not is_approval_pause(_paused(status="running"))
    assert not is_approval_pause(_paused(status="cancelled"))
    assert not is_approval_pause(_paused(status="succeeded"))
    assert not is_approval_pause(_paused(status="failed"))
    running = RunState.start("approval-gate", {})
    running.status = "running"
    running.pending_node = "gate"
    running.pending = {"type": "approval", "prompt": "go?"}
    assert not is_approval_pause(running)
    empty = RunState.start("approval-gate", {})
    empty.status = "paused"
    assert not is_approval_pause(empty)


def test_escape_html_for_server_rendered_text() -> None:
    assert "&lt;script&gt;" in escape_html("<script>alert(1)</script>")
    assert "a@b.com" not in escape_html("")


def test_app_js_uses_text_content_not_inner_html() -> None:
    src = (ASSETS / "app.js").read_text(encoding="utf-8")
    assert "textContent" in src
    assert "innerHTML" not in src
    assert "insertAdjacentHTML" not in src
    assert "document.write" not in src
    assert "credentials" in src and "same-origin" in src
    assert "/approvals/api/runs" in src
    assert "application/json" in src
    assert "confirm(" in src


def test_index_html_has_no_cdn_or_http_script_src() -> None:
    src = (ASSETS / "index.html").read_text(encoding="utf-8")
    assert "ReadyAgents approvals" in src
    assert "<main" in src
    assert "Pending approvals" in src
    assert "local foreground UI" in src
    assert "not a hosted dashboard" in src
    assert 'href="/approvals/assets/style.css"' in src
    assert 'src="/approvals/assets/app.js"' in src
    assert "defer" in src
    assert "<noscript>" in src
    assert ASSET_STYLE_URL in src
    assert ASSET_SCRIPT_URL in src
    assert "cdn" not in src.lower()
    assert "fonts.googleapis" not in src.lower()
    assert re.search(r"<script\b", src, re.I)
    for match in re.finditer(r"<script\b([^>]*)>", src, re.I):
        attrs = match.group(1)
        src_attr = re.search(r"\bsrc\s*=\s*['\"]([^'\"]+)['\"]", attrs, re.I)
        assert src_attr is not None
        assert not src_attr.group(1).lower().startswith("http")
        assert "cdn" not in src_attr.group(1).lower()
    assert not re.search(r"\son\w+\s*=", src)
    css = (ASSETS / "style.css").read_text(encoding="utf-8")
    assert "system-ui" in css
    assert "url(" not in css.lower()
    assert "http://" not in css.lower()
    assert "https://" not in css.lower()


def test_packaged_assets_are_importlib_resources() -> None:
    root = resources.files("readyagents.approvals")
    assert (root / "assets" / "index.html").is_file()
    assert (root / "assets" / "app.js").is_file()
    assert (root / "assets" / "style.css").is_file()
