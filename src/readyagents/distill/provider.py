"""LLM provider for ``adapter:<id>``. Inference is pack-owned via Tuner.complete."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from readyagents.config import Settings, get_settings
from readyagents.errors import DistillUnsigned, LLMError
from readyagents.llm.base import CompletionResult, Message


class AdapterProvider:
    """Serve a signed adapter through the pack tuner. Missing pack is LLMError (fallback)."""

    name = "adapter"

    def __init__(self, artifact: Path, tuner: Any, adapter_id: str) -> None:
        self.artifact = Path(artifact)
        self.tuner = tuner
        self.adapter_id = adapter_id

    def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> CompletionResult:
        del tools, kwargs
        if self.tuner is None or not callable(getattr(self.tuner, "complete", None)):
            raise LLMError(f"adapter {self.adapter_id} requires the training pack for inference")
        prompt = _prompt_text(messages)
        try:
            text = self.tuner.complete(self.artifact, prompt)
        except LLMError:
            raise
        except Exception as extra:
            raise LLMError(f"adapter {self.adapter_id} inference failed: {extra}") from extra
        return CompletionResult(text=str(text or ""), model=f"adapter:{self.adapter_id}")


def adapter_provider(
    adapter_id: str,
    *,
    settings: Settings | None = None,
    tuner: Any = None,
) -> AdapterProvider:
    from readyagents.distill.adapters import require_signed
    from readyagents.distill.train import collect_tuner

    settings = settings or get_settings()
    try:
        record = require_signed(adapter_id, settings=settings)
    except DistillUnsigned as extra:
        raise LLMError(str(extra)) from extra
    resolved = tuner if tuner is not None else collect_tuner()
    artifact = Path(record.path)
    return AdapterProvider(artifact, resolved, adapter_id)


def _prompt_text(messages: list[Message]) -> str:
    for item in reversed(list(messages or [])):
        content = getattr(item, "content", None)
        if content:
            return str(content)
        if isinstance(item, dict) and item.get("content"):
            return str(item["content"])
    return ""
