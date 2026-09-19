"""type: decide — typed questions, confidence-gated routing."""

from __future__ import annotations

import json
from typing import Any

from readyagents.cost.tokens import heuristic_tokens
from readyagents.decide.base import questions_from_mapping
from readyagents.decide.registry import get_decider
from readyagents.decide.types import Decision
from readyagents.errors import CassetteMiss, DecideError, EgressDenied, PolicyDenied
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

    vendor_state = _redact_state(judged, ctx)

    decider, model_id = get_decider(
        model_hint or None,
        secrets=getattr(ctx, "secrets", None),
        offline=False,
        llm=getattr(ctx, "llm", None),
        min_confidence=min_conf,
    )
    _enforce_decider_policy(ctx, node, state, decider.name, model_id)

    blob = json.dumps(vendor_state, ensure_ascii=False, default=str)
    tokens = heuristic_tokens(blob + json.dumps({k: q.wire() for k, q in questions.items()}))
    meter = getattr(ctx, "spend_meter", None)
    if meter is not None:
        consult = getattr(meter, "consult_before_call", None)
        if callable(consult):
            consult(model_id, prompt_tokens=tokens)
    decision = decider.decide(state=vendor_state, questions=questions, model=model_id)
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
    _audit_decide(ctx, state, node, decision, judged, nxt, low)
    return payload


def _audit_decide(
    ctx: Any,
    state: Any,
    node: Any,
    decision: Decision,
    judged: Any,
    nxt: str | None,
    low: list[str],
) -> None:
    auditor = getattr(ctx, "auditor", None)
    if auditor is None:
        return
    import hashlib

    from readyagents.llm.cache import canonical_json_bytes

    state_hash = hashlib.sha256(canonical_json_bytes(judged)).hexdigest()
    auditor(
        "decide",
        run_id=getattr(state, "run_id", None),
        node_id=node.id,
        decider=decision.decider,
        model=decision.model,
        question_keys=list(decision.answers),
        routed=nxt,
        low_confidence=low,
        state_hash=state_hash,
    )


def _enforce_decider_policy(
    ctx: Any, node: Any, state: Any, decider_name: str, model_id: str
) -> None:
    policy = getattr(ctx, "policy", None)
    if policy is None or not hasattr(policy, "decider_rule"):
        return
    rule_id, rule = policy.decider_rule(decider_name)
    if rule is None and getattr(policy, "default", "allow") == "deny":
        raise PolicyDenied(node.id, f"decider '{decider_name}' is not allowed", rule="default")
    if rule is not None and rule.allow_models:
        allowed = {str(item) for item in rule.allow_models}
        if model_id not in allowed and f"{decider_name}:{model_id}" not in allowed:
            raise PolicyDenied(
                node.id,
                f"decider model '{model_id}' is not allowed",
                rule=rule_id,
            )
    tainted = _state_is_tainted(node, state)
    action = getattr(rule, "on_tainted", "allow") if rule is not None else "allow"
    if tainted and action == "deny":
        raise PolicyDenied(node.id, "tainted state cannot use this decider", rule=rule_id)


def preflight_sovereign_decide(workflow: Any, *, settings: Any) -> None:
    """Refuse hosted Jev before the first node when --sovereign is on."""
    from readyagents.secrets import secret_for_provider
    from readyagents.sovereign.egress import is_loopback_url

    base = str(getattr(settings, "typesafe_base_url", None) or "https://api.typesafe.ai")
    has_key = bool(secret_for_provider("typesafe", settings=settings))
    for node in getattr(workflow, "nodes", None) or []:
        kind = str(getattr(node, "type", "") or "")
        if kind == "decide":
            ref = str(getattr(node, "decider", None) or "")
        elif kind == "classify":
            rem = dict(getattr(node, "model_for_remainder", None) or {})
            ref = str(rem.get("decider") or "")
        else:
            continue
        name = ref.split(":", 1)[0].strip().lower() if ref else ""
        if name == "shim":
            continue
        if name == "":
            if kind != "decide" or not has_key:
                continue
            name = "jev"
        if name != "jev":
            continue
        if is_loopback_url(base):
            continue
        raise EgressDenied(base, node_id=str(node.id))


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


def _state_is_tainted(node: Any, state: Any) -> bool:
    from readyagents.firewall.taint import UNTRUSTED, provenance_for_template, walk_strings

    raw = getattr(node, "state", None)
    texts: list[str]
    if isinstance(raw, str):
        texts = [raw]
    else:
        texts = [text for text in walk_strings(raw) if "{{" in str(text)]
    return any(
        provenance_for_template(state, text, node_id=getattr(node, "id", None)).trust == UNTRUSTED
        for text in texts
    )


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
