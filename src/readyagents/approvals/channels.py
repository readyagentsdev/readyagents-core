"""Opt-in approval notification channels. Failure never changes run state."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from readyagents.logging import get_logger
from readyagents.policy import Redactor

log = get_logger("approvals.channels")
_PROMPT_LIMIT = 200
_PAYLOAD_LIMIT = 4096
_COMMANDED = {"file", "command", "webhook"}


def notify_channels(
    channels: list[Any],
    *,
    run_id: str,
    node_id: str,
    prompt: str,
    eligible: list[str],
    expires_at: str | None,
    workspace: Path | None = None,
    redactor: Any = None,
) -> None:
    active = redactor if redactor is not None else Redactor()
    payload = _payload(
        run_id=run_id,
        node_id=node_id,
        prompt=prompt,
        eligible=eligible,
        expires_at=expires_at,
        redactor=active,
    )
    seen_fail: set[str] = set()
    for spec in channels or []:
        raw_kind = getattr(spec, "kind", None)
        if raw_kind is None and isinstance(spec, dict):
            raw_kind = spec.get("kind")
        kind = str(raw_kind or "")
        kind = kind.strip().lower()
        if kind not in _COMMANDED:
            continue
        try:
            if kind == "file":
                _write_file(spec, payload, workspace=workspace)
            elif kind == "command":
                _run_command(spec, payload)
            elif kind == "webhook":
                _post_webhook(spec, payload)
        except Exception as extra:  # noqa: BLE001
            key = f"{kind}:{run_id}:{node_id}"
            if key not in seen_fail:
                seen_fail.add(key)
                log.warning(
                    "approval channel %s failed: %s",
                    kind,
                    extra,
                    extra={"run_id": run_id, "node_id": node_id, "event": "approval_channel_error"},
                )


def _payload(
    *,
    run_id: str,
    node_id: str,
    prompt: str,
    eligible: list[str],
    expires_at: str | None,
    redactor: Any,
) -> dict[str, Any]:
    question = str(prompt or "")
    if len(question) > _PROMPT_LIMIT:
        question = question[: _PROMPT_LIMIT - 1] + "…"
    if redactor is not None and hasattr(redactor, "redact"):
        question = str(redactor.redact(question))
    body = {
        "event": "approval_required",
        "run_id": run_id,
        "node_id": node_id,
        "question": question,
        "eligible_roles": list(eligible),
        "deadline": expires_at,
    }
    encoded = json.dumps(body, ensure_ascii=False, default=str)
    if len(encoded) > _PAYLOAD_LIMIT:
        body["question"] = body["question"][:80] + "…"
    return body


def _attr(spec: Any, name: str) -> Any:
    if isinstance(spec, dict):
        return spec.get(name)
    return getattr(spec, name, None)


def _write_file(spec: Any, payload: dict[str, Any], *, workspace: Path | None) -> None:
    raw = _attr(spec, "path")
    if not raw:
        raise ValueError("file channel requires path")
    dest = Path(str(raw))
    if not dest.is_absolute():
        dest = (workspace or Path.cwd()) / dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
    with dest.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _run_command(spec: Any, payload: dict[str, Any]) -> None:
    command = _attr(spec, "command")
    if not command:
        raise ValueError("command channel requires command")
    args = [str(item) for item in list(command)]
    blob = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    subprocess.run(  # noqa: S603
        args,
        input=blob,
        timeout=5,
        check=False,
        capture_output=True,
    )


def _post_webhook(spec: Any, payload: dict[str, Any]) -> None:
    url = str(_attr(spec, "url") or "").strip()
    if not url:
        raise ValueError("webhook channel requires url")
    from readyagents.notify import post_json

    post_json(url, payload)
