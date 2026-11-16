"""Provider-neutral chat types.

Runs persist conversations in this neutral shape, so a run can move between
providers (cloud ↔ local, fallback after an outage) mid-conversation. An
assistant message may also carry the provider's raw content, which the same
provider replays verbatim (required for Claude thinking blocks).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class ChatResult:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = "end_turn"  # end_turn | tool_use | max_tokens | pause
    served_model: str = ""
    raw: dict | None = None  # {"provider": kind, "content": [...]} for verbatim replay

    def to_message(self) -> dict:
        msg: dict[str, Any] = {"role": "assistant", "content": self.text}
        if self.tool_calls:
            msg["tool_calls"] = [c.to_dict() for c in self.tool_calls]
        if self.raw:
            msg["raw"] = self.raw
        return msg


class ProviderError(Exception):
    """A model call failed. ``retryable`` drives fallback to the next candidate model."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class ProviderRefusal(ProviderError):
    """The model declined the request on policy grounds; retrying elsewhere is not attempted."""

    def __init__(self, message: str):
        super().__init__(message, retryable=False)


def user_message(text: str) -> dict:
    return {"role": "user", "content": text}


def tool_results_message(results: list[dict]) -> dict:
    """results: [{"tool_call_id", "name", "content", "is_error"}]"""
    return {"role": "tool", "results": results}
