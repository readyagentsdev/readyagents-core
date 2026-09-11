"""Run a declared type: team node. Engine-enforced stop; no quality claim."""

from __future__ import annotations

import json
import time
from typing import Any

from readyagents.errors import (
    ApprovalRequired,
    TeamRoundsExceeded,
    TeamScratchpadDenied,
    TeamSpendExceeded,
    TeamUnknownMember,
    TeamWallExceeded,
)
from readyagents.firewall.taint import untrusted
from readyagents.team.spec import TeamMemberSpec, TeamSpec, parse_team
from readyagents.workflow.schema import NodeSpec
from readyagents.workflow.state import utc_now
from readyagents.workflow.templates import interpolate

_NOW = time.monotonic


def run_team_node(node: NodeSpec, state: Any, ctx: Any) -> Any:
    spec = parse_team(node)
    bucket = _bucket(state, node.id, spec)
    started = bucket.get("started_mono")
    if started is None:
        started = _NOW()
        bucket["started_mono"] = started
    else:
        started = float(started)
    state.metadata["scratchpad"] = bucket["scratchpad"]

    pending_member = bucket.get("pending_member")
    if pending_member:
        member = spec.member_map()[str(pending_member)]
        output = _run_member(node, member, spec, state, ctx, bucket)
        _after_member(node, spec, member, output, state, ctx, bucket)
        bucket["pending_member"] = None
        bucket["round"] = int(bucket.get("round") or 0) + 1
        _checkpoint(state, node.id, bucket)

    while True:
        _check_terminate(node.id, spec, bucket, ctx)
        if _goal_met(spec, bucket, state):
            bucket["stop"] = "goal"
            _checkpoint(state, node.id, bucket)
            return _team_output(bucket)
        member_id = _next_member(node, spec, state, ctx, bucket)
        if member_id is None:
            bucket["stop"] = "done"
            _checkpoint(state, node.id, bucket)
            return _team_output(bucket)
        member = spec.member_map()[member_id]
        try:
            output = _run_member(node, member, spec, state, ctx, bucket)
        except ApprovalRequired:
            bucket["pending_member"] = member.id
            _checkpoint(state, node.id, bucket)
            raise
        _after_member(node, spec, member, output, state, ctx, bucket)
        bucket["round"] = int(bucket.get("round") or 0) + 1
        _checkpoint(state, node.id, bucket)
        if spec.strategy == "pipeline" and bucket.get("pipeline_index", 0) >= len(spec.members):
            bucket["stop"] = "done"
            _checkpoint(state, node.id, bucket)
            return _team_output(bucket)


def _bucket(state: Any, team_id: str, spec: TeamSpec) -> dict[str, Any]:
    teams = state.metadata.setdefault("teams", {})
    if team_id in teams and isinstance(teams[team_id], dict):
        pad = teams[team_id].setdefault("scratchpad", {k: [] for k in spec.scratchpad.keys})
        for key in spec.scratchpad.keys:
            pad.setdefault(key, [])
        teams[team_id].setdefault("handoffs", [])
        teams[team_id].setdefault("usage", {})
        teams[team_id].setdefault("taint", {})
        teams[team_id].setdefault("round", 0)
        return teams[team_id]
    pad = {key: [] for key in spec.scratchpad.keys}
    bucket = {
        "scratchpad": pad,
        "handoffs": [],
        "usage": {},
        "taint": {},
        "round": 0,
        "plan": [],
        "plan_index": 0,
        "pipeline_index": 0,
        "stop": None,
        "pending_member": None,
        "started_mono": _NOW(),
    }
    teams[team_id] = bucket
    return bucket


def _checkpoint(state: Any, team_id: str, bucket: dict[str, Any]) -> None:
    state.metadata.setdefault("teams", {})[team_id] = bucket
    state.metadata["scratchpad"] = bucket.get("scratchpad")


def _check_terminate(node_id: str, spec: TeamSpec, bucket: dict[str, Any], ctx: Any) -> None:
    used_rounds = int(bucket.get("round") or 0)
    limit = int(spec.terminate.max_rounds)
    if used_rounds >= limit:
        raise TeamRoundsExceeded(node_id, used_rounds, limit)
    wall = spec.terminate.max_wall_seconds
    if wall is not None:
        elapsed = _NOW() - float(bucket.get("started_mono") or 0)
        if elapsed >= float(wall):
            raise TeamWallExceeded(node_id, elapsed, float(wall))
    usd = spec.terminate.max_cost_usd
    if usd is not None:
        used = int(bucket.get("cost_micros") or 0)
        meter = getattr(ctx, "spend_meter", None)
        if meter is not None:
            used = max(used, int(getattr(meter, "cost_micros", 0) or 0))
        limit_micros = int(round(float(usd) * 1_000_000))
        if used >= limit_micros:
            raise TeamSpendExceeded(node_id, used, limit_micros)


def _goal_met(spec: TeamSpec, bucket: dict[str, Any], state: Any) -> bool:
    goal = spec.terminate.goal
    if not goal:
        return False
    ns = dict(state.mapping())
    ns["scratchpad"] = bucket.get("scratchpad") or {}
    try:
        text = interpolate(goal, ns).strip().lower()
    except Exception:
        return False
    if text in {"", "0", "false", "none", "[]", "{}"}:
        return False
    try:
        return float(text) > 0
    except ValueError:
        return text in {"true", "yes", "ok"}


def _next_member(
    node: NodeSpec, spec: TeamSpec, state: Any, ctx: Any, bucket: dict[str, Any]
) -> str | None:
    ids = spec.member_ids()
    if spec.strategy == "pipeline":
        idx = int(bucket.get("pipeline_index") or 0)
        if idx >= len(ids):
            return None
        bucket["pipeline_index"] = idx + 1
        _handoff(bucket, from_id="supervisor", to=ids[idx], reason="pipeline", payload={})
        return ids[idx]
    if spec.strategy == "plan_then_execute":
        plan = list(bucket.get("plan") or [])
        if not plan:
            choice = _supervisor_choice(node, spec, state, ctx, bucket)
            if choice.get("done"):
                return None
            plan = [str(x) for x in (choice.get("plan") or [])]
            nxt = choice.get("next")
            if nxt and not plan:
                plan = [str(nxt)]
            if not plan:
                return None
            for item in plan:
                if item not in spec.member_map():
                    raise TeamUnknownMember(node.id, item)
            bucket["plan"] = plan
            bucket["plan_index"] = 0
        idx = int(bucket.get("plan_index") or 0)
        if idx >= len(plan):
            return None
        bucket["plan_index"] = idx + 1
        target = plan[idx]
        _handoff(bucket, from_id="supervisor", to=target, reason="plan", payload={})
        return target
    choice = _supervisor_choice(node, spec, state, ctx, bucket)
    if choice.get("done"):
        return None
    target = str(choice.get("next") or "")
    if not target:
        return None
    if target not in spec.member_map():
        raise TeamUnknownMember(node.id, target)
    _handoff(
        bucket,
        from_id="supervisor",
        to=target,
        reason=str(choice.get("reason") or spec.strategy),
        payload=choice.get("payload") or {},
    )
    return target


def _supervisor_choice(
    node: NodeSpec, spec: TeamSpec, state: Any, ctx: Any, bucket: dict[str, Any]
) -> dict[str, Any]:
    from readyagents.workflow.nodes import _run_agent

    ids = spec.member_ids()
    pad = json.dumps(bucket.get("scratchpad") or {}, ensure_ascii=False, default=str)
    prompt = (
        f"{spec.supervisor.prompt}\n"
        f"Declared members: {', '.join(ids)}.\n"
        f"Scratchpad: {pad}\n"
        'Reply with JSON only: {"next": "<member_id>", "reason": "<text>", '
        '"done": false, "plan": ["id", ...], "payload": {}}'
    )
    sup = NodeSpec(
        id=f"{node.id}__supervisor",
        type="agent",
        prompt=prompt,
        system=spec.supervisor.system,
        model=spec.supervisor.model or node.model,
    )
    before = dict(state.usage)
    raw = _run_agent(sup, state, ctx)
    _note_usage(bucket, "supervisor", state, ctx, before=before)
    data = _parse_json(raw)
    if not isinstance(data, dict):
        data = {"next": str(raw).strip(), "reason": "plain", "done": False}
    return data


def _run_member(
    team: NodeSpec,
    member: TeamMemberSpec,
    spec: TeamSpec,
    state: Any,
    ctx: Any,
    bucket: dict[str, Any],
) -> Any:
    from readyagents.workflow.nodes import execute_node

    pad = bucket.get("scratchpad") or {}
    readable = _readable(member, spec)
    view = {k: pad.get(k) for k in readable if k in pad}
    prompt = member.prompt or f"You are {member.role or member.id}. Work the task."
    prompt = f"{prompt}\nScratchpad (granted): {json.dumps(view, ensure_ascii=False, default=str)}"
    kind = member.type
    kwargs: dict[str, Any] = {
        "id": member.id,
        "type": kind,
        "prompt": prompt,
        "system": member.system_prompt(),
        "model": member.model or spec.supervisor.model,
        "tools": list(member.tools or []),
        "approver_roles": list(member.approver_roles or []),
        "template": member.template,
        "parse_json": member.parse_json,
    }
    if kind == "approval":
        kwargs["then"] = "__continue__"
        kwargs["next"] = "__continue__"
    spec_node = NodeSpec.model_validate(kwargs)
    if member.max_cost_usd is not None:
        used = int((bucket.get("usage") or {}).get(member.id, {}).get("cost_micros") or 0)
        limit = int(round(float(member.max_cost_usd) * 1_000_000))
        if used >= limit:
            raise TeamSpendExceeded(team.id, used, limit)
    before = dict(state.usage)
    output = execute_node(spec_node, state, ctx)
    _note_usage(bucket, member.id, state, ctx, before=before)
    return output


def _after_member(
    team: NodeSpec,
    spec: TeamSpec,
    member: TeamMemberSpec,
    output: Any,
    state: Any,
    ctx: Any,
    bucket: dict[str, Any],
) -> None:
    writes = _extract_writes(output)
    pad = bucket.setdefault("scratchpad", {})
    taint = bucket.setdefault("taint", {})
    grant_write = set((member.scratchpad.write if member.scratchpad else []) or [])
    declared = set(spec.scratchpad.keys)
    for key, value in writes.items():
        if declared and key not in declared:
            raise TeamScratchpadDenied(team.id, member.id, key, "write")
        if grant_write and key not in grant_write:
            raise TeamScratchpadDenied(team.id, member.id, key, "write")
        if not grant_write and declared:
            raise TeamScratchpadDenied(team.id, member.id, key, "write")
        pad[key] = value
        taint[key] = member.id
        state.provenance[f"scratchpad.{key}"] = untrusted(
            source="team_scratchpad", node_id=member.id, detail=member.id
        ).as_dict()
    grant_read = set((member.scratchpad.read if member.scratchpad else []) or [])
    if grant_read or declared:
        data = _parse_json(output)
        if isinstance(data, dict):
            requested = data.get("scratchpad_read")
            if isinstance(requested, list):
                for key in requested:
                    if key not in grant_read:
                        raise TeamScratchpadDenied(team.id, member.id, str(key), "read")
    _handoff(
        bucket,
        from_id=member.id,
        to="supervisor",
        reason="member_done",
        payload=_clip(output),
    )
    bucket["last_output"] = _clip(output)


def _extract_writes(output: Any) -> dict[str, Any]:
    data = _parse_json(output)
    if not isinstance(data, dict):
        return {}
    blob = data.get("scratchpad")
    if isinstance(blob, dict):
        return dict(blob)
    blob = data.get("scratchpad_write")
    if isinstance(blob, dict):
        return dict(blob)
    return {}


def _readable(member: TeamMemberSpec, spec: TeamSpec) -> list[str]:
    grant = member.scratchpad
    if grant is None:
        return list(spec.scratchpad.keys)
    return list(grant.read)


def _handoff(
    bucket: dict[str, Any],
    *,
    from_id: str,
    to: str,
    reason: str,
    payload: Any,
) -> None:
    bucket.setdefault("handoffs", []).append(
        {
            "from": from_id,
            "to": to,
            "reason": reason,
            "payload": _clip(payload),
            "ts": utc_now(),
        }
    )


def _note_usage(
    bucket: dict[str, Any],
    who: str,
    state: Any,
    ctx: Any,
    *,
    before: dict[str, Any] | None = None,
) -> None:
    now = dict(state.usage)
    prev = before or {}
    delta_prompt = int(now.get("prompt_tokens") or 0) - int(prev.get("prompt_tokens") or 0)
    delta_completion = int(now.get("completion_tokens") or 0) - int(
        prev.get("completion_tokens") or 0
    )
    delta_total = int(now.get("total_tokens") or 0) - int(prev.get("total_tokens") or 0)
    delta_cost = int(now.get("cost_micros") or 0) - int(prev.get("cost_micros") or 0)
    tools = len(getattr(ctx, "last_tool_rounds", None) or [])
    row = bucket.setdefault("usage", {}).setdefault(
        who,
        {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_micros": 0,
            "tool_calls": 0,
        },
    )
    row["prompt_tokens"] += max(0, delta_prompt)
    row["completion_tokens"] += max(0, delta_completion)
    row["total_tokens"] += max(0, delta_total)
    row["cost_micros"] += max(0, delta_cost)
    row["tool_calls"] += tools
    bucket["cost_micros"] = int(bucket.get("cost_micros") or 0) + max(0, delta_cost)


def _team_output(bucket: dict[str, Any]) -> dict[str, Any]:
    return {
        "scratchpad": dict(bucket.get("scratchpad") or {}),
        "handoffs": list(bucket.get("handoffs") or []),
        "rounds": int(bucket.get("round") or 0),
        "stop": bucket.get("stop") or "done",
        "usage": dict(bucket.get("usage") or {}),
        "taint": dict(bucket.get("taint") or {}),
        "last": bucket.get("last_output"),
    }


def _parse_json(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None


def _clip(value: Any, limit: int = 4096) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    return value
