"""Pure mapping between vendor tool-call payloads and engine ToolCall objects."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from readyagents.llm.base import Message, ToolCall


def parse_json_arguments(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return {}
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return {"_raw": raw}
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
    return {"value": raw}


def spec_from_tool(tool: Any) -> dict[str, Any]:
    raw = getattr(tool, "schema", None)
    if isinstance(raw, dict) and raw:
        schema = dict(raw)
    else:
        schema = {"type": "object", "properties": {}}
    return {
        "name": str(getattr(tool, "name", "")),
        "description": str(getattr(tool, "description", "") or ""),
        "schema": schema,
    }


def openai_tools_payload(tools: Sequence[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    out: list[dict[str, Any]] = []
    for spec in tools:
        if spec.get("type") == "function" and isinstance(spec.get("function"), dict):
            out.append(dict(spec))
            continue
        name = spec.get("name")
        if not name:
            fn = spec.get("function") if isinstance(spec.get("function"), dict) else {}
            name = fn.get("name")
            if not name:
                continue
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": fn.get("description") or spec.get("description") or "",
                        "parameters": fn.get("parameters")
                        or spec.get("schema")
                        or {"type": "object", "properties": {}},
                    },
                }
            )
            continue
        schema = (
            spec.get("schema") or spec.get("parameters") or {"type": "object", "properties": {}}
        )
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": spec.get("description") or "",
                    "parameters": schema,
                },
            }
        )
    return out or None


def anthropic_tools_payload(tools: Sequence[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    out: list[dict[str, Any]] = []
    for spec in tools:
        if spec.get("input_schema") and spec.get("name") and spec.get("type") != "function":
            out.append(
                {
                    "name": spec["name"],
                    "description": spec.get("description") or "",
                    "input_schema": spec["input_schema"],
                }
            )
            continue
        if spec.get("type") == "function":
            fn = spec.get("function") if isinstance(spec.get("function"), dict) else {}
            name = fn.get("name")
            if not name:
                continue
            out.append(
                {
                    "name": name,
                    "description": fn.get("description") or spec.get("description") or "",
                    "input_schema": fn.get("parameters")
                    or spec.get("schema")
                    or {"type": "object", "properties": {}},
                }
            )
            continue
        name = spec.get("name")
        if not name:
            continue
        out.append(
            {
                "name": name,
                "description": spec.get("description") or "",
                "input_schema": spec.get("schema")
                or spec.get("parameters")
                or {"type": "object", "properties": {}},
            }
        )
    return out or None


def tool_calls_from_openai_message(message: Any) -> list[ToolCall]:
    raw_calls = getattr(message, "tool_calls", None)
    if raw_calls is None and isinstance(message, dict):
        raw_calls = message.get("tool_calls")
    if not raw_calls:
        return []
    out: list[ToolCall] = []
    for index, call in enumerate(raw_calls):
        if isinstance(call, dict):
            cid = str(call.get("id") or f"call_{index}")
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str(fn.get("name") or call.get("name") or "")
            args = parse_json_arguments(fn.get("arguments") if fn else call.get("arguments"))
        else:
            cid = str(getattr(call, "id", None) or f"call_{index}")
            fn = getattr(call, "function", None)
            name = str(getattr(fn, "name", None) or getattr(call, "name", "") or "")
            args_raw = (
                getattr(fn, "arguments", None)
                if fn is not None
                else getattr(call, "arguments", None)
            )
            args = parse_json_arguments(args_raw)
        if name:
            out.append(ToolCall(id=cid, name=name, arguments=args))
    return out


def tool_calls_from_anthropic_content(blocks: Any) -> list[ToolCall]:
    if not blocks:
        return []
    out: list[ToolCall] = []
    for index, block in enumerate(blocks):
        if isinstance(block, dict):
            btype = block.get("type")
            name = block.get("name")
            cid = block.get("id")
            inp = block.get("input")
        else:
            btype = getattr(block, "type", None)
            name = getattr(block, "name", None)
            cid = getattr(block, "id", None)
            inp = getattr(block, "input", None)
        if str(btype) != "tool_use" or not name:
            continue
        out.append(
            ToolCall(
                id=str(cid or f"call_{index}"),
                name=str(name),
                arguments=parse_json_arguments(inp),
            )
        )
    return out


def messages_to_openai(messages: Sequence[Message], *, store: Any = None) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "tool":
            row: dict[str, Any] = {
                "role": "tool",
                "content": message.content or "",
                "tool_call_id": message.tool_call_id or "",
            }
            if message.name:
                row["name"] = message.name
            payload.append(row)
            continue
        from readyagents.media.payload import openai_content

        row = {"role": message.role, "content": openai_content(message, store=store)}
        if message.tool_calls:
            row["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
        payload.append(row)
    return payload


def messages_to_anthropic(messages: Sequence[Message]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    chat: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    def flush_tool_results() -> None:
        if pending:
            chat.append({"role": "user", "content": list(pending)})
            pending.clear()

    for message in messages:
        if message.role == "system":
            if message.content:
                system_parts.append(message.content)
            continue
        if message.role == "tool":
            pending.append(
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id or "",
                    "content": message.content or "",
                }
            )
            continue
        flush_tool_results()
        if message.role == "assistant" and message.tool_calls:
            blocks: list[dict[str, Any]] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            for call in message.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                )
            chat.append({"role": "assistant", "content": blocks})
        else:
            role = "assistant" if message.role == "assistant" else "user"
            from readyagents.media.payload import anthropic_content

            chat.append({"role": role, "content": anthropic_content(message)})
    flush_tool_results()
    return "\n".join(system_parts).strip(), chat


def tool_calls_to_json(calls: Sequence[ToolCall] | None) -> list[dict[str, Any]]:
    return [
        {"id": call.id, "name": call.name, "arguments": dict(call.arguments)}
        for call in (calls or [])
    ]


def tool_calls_from_json(raw: Any) -> list[ToolCall]:
    if not isinstance(raw, list):
        return []
    out: list[ToolCall] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not name:
            continue
        out.append(
            ToolCall(
                id=str(item.get("id") or f"call_{index}"),
                name=name,
                arguments=parse_json_arguments(item.get("arguments")),
            )
        )
    return out
