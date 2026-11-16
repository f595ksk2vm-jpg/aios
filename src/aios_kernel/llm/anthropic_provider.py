"""Claude through the official Anthropic SDK."""
from __future__ import annotations

import anthropic

from .types import ChatResult, ProviderError, ProviderRefusal, ToolCall, ToolSpec, Usage

# Models that accept the server-side refusal `fallbacks` parameter.
_FALLBACK_MODELS = ("claude-fable-5", "claude-opus-5", "claude-sonnet-5-5")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _to_anthropic(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            raw = m.get("raw") or {}
            if raw.get("provider") == "anthropic" and raw.get("content"):
                # Replay exactly what Claude produced (thinking blocks included, unmodified).
                out.append({"role": "assistant", "content": raw["content"]})
                continue
            blocks: list[dict] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for c in m.get("tool_calls", []):
                blocks.append({"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]})
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(no content)"}]})
        elif role == "tool":
            out.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": r["tool_call_id"], "content": r["content"] or "(empty)",
                 **({"is_error": True} if r.get("is_error") else {})}
                for r in m["results"]]})
    return out


class AnthropicProvider:
    kind = "anthropic"

    def __init__(self, *, api_key: str | None, base_url: str | None = None, meta: dict | None = None):
        self.meta = meta or {}
        kwargs: dict = {"max_retries": int(self.meta.get("max_retries", 2))}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        # Without an explicit key the SDK resolves credentials from its
        # standard environment (ANTHROPIC_API_KEY, auth profiles, ...).
        self.client = anthropic.AsyncAnthropic(**kwargs)

    async def chat(self, *, model: str, system: str, messages: list[dict], tools: list[ToolSpec],
                   max_tokens: int = 16000, options: dict | None = None) -> ChatResult:
        options = options or {}
        params: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": _to_anthropic(messages),
            # Cache the stable prefix (tools + system + earlier turns) across the steps of a run.
            "cache_control": {"type": "ephemeral"},
        }
        if system:
            params["system"] = system
        if tools:
            eager = bool(self.meta.get("eager_tool_streaming", True))
            params["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema,
                 **({"eager_input_streaming": True} if eager else {})}
                for t in tools]
        if options.get("effort"):
            params["output_config"] = {"effort": options["effort"]}

        use_fallbacks = bool(self.meta.get("refusal_fallbacks", True)) and model.startswith(_FALLBACK_MODELS)
        try:
            if use_fallbacks:
                # On a policy decline the API re-runs the request on a fallback model within the same call.
                ctx = self.client.beta.messages.stream(betas=[_FALLBACK_BETA], fallbacks="default", **params)
            else:
                ctx = self.client.messages.stream(**params)
            async with ctx as stream:
                resp = await stream.get_final_message()
        except anthropic.RateLimitError as e:
            raise ProviderError(f"anthropic rate limited: {e.message}", retryable=True) from e
        except anthropic.APIStatusError as e:
            raise ProviderError(f"anthropic API error {e.status_code}: {e.message}", retryable=e.status_code >= 500) from e
        except anthropic.APIConnectionError as e:
            raise ProviderError(f"anthropic connection error: {e}", retryable=True) from e

        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            why = getattr(details, "explanation", None) or getattr(details, "category", None) or "no details"
            raise ProviderRefusal(f"model declined the request ({why})")

        text_parts, calls = [], []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {})))
        u = resp.usage
        usage = Usage(
            input_tokens=(u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0)
                         + (getattr(u, "cache_creation_input_tokens", 0) or 0),
            output_tokens=u.output_tokens or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        stop = {"tool_use": "tool_use", "max_tokens": "max_tokens", "pause_turn": "pause"}.get(resp.stop_reason or "", "end_turn")
        return ChatResult(
            text="".join(text_parts), tool_calls=calls, usage=usage, stop_reason=stop, served_model=resp.model,
            raw={"provider": "anthropic", "content": [b.to_dict() for b in resp.content]},
        )

    async def health(self) -> dict:
        try:
            page = await self.client.models.list(limit=1)
            return {"ok": True, "detail": f"reachable ({len(page.data)} model listed)"}
        except Exception as e:  # health probe: report, don't raise
            return {"ok": False, "detail": f"{type(e).__name__}: {e}"}

    async def discover(self) -> list[dict]:
        out = []
        async for m in self.client.models.list():
            out.append({"model_name": m.id, "display_name": getattr(m, "display_name", m.id),
                        "context_window": getattr(m, "max_input_tokens", None)})
        return out
