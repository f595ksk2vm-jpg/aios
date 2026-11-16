"""Deterministic provider for automated tests and offline development.

It is not a model: a Python callable decides each reply. It exists so the
orchestration machinery can be exercised without network access, and is only
registered when ``AIOS_ENABLE_SCRIPTED_PROVIDER`` is set or a test installs it.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from .types import ChatResult, ToolSpec, Usage

Responder = Callable[[str, str, list[dict], list[ToolSpec]], Awaitable[ChatResult] | ChatResult]


def _default(model: str, system: str, messages: list[dict], tools: list[ToolSpec]) -> ChatResult:
    return ChatResult(text="(scripted provider: no responder installed)")


class ScriptedProvider:
    kind = "scripted"
    responder: Responder = staticmethod(_default)  # class-level so tests can install one before the kernel starts

    def __init__(self, **_: object):
        pass

    async def chat(self, *, model: str, system: str, messages: list[dict], tools: list[ToolSpec],
                   max_tokens: int = 16000, options: dict | None = None) -> ChatResult:
        res = type(self).responder(model, system, messages, tools)
        if hasattr(res, "__await__"):
            res = await res  # type: ignore[assignment]
        if not res.usage.input_tokens:
            res.usage = Usage(input_tokens=sum(len(str(m)) for m in messages) // 4 + len(system) // 4,
                              output_tokens=max(1, len(res.text) // 4))
        res.served_model = res.served_model or model
        return res  # type: ignore[return-value]

    async def health(self) -> dict:
        return {"ok": True, "detail": "in-process"}

    async def discover(self) -> list[dict]:
        return []
