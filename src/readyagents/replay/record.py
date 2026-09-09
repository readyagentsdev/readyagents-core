"""Opt-in recording: provider decorator plus a single tool-dispatch seam."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from readyagents.errors import CassetteError
from readyagents.llm.base import CompletionResult, Message
from readyagents.replay.cassette import Cassette, classify_node_type, classify_tool

_HINT = (
    "Hint: pass --record (or READYAGENTS_RECORD=1) to capture a cassette for "
    "offline replay. Cassettes contain full prompts and completions and must "
    "be reviewed before they are committed."
)


def known_secret_values(settings: Any, secrets: Any = None) -> list[str]:
    """Literal secret values that must never be persisted into a cassette."""
    found: list[str] = []
    for attr in (
        "openai_api_key",
        "anthropic_api_key",
        "openai_compat_api_key",
        "decision_secret",
    ):
        value = getattr(settings, attr, None) if settings is not None else None
        if isinstance(value, str) and value.strip():
            found.append(value.strip())
    if settings is not None:
        method = getattr(settings, "redact_literal_list", None)
        if callable(method):
            found.extend(str(item) for item in method() if str(item))
    if secrets is not None:
        listing = getattr(secrets, "values", None)
        if callable(listing):
            try:
                for item in listing():
                    if isinstance(item, str) and item.strip():
                        found.append(item.strip())
            except Exception:  # noqa: BLE001
                pass
        elif isinstance(secrets, Mapping):
            for item in secrets.values():
                if isinstance(item, str) and item.strip():
                    found.append(item.strip())
        elif isinstance(secrets, (list, tuple)):
            for backend in secrets:
                mapping = getattr(backend, "as_dict", None)
                data = mapping() if callable(mapping) else None
                if isinstance(data, Mapping):
                    for item in data.values():
                        if isinstance(item, str) and item.strip():
                            found.append(item.strip())
    # Skip tiny fragments — they false-positive on ordinary output.
    return [item for item in found if len(item) >= 8]


def contains_secret(value: Any, secrets: list[str]) -> bool:
    if not secrets:
        return False
    text = _as_text(value)
    return any(secret in text for secret in secrets)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    import json

    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


def redact_value(redactor: Any, value: Any) -> Any:
    if redactor is None:
        return value
    method = getattr(redactor, "redact", None)
    if callable(method):
        return method(value)
    text_fn = getattr(redactor, "redact_text", None)
    if callable(text_fn) and isinstance(value, str):
        return text_fn(value)
    return value


class RecordingProvider:
    """Wraps a real provider; writes every completion into a cassette."""

    name = "recording"

    def __init__(
        self,
        cassette: Cassette,
        inner: Any,
        *,
        node_id: str = "",
        redactor: Any = None,
        secrets: list[str] | None = None,
    ) -> None:
        self.cassette = cassette
        self.inner = inner
        self.node_id = node_id
        self.redactor = redactor
        self.secrets = list(secrets or [])

    def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: Any = None,
        **kwargs: Any,
    ) -> CompletionResult:
        result = self.inner.complete(messages, model=model, tools=tools, **kwargs)
        record_llm_result(
            self.cassette,
            node_id=self.node_id,
            model=model,
            messages=messages,
            tools=tools if isinstance(tools, list) else None,
            result=result,
            redactor=self.redactor,
            secrets=self.secrets,
        )
        return result


def record_llm_result(
    cassette: Cassette,
    *,
    node_id: str,
    model: str,
    messages: list[Message],
    tools: list[dict[str, Any]] | None,
    result: CompletionResult,
    redactor: Any = None,
    secrets: list[str] | None = None,
) -> None:
    secret_list = list(secrets or [])
    payload = {
        "messages": [{"role": m.role, "content": m.content} for m in messages],
        "text": result.text,
        "tool_calls": [
            {"name": c.name, "arguments": dict(c.arguments)} for c in (result.tool_calls or [])
        ],
    }
    if contains_secret(payload, secret_list):
        cassette.record_llm(
            node_id=node_id,
            model=model,
            messages=messages,
            tools=tools,
            result=result,
            blocked=True,
        )
        return
    # Key from the original messages so replay of the same call hits; store redacted text.
    safe_result = CompletionResult(
        text=str(redact_value(redactor, result.text)),
        model=result.model,
        raw=result.raw,
        usage=dict(result.usage or {}),
        tool_calls=result.tool_calls,
    )
    cassette.redacted = cassette.redacted or redactor is not None
    cassette.record_llm(
        node_id=node_id,
        model=model,
        messages=messages,
        tools=tools,
        result=safe_result,
        blocked=False,
    )


def dispatch_tool(
    *,
    cassette: Cassette | None,
    offline: bool,
    recording: bool,
    node_id: str,
    name: str,
    arguments: Mapping[str, Any] | None,
    runner: Callable[[], Any],
    redactor: Any = None,
    secrets: list[str] | None = None,
    report_node_type: str | None = None,
    ctx: Any = None,
    state: Any = None,
    raw_arguments: Any = None,
    prompt_tainted: bool = False,
) -> Any:
    """Single tool-dispatch seam used by node tools and agent tool-calls."""
    if ctx is not None and getattr(ctx, "policy", None) is not None and state is not None:
        _enforce_firewall(
            ctx=ctx,
            state=state,
            node_id=node_id,
            name=name,
            arguments=dict(arguments or {}),
            raw_arguments=raw_arguments if raw_arguments is not None else arguments,
            prompt_tainted=prompt_tainted,
        )
    seals = cassette.tool_seals if cassette is not None else None
    klass = classify_tool(name, seals=seals)
    if offline:
        if cassette is None:
            raise CassetteError(
                "Offline replay requires a cassette. Record one with --record "
                "or READYAGENTS_RECORD=1."
            )
        if klass == "recomputed":
            result = runner()
            if cassette.report is not None:
                cassette.report.note(node_id, "recomputed")
            return result
        return cassette.replay_tool(node_id=node_id, name=name, arguments=arguments)
    result = runner()
    if cassette is not None and recording:
        payload = {"name": name, "arguments": dict(arguments or {}), "result": result}
        blocked = contains_secret(payload, list(secrets or []))
        safe_result = redact_value(redactor, result) if not blocked else result
        cassette.record_tool(
            node_id=node_id,
            name=name,
            arguments=dict(arguments or {}),
            result=safe_result,
            blocked=blocked,
        )
        if blocked:
            cassette.report.note(node_id, "unsealable")
        elif klass == "sealable":
            cassette.report.note(node_id, "sealed")
        elif klass == "recomputed":
            cassette.report.note(node_id, "recomputed")
        else:
            cassette.report.note(node_id, "unsealable")
        return result
    if cassette is not None:
        if klass == "recomputed":
            cassette.report.note(node_id, "recomputed")
        elif klass == "sealable":
            cassette.report.note(node_id, "unsealable")
        else:
            cassette.report.note(node_id, "unsealable")
    elif report_node_type:
        cassette_unused = classify_node_type(report_node_type)
        del cassette_unused
    return result


def _enforce_firewall(
    *,
    ctx: Any,
    state: Any,
    node_id: str,
    name: str,
    arguments: dict[str, Any],
    raw_arguments: Any,
    prompt_tainted: bool = False,
) -> None:
    from readyagents.firewall.enforce import Decision, ToolRequest, apply_decision, evaluate

    pin_changed, description = _pin_status(ctx, state, name, node_id)
    decision = evaluate(
        ToolRequest(
            name=name,
            arguments=arguments,
            node_id=node_id,
            raw_arguments=raw_arguments,
            prompt_tainted=prompt_tainted,
        ),
        state,
        ctx.policy,
        pin_changed=pin_changed,
        description=description,
    )
    decided = None
    decision_fn = getattr(ctx, "decision_for", None)
    if callable(decision_fn):
        decided = decision_fn(node_id)
    if decision.action == "gate" and decided:
        if str(decided).strip().lower() in {
            "approve",
            "approved",
            "yes",
            "true",
            "accept",
            "ok",
        }:
            decision = Decision(
                action="allow",
                rule=decision.rule,
                reason="policy gate approved via signed decision",
                detection=decision.detection,
            )
        else:
            decision = Decision(
                action="deny",
                rule=decision.rule,
                reason="policy gate rejected via signed decision",
                detection=decision.detection,
            )
    if ctx.auditor is not None:
        ctx.auditor(
            f"policy_{decision.action}",
            run_id=state.run_id,
            node_id=node_id,
            tool=name,
            rule=decision.rule,
            reason=decision.reason,
            actor=getattr(ctx, "actor", None),
        )
    apply_decision(decision, node_id=node_id, run_id=state.run_id)


_PIN_APPROVE = {"approve", "approved", "yes", "true", "accept", "ok"}


def _pin_status(ctx: Any, state: Any, name: str, node_id: str) -> tuple[bool, str | None]:
    description = None
    descs = getattr(ctx, "mcp_descriptions", None) or {}
    if name in descs:
        description = descs[name]
    if "." not in name:
        return False, description
    server = name.split(".", 1)[0]
    current = (getattr(ctx, "pin_digests", None) or {}).get(server)
    if not current:
        return False, description
    from readyagents.firewall.mcp_pin import load_pin_digest, store_pin_digest

    pin_home = getattr(ctx, "pin_home", None)
    pins = dict(state.metadata.get("mcp_pins") or {})
    stored_run = pins.get(server)
    stored_file = load_pin_digest(pin_home, server)
    stored = stored_run if stored_run is not None else stored_file
    if stored is None:
        pins[server] = current
        state.metadata["mcp_pins"] = pins
        store_pin_digest(pin_home, server, current)
        return False, description
    if stored != current:
        decision_fn = getattr(ctx, "decision_for", None)
        decided = decision_fn(node_id) if callable(decision_fn) else None
        if str(decided or "").strip().lower() in _PIN_APPROVE:
            pins[server] = current
            state.metadata["mcp_pins"] = pins
            store_pin_digest(pin_home, server, current)
            return False, description
        return True, description
    if stored_run is None:
        pins[server] = current
        state.metadata["mcp_pins"] = pins
    return False, description


def first_run_hint(*, recording: bool, has_agent: bool) -> str | None:
    if recording or not has_agent:
        return None
    return _HINT
