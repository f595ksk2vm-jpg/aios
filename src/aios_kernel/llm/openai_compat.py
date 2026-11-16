"""Any server that speaks the OpenAI chat-completions protocol.

This one adapter covers locally hosted models (Ollama, vLLM, llama.cpp,
LM Studio, LocalAI) and most hosted inference APIs. Only ``base_url`` and an
optional key differ, which is what makes local, cloud and hybrid model
placement a configuration choice rather than a code change.
"""
from __future__ import annotations

import json
import uuid

import httpx

from .types import ChatResult, ProviderError, ToolCall, ToolSpec, Usage


def _to_openai(system: str, messages: list[dict]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}] if system else []
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            msg: dict = {"role": "assistant", "content": m.get("content") or None}
            if m.get("tool_calls"):
                msg["tool_calls"] = [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                    for c in m["tool_calls"]]
            out.append(msg)
        elif role == "tool":
            for r in m["results"]:
                content = r["content"] or "(empty)"
                out.append({"role": "tool", "tool_call_id": r["tool_call_id"],
                            "content": f"ERROR: {content}" if r.get("is_error") else content})
    return out


class OpenAICompatProvider:
    kind = "openai_compat"

    def __init__(self, *, base_url: str, api_key: str | None = None, meta: dict | None = None):
        self.base_url = base_url.rstrip("/")
        self.meta = meta or {}
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.client = httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(float(self.meta.get("timeout_s", 600)), connect=10))

    async def chat(self, *, model: str, system: str, messages: list[dict], tools: list[ToolSpec],
                   max_tokens: int = 16000, options: dict | None = None) -> ChatResult:
        body: dict = {"model": model, "messages": _to_openai(system, messages),
                      self.meta.get("max_tokens_param", "max_tokens"): max_tokens}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                               "parameters": t.input_schema}} for t in tools]
        if "temperature" in self.meta:
            body["temperature"] = self.meta["temperature"]
        try:
            r = await self.client.post(f"{self.base_url}/chat/completions", json=body)
        except httpx.HTTPError as e:
            raise ProviderError(f"cannot reach {self.base_url}: {type(e).__name__}: {e}", retryable=True) from e
        if r.status_code >= 400:
            raise ProviderError(f"{self.base_url} returned {r.status_code}: {r.text[:500]}",
                                retryable=r.status_code in (408, 409, 429) or r.status_code >= 500)
        try:
            data = r.json()
            choice = data["choices"][0]
            msg = choice["message"]
        except (ValueError, KeyError, IndexError) as e:
            raise ProviderError(f"malformed response from {self.base_url}: {r.text[:300]}", retryable=True) from e

        calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            args = fn.get("arguments") or "{}"
            try:
                parsed = json.loads(args) if isinstance(args, str) else dict(args)
            except ValueError:
                # Hand the model its own malformed arguments back as a tool error on the next turn.
                parsed = {"__invalid_json__": str(args)[:2000]}
            calls.append(ToolCall(id=tc.get("id") or f"call_{uuid.uuid4().hex[:12]}", name=fn.get("name", ""), arguments=parsed))
        u = data.get("usage") or {}
        finish = choice.get("finish_reason")
        stop = "tool_use" if calls else ("max_tokens" if finish == "length" else "end_turn")
        return ChatResult(text=msg.get("content") or "", tool_calls=calls,
                          usage=Usage(input_tokens=int(u.get("prompt_tokens") or 0), output_tokens=int(u.get("completion_tokens") or 0)),
                          stop_reason=stop, served_model=data.get("model") or model)

    async def health(self) -> dict:
        try:
            r = await self.client.get(f"{self.base_url}/models", timeout=5)
            return {"ok": r.status_code < 400, "detail": f"HTTP {r.status_code}"}
        except httpx.HTTPError as e:
            return {"ok": False, "detail": f"{type(e).__name__}: {e}"}

    async def discover(self) -> list[dict]:
        r = await self.client.get(f"{self.base_url}/models", timeout=10)
        r.raise_for_status()
        return [{"model_name": m["id"], "display_name": m["id"], "context_window": None} for m in r.json().get("data", [])]

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        r = await self.client.post(f"{self.base_url}/embeddings", json={"model": model, "input": texts}, timeout=120)
        r.raise_for_status()
        return [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d.get("index", 0))]
