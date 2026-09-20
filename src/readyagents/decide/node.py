"""type: decide — typed questions, confidence-gated routing."""

from __future__ import annotations

import json
from typing import Any

from readyagents.cost.tokens import heuristic_tokens
from readyagents.decide.base import questions_from_mapping
from readyagents.decide.registry import get_decider
from readyagents.decide.types import Decision
from readyagents.errors import CassetteMiss, DecideError
from readyagents.logging import get_logger
from readyagents.workflow.templates import interpolate, interpolate_value

log = get_logger("decide.node")


def run_decide_node(node: Any, state: Any, ctx: Any) -> Any:
    if getattr(ctx, "dry_run", False):
        return {"dry_run": True, "type": "decide"}

    questions = questions_from_mapping(dict(getattr(node, "questions", None) or {}))
    judged = _render_state(getattr(node, "state", None), state)
    model_hint = str(getattr(node, "decider", None) or "")
    min_confidence = getattr(node, "min_confidence", None)
    min_conf = float(min_confidence) if min_confidence is not None else None

    if getattr(ctx, "offline", False):
        cassette = getattr(ctx, "cassette", None)
        if cassette is None:
            raise DecideError(
                "Offline replay cannot construct a decider, read an API key, or open a "
                "socket. Record a cassette with --record."
            )
        replay = getattr(cassette, "replay_decide", None)
        if not callable(replay):
            raise DecideError(
                f"Node '{node.id}': offline replay has no decide recording. "
                "Record a cassette with --record."
            )
        try:
            decision = replay(
                node_id=node.id,
                model=model_hint,
                state=judged,
                questions=questions,
            )
        except CassetteMiss:
            raise
        return _finish(
            node,
            state,
            ctx,
            decision,
            min_conf,
            judged,
            questions,
            digest_model=model_hint,
            record=False,
        )

    token = getattr(ctx, "cancellation", None)
    if token is not None:
        raise_if = getattr(token, "raise_if_requested", None)
        if callable(raise_if):
            raise_if(run_id=getattr(state, "run_id", None))

    judged = _redact_state(judged, ctx)

    blob = json.dumps(judged, ensure_ascii=False, default=str)
    tokens = heuristic_tokens(blob + json.dumps({k: q.wire() for k, q in questions.items()}))
    decider, model_id = get_decider(
        model_hint or None,
        secrets=getattr(ctx, "secrets", None),
        offline=False,
        llm=getattr(ctx, "llm", None),
        min_confidence=min_conf,
    )
    meter = getattr(ctx, "spend_meter", None)
    if meter is not None:
        consult = getattr(meter, "consult_before_call", None)
        if callable(consult):
            consult(model_id, prompt_tokens=tokens)
    decision = decider.decide(state=judged, questions=questions, model=model_id)
    if meter is not None:
        record_usage = getattr(meter, "record_usage", None)
        if callable(record_usage):
            usage = dict(decision.usage or {})
            if not usage:
                usage = {"prompt_tokens": tokens, "total_tokens": tokens}
            record_usage(model_id, usage)
    return _finish(
        node,
        state,
        ctx,
        decision,
        min_conf,
        judged,
        questions,
        digest_model=model_hint,
        record=True,
    )


def _finish(
    node: Any,
    state: Any,
    ctx: Any,
    decision: Decision,
    min_conf: float | None,
    judged: Any,
    questions: Any,
    *,
    digest_model: str,
    record: bool,
) -> dict[str, Any]:
    if record and getattr(ctx, "recording", False):
        cassette = getattr(ctx, "cassette", None)
        if cassette is not None:
            from readyagents.replay.record import contains_secret

            secrets = list(getattr(ctx, "cassette_secrets", None) or [])
            blocked = contains_secret(judged, secrets)
            recorder = getattr(cassette, "record_decide", None)
            if callable(recorder):
                recorder(
                    node_id=node.id,
                    model=digest_model,
                    state=judged,
                    questions=questions,
                    decision=decision,
                    blocked=blocked,
                )
    nxt, reason, low = _route(node, decision, min_conf)
    payload = decision.as_dict(min_confidence=min_conf)
    payload["low_confidence"] = low
    payload["routed"] = nxt
    payload["route_reason"] = reason
    payload["next"] = nxt
    return payload


def _route(
    node: Any, decision: Decision, min_conf: float | None
) -> tuple[str | None, str, list[str]]:
    low = decision.low_confidence_keys(min_conf)
    if low:
        on_low = (getattr(node, "on_low_confidence", None) or "").strip()
        if on_low == "fail":
            raise DecideError(
                f"Node '{node.id}': confidence below {min_conf} for {low}",
                node_id=str(node.id),
                question_keys=low,
            )
        if on_low:
            return on_low, "on_low_confidence", low
    route_on = (getattr(node, "route_on", None) or "").strip()
    if not route_on:
        return getattr(node, "next", None), "next", low
    answer = decision.answers.get(route_on)
    if answer is None:
        raise DecideError(
            f"Node '{node.id}': route_on '{route_on}' has no answer",
            node_id=str(node.id),
        )
    if answer.type == "choice":
        routes = dict(getattr(node, "routes", None) or {})
        choice = str(answer.choice or "")
        if choice in routes:
            return str(routes[choice]), f"routes.{choice}", low
        default = getattr(node, "default", None)
        nxt = str(default) if default is not None else getattr(node, "next", None)
        return nxt, "default", low
    if answer.type == "noul":
        raw_t = getattr(node, "threshold", None)
        threshold = 0.5 if raw_t is None else float(raw_t)
        matched = float(answer.noul or 0.0) >= threshold
        nxt = node.then if matched else node.else_
        return nxt, "then" if matched else "else", low
    return getattr(node, "next", None), "next", low


def _render_state(raw: Any, run_state: Any) -> Any:
    ns = run_state.mapping()
    if isinstance(raw, str):
        return interpolate(raw, ns)
    if isinstance(raw, (dict, list)):
        return interpolate_value(raw, ns)
    return raw


def _redact_state(judged: Any, ctx: Any) -> Any:
    redactor = getattr(ctx, "redactor", None)
    if redactor is None:
        return judged
    method = getattr(redactor, "redact", None) or getattr(redactor, "redact_text", None)
    if not callable(method):
        return judged
    if isinstance(judged, str):
        text = getattr(redactor, "redact_text", None) or method
        return str(text(judged))
    redacted = method(judged)
    return redacted if redacted is not None else judged
