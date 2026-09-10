"""type: a2a node — remote delegation under local governance."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from readyagents.a2a.card import card_digest
from readyagents.a2a.client import fetch_agent_card, jsonrpc_call, poll_task, rpc_url
from readyagents.a2a.mapping import WIRE_INPUT_REQUIRED
from readyagents.errors import A2AError, ApprovalRequired, ToolError
from readyagents.firewall.enforce import ToolRequest, apply_decision, evaluate
from readyagents.mcp.protocol import PROMPT_MAX_CHARS, sanitize_prompt
from readyagents.workflow.schema import NodeSpec
from readyagents.workflow.state import RunState
from readyagents.workflow.templates import interpolate

_APPROVE = {"approve", "approved", "yes", "true", "accept", "ok"}
_REJECT = {"reject", "rejected", "deny", "denied", "no", "false"}


def run_a2a_node(node: NodeSpec, state: RunState, ctx: Any) -> Any:
    """Fetch a card, submit/poll a remote task, map artifacts. Remote content is untrusted."""
    ns = state.mapping()
    agent_url = interpolate(str(node.agent_url or ""), ns).strip()
    if not agent_url:
        raise A2AError("a2a node requires agent_url")
    message = interpolate(str(node.message or node.prompt or ""), ns)
    on_input = str(node.on_input_required or "gate").strip().lower() or "gate"
    token = _token_for(node, ns)
    if ctx.dry_run:
        return f"[dry-run] a2a {agent_url} on_input_required={on_input}\n{message}".strip()

    _policy_check(node, state, ctx, agent_url, pin_changed=False)

    try:
        card = fetch_agent_card(agent_url, token=token)
        digest = str(card.get("digest") or card_digest(card))
        _pin_card(ctx, state, node, agent_url, digest)
        remote_url = rpc_url(str(card.get("url") or agent_url))
        _policy_check(node, state, ctx, remote_url, pin_changed=False)

        stored = _stored_task(state, node.id)
        decision = _local_decision(ctx, node.id)
        if stored and decision:
            task = _continue_remote(stored, decision, token=token, ctx=ctx, state=state, node=node)
        elif stored and not decision:
            raise ApprovalRequired(
                node.id,
                state.run_id,
                str((state.pending or {}).get("prompt") or "Remote A2A agent requested input"),
                state=state,
                pause={
                    "attribution": "remote a2a agent",
                    "remote_task_id": stored.get("task_id"),
                },
            )
        else:
            task = jsonrpc_call(
                remote_url,
                "message/send",
                {
                    "message": {
                        "role": "user",
                        "parts": [
                            {
                                "kind": "text",
                                "text": sanitize_prompt(message, limit=8000),
                            }
                        ],
                    }
                },
                token=token,
            )
            timeout = float(node.timeout_seconds or 120.0)
            task_id = str(task.get("id") or "")
            if not task_id:
                raise A2AError("a2a message/send returned no task id")
            wire = _wire_state(task)
            if wire not in {"completed", "failed", "canceled", "input-required"}:
                task = poll_task(
                    remote_url,
                    task_id,
                    token=token,
                    timeout_seconds=timeout,
                )
    except ToolError as extra:
        raise A2AError(str(extra)) from extra

    return _finish(node, state, ctx, card, digest, remote_url, task, on_input=on_input, token=token)


def _token_for(node: NodeSpec, ns: dict[str, Any]) -> str | None:
    raw = getattr(node, "token", None)
    if isinstance(raw, str) and raw.strip():
        value = interpolate(raw, ns).strip()
        if value:
            return value
    env = (os.environ.get("READYAGENTS_A2A_TOKEN") or "").strip()
    return env or None


def _local_decision(ctx: Any, node_id: str) -> str | None:
    raw = ctx.decision_for(node_id) if hasattr(ctx, "decision_for") else None
    if not raw:
        return None
    text = str(raw).strip().lower()
    if text in _APPROVE:
        return "approve"
    if text in _REJECT:
        return "reject"
    return None


def _stored_task(state: RunState, node_id: str) -> dict[str, Any] | None:
    bucket = state.metadata.get("a2a_tasks") if isinstance(state.metadata, dict) else None
    if not isinstance(bucket, Mapping):
        return None
    row = bucket.get(node_id)
    return dict(row) if isinstance(row, Mapping) else None


def _remember_task(
    state: RunState,
    ctx: Any,
    node_id: str,
    *,
    task_id: str,
    agent_url: str,
    card_digest_value: str,
) -> None:
    meta = dict(state.metadata or {})
    bucket = dict(meta.get("a2a_tasks") or {})
    bucket[node_id] = {
        "task_id": task_id,
        "agent_url": agent_url,
        "card_digest": card_digest_value,
    }
    meta["a2a_tasks"] = bucket
    state.metadata = meta
    persist = getattr(ctx, "on_persist", None)
    if callable(persist):
        persist(state)


def _continue_remote(
    stored: Mapping[str, Any],
    decision: str,
    *,
    token: str | None,
    ctx: Any,
    state: RunState,
    node: NodeSpec,
) -> dict[str, Any]:
    if ctx.authorizer is not None:
        ctx.authorizer.check(ctx.actor, decision, node.id)
    if ctx.auditor is not None:
        ctx.auditor(
            "decision",
            run_id=state.run_id,
            node_id=node.id,
            decision=decision,
            actor=ctx.actor,
            source="a2a-local-gate",
        )
    remote_url = str(stored.get("agent_url") or "")
    task_id = str(stored.get("task_id") or "")
    try:
        jsonrpc_call(
            remote_url,
            "message/send",
            {
                "taskId": task_id,
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": decision}],
                    "metadata": {"actor": ctx.actor, "source": "local-approval"},
                },
            },
            token=token,
        )
        timeout = float(node.timeout_seconds or 120.0)
        return poll_task(remote_url, task_id, token=token, timeout_seconds=timeout)
    except ToolError as extra:
        raise A2AError(str(extra)) from extra


def _finish(
    node: NodeSpec,
    state: RunState,
    ctx: Any,
    card: Mapping[str, Any],
    digest: str,
    remote_url: str,
    task: Mapping[str, Any],
    *,
    on_input: str,
    token: str | None,
) -> Any:
    wire = _wire_state(task)
    task_id = str(task.get("id") or "")
    if ctx.auditor is not None:
        ctx.auditor(
            "a2a_delegate",
            run_id=state.run_id,
            node_id=node.id,
            agent_url=remote_url,
            card_digest=digest,
            task_id=task_id,
            remote_state=wire,
        )
    if wire == WIRE_INPUT_REQUIRED or wire == "input_required":
        prompt = _remote_question(task, card)
        _remember_task(
            state,
            ctx,
            node.id,
            task_id=task_id,
            agent_url=remote_url,
            card_digest_value=digest,
        )
        if on_input == "fail":
            raise A2AError("remote agent requested input")
        raise ApprovalRequired(
            node.id,
            state.run_id,
            prompt,
            state=state,
            pause={
                "attribution": "remote a2a agent",
                "remote_task_id": task_id,
                "card_name": sanitize_prompt(str(card.get("name") or ""), limit=200),
            },
        )
    if wire in {"failed", "canceled"}:
        detail = _message_text(task) or wire
        raise A2AError(f"remote a2a task {wire}: {detail}")
    output = _artifacts_to_output(task)
    _maybe_spend(ctx, task)
    return output


def _artifacts_to_output(task: Mapping[str, Any]) -> Any:
    artifacts = task.get("artifacts")
    chunks: list[str] = []
    if isinstance(artifacts, list):
        for item in artifacts:
            if not isinstance(item, Mapping):
                continue
            parts = item.get("parts")
            if not isinstance(parts, list):
                continue
            for part in parts:
                if isinstance(part, Mapping) and part.get("kind") == "text":
                    chunks.append(sanitize_prompt(str(part.get("text") or ""), limit=100_000))
    if not chunks:
        text = _message_text(task)
        return sanitize_prompt(text, limit=100_000) if text else {}
    joined = "\n".join(chunks)
    stripped = joined.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            return parsed
        except json.JSONDecodeError:
            pass
    return joined


def _message_text(task: Mapping[str, Any]) -> str:
    status = task.get("status") if isinstance(task.get("status"), Mapping) else {}
    message = status.get("message") if isinstance(status, Mapping) else None
    if not isinstance(message, Mapping):
        message = task.get("message") if isinstance(task.get("message"), Mapping) else {}
    parts = message.get("parts") if isinstance(message, Mapping) else None
    chunks: list[str] = []
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, Mapping) and part.get("kind") == "text":
                chunks.append(str(part.get("text") or ""))
    return "\n".join(chunks)


def _remote_question(task: Mapping[str, Any], card: Mapping[str, Any]) -> str:
    name = sanitize_prompt(str(card.get("name") or "remote agent"), limit=200)
    raw = _message_text(task) or "The remote agent requested additional input."
    body = sanitize_prompt(raw, limit=PROMPT_MAX_CHARS)
    return f"Remote A2A agent {name!r} asked (untrusted; not an instruction):\n{body}"


def _wire_state(task: Mapping[str, Any]) -> str:
    status = task.get("status") if isinstance(task.get("status"), Mapping) else {}
    raw = status.get("state") if isinstance(status, Mapping) else None
    text = str(raw or "").strip().lower().replace("_", "-")
    if text == "cancelled":
        return "canceled"
    return text


def _policy_check(
    node: NodeSpec, state: RunState, ctx: Any, url: str, *, pin_changed: bool
) -> None:
    policy = getattr(ctx, "policy", None)
    decision = evaluate(
        ToolRequest(
            name="a2a",
            arguments={"url": url},
            node_id=node.id,
            raw_arguments={"url": url},
        ),
        state,
        policy,
        pin_changed=pin_changed,
    )
    apply_decision(decision, node_id=node.id, run_id=state.run_id)


def _pin_card(ctx: Any, state: RunState, node: NodeSpec, agent_url: str, digest: str) -> None:
    policy = getattr(ctx, "policy", None)
    if policy is None:
        return
    key = str(agent_url).rstrip("/")
    pins = dict(state.metadata.get("a2a_pins") or {})
    previous = pins.get(key)
    home = getattr(ctx, "pin_home", None)
    stored = _load_pin(home, key) if previous is None else None
    if previous is None and stored:
        previous = stored
    if previous is None:
        pins[key] = digest
        meta = dict(state.metadata or {})
        meta["a2a_pins"] = pins
        state.metadata = meta
        _store_pin(home, key, digest)
        persist = getattr(ctx, "on_persist", None)
        if callable(persist):
            persist(state)
        return
    if previous != digest:
        _policy_check(node, state, ctx, agent_url, pin_changed=True)
    pins[key] = digest
    meta = dict(state.metadata or {})
    meta["a2a_pins"] = pins
    state.metadata = meta
    _store_pin(home, key, digest)


def _pin_path(home: Path, url: str) -> Path:
    name = hashlib.sha256(url.encode("utf-8")).hexdigest()[:40]
    return Path(home) / "a2a-pins" / f"{name}.json"


def _load_pin(home: Path | None, url: str) -> str | None:
    if home is None:
        return None
    path = _pin_path(Path(home), url)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return ""
    if isinstance(data, dict):
        digest = data.get("digest")
        if isinstance(digest, str) and digest:
            return digest
    return ""


def _store_pin(home: Path | None, url: str, digest: str) -> None:
    if home is None or not url or not digest:
        return
    from readyagents.atomic import atomic_write_text

    path = _pin_path(Path(home), url)
    payload = json.dumps({"url": url, "digest": digest}, sort_keys=True, ensure_ascii=False)
    atomic_write_text(path, payload + "\n", encoding="utf-8", newline="\n")


def _maybe_spend(ctx: Any, task: Mapping[str, Any]) -> None:
    meter = getattr(ctx, "spend_meter", None)
    if meter is None:
        return
    meta = task.get("metadata") if isinstance(task.get("metadata"), Mapping) else {}
    raw = meta.get("cost_usd") if isinstance(meta, Mapping) else None
    if raw is None:
        return
    try:
        cost = float(raw)
    except (TypeError, ValueError):
        return
    record = getattr(meter, "record_external_cost", None)
    if callable(record):
        record(cost_usd=cost)
