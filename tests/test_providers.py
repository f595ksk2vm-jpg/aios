"""Model providers exercised over real HTTP against protocol-faithful stub servers.

No live model is called; what is verified is the wire contract: what we send,
and that what a server returns is parsed into the neutral shape the runtime uses.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aios_kernel.llm.anthropic_provider import AnthropicProvider
from aios_kernel.llm.openai_compat import OpenAICompatProvider
from aios_kernel.llm.types import ProviderError, ProviderRefusal, ToolSpec, tool_results_message, user_message

TOOLS = [ToolSpec("fs_write", "Write a file", {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})]


class Stub:
    def __init__(self, handler):
        self.requests: list[dict] = []
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self._reply(200, {"data": [{"id": "llama-test"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])) or b"{}")
                stub.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                status, payload = handler(len(stub.requests), body)
                if isinstance(payload, str):  # SSE
                    data = payload.encode()
                    self.send_response(status)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("content-length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self._reply(status, payload)

            def _reply(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def sse(events: list[dict]) -> str:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


def anthropic_stream(blocks: list[dict], stop_reason: str, model="claude-opus-5-5", stop_details=None) -> str:
    events = [{"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": [],
                                                    "stop_reason": None, "stop_sequence": None,
                                                    "usage": {"input_tokens": 12, "output_tokens": 1, "cache_read_input_tokens": 100,
                                                              "cache_creation_input_tokens": 0}}}]
    for i, b in enumerate(blocks):
        if b["type"] == "text":
            events += [{"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}},
                       {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": b["text"]}}]
        elif b["type"] == "thinking":
            events += [{"type": "content_block_start", "index": i, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
                       {"type": "content_block_delta", "index": i, "delta": {"type": "signature_delta", "signature": b["signature"]}}]
        else:
            events += [{"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}},
                       {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}}]
        events.append({"type": "content_block_stop", "index": i})
    delta = {"stop_reason": stop_reason, "stop_sequence": None}
    if stop_details:
        delta["stop_details"] = stop_details
    events += [{"type": "message_delta", "delta": delta, "usage": {"output_tokens": 40}}, {"type": "message_stop"}]
    return sse(events)


async def test_anthropic_provider_round_trip():
    def handler(n, body):
        if n == 1:
            return 200, anthropic_stream([{"type": "thinking", "signature": "sig123"}, {"type": "text", "text": "Writing the file."},
                                          {"type": "tool_use", "id": "toolu_1", "name": "fs_write", "input": {"path": "a.txt"}}], "tool_use")
        return 200, anthropic_stream([{"type": "text", "text": "Done."}], "end_turn")

    stub = Stub(handler)
    try:
        p = AnthropicProvider(api_key="test-key", base_url=stub.url, meta={"max_retries": 0})
        history = [user_message("Write a.txt")]
        r1 = await p.chat(model="claude-opus-5-5", system="You are helpful.", messages=history, tools=TOOLS, options={"effort": "high"})
        assert r1.text == "Writing the file." and r1.stop_reason == "tool_use"
        assert r1.tool_calls[0].name == "fs_write" and r1.tool_calls[0].arguments == {"path": "a.txt"}
        assert r1.usage.input_tokens == 112 and r1.usage.output_tokens == 40 and r1.usage.cache_read_tokens == 100

        req = stub.requests[0]
        body = req["body"]
        auth = req["headers"].get("x-api-key") or req["headers"].get("authorization", "").removeprefix("Bearer ")
        assert req["path"].startswith("/v1/messages") and auth == "test-key"
        assert body["model"] == "claude-opus-5-5" and body["stream"] is True and body["system"] == "You are helpful."
        assert body["cache_control"] == {"type": "ephemeral"} and body["output_config"] == {"effort": "high"}
        assert body["tools"][0]["name"] == "fs_write" and body["tools"][0]["eager_input_streaming"] is True
        assert "temperature" not in body and "thinking" not in body  # adaptive thinking by default; no sampling params
        # Refusal fallbacks are opted into on models that support them.
        assert body["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in req["headers"]["anthropic-beta"]

        # Second turn: Claude's own content (thinking block included) is replayed verbatim, tool results follow.
        history += [r1.to_message(), tool_results_message([{"tool_call_id": "toolu_1", "name": "fs_write", "content": "created a.txt", "is_error": False}])]
        r2 = await p.chat(model="claude-opus-5-5", system="You are helpful.", messages=history, tools=TOOLS)
        assert r2.text == "Done." and not r2.tool_calls and r2.stop_reason == "end_turn"
        sent = stub.requests[1]["body"]["messages"]
        assert [b["type"] for b in sent[1]["content"]] == ["thinking", "text", "tool_use"] and sent[1]["content"][0]["signature"] == "sig123"
        assert sent[2] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "created a.txt"}]}

        # Haiku has no server-side fallback: plain endpoint, no beta header.
        await p.chat(model="claude-haiku-5-5", system="", messages=[user_message("hi")], tools=[])
        assert "fallbacks" not in stub.requests[2]["body"] and "tools" not in stub.requests[2]["body"]
    finally:
        stub.close()


async def test_anthropic_refusal_and_errors():
    def handler(n, body):
        if n == 1:
            return 200, anthropic_stream([], "refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": "declined"})
        if n == 2:
            return 529, {"type": "error", "error": {"type": "overloaded_error", "message": "overloaded"}}
        return 400, {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}

    stub = Stub(handler)
    try:
        p = AnthropicProvider(api_key="k", base_url=stub.url, meta={"max_retries": 0})
        with pytest.raises(ProviderRefusal):
            await p.chat(model="claude-opus-5-5", system="", messages=[user_message("x")], tools=[])
        with pytest.raises(ProviderError) as overloaded:
            await p.chat(model="claude-opus-5-5", system="", messages=[user_message("x")], tools=[])
        assert overloaded.value.retryable
        with pytest.raises(ProviderError) as bad:
            await p.chat(model="claude-opus-5-5", system="", messages=[user_message("x")], tools=[])
        assert not bad.value.retryable
    finally:
        stub.close()


async def test_openai_compatible_provider_round_trip():
    def handler(n, body):
        if n == 1:
            return 200, {"model": "llama-test", "usage": {"prompt_tokens": 20, "completion_tokens": 9}, "choices": [{
                "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_9", "type": "function", "function": {"name": "fs_write", "arguments": "{\"path\": \"b.txt\"}"}}]}}]}
        return 200, {"model": "llama-test", "usage": {"prompt_tokens": 30, "completion_tokens": 3},
                     "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "All done."}}]}

    stub = Stub(handler)
    try:
        p = OpenAICompatProvider(base_url=stub.url + "/v1", api_key="sk-local")
        history = [user_message("Write b.txt")]
        r1 = await p.chat(model="llama-test", system="sys", messages=history, tools=TOOLS)
        assert r1.tool_calls[0].arguments == {"path": "b.txt"} and r1.stop_reason == "tool_use" and r1.usage.input_tokens == 20
        body = stub.requests[0]["body"]
        assert stub.requests[0]["path"] == "/v1/chat/completions" and stub.requests[0]["headers"]["authorization"] == "Bearer sk-local"
        assert body["messages"][0] == {"role": "system", "content": "sys"} and body["tools"][0]["function"]["name"] == "fs_write"

        history += [r1.to_message(), tool_results_message([{"tool_call_id": "call_9", "name": "fs_write", "content": "created", "is_error": False}])]
        r2 = await p.chat(model="llama-test", system="sys", messages=history, tools=TOOLS)
        assert r2.text == "All done." and r2.stop_reason == "end_turn"
        sent = stub.requests[1]["body"]["messages"]
        assert sent[2]["tool_calls"][0]["function"]["arguments"] == '{"path": "b.txt"}'
        assert sent[3] == {"role": "tool", "tool_call_id": "call_9", "content": "created"}
        assert (await p.health())["ok"] and (await p.discover())[0]["model_name"] == "llama-test"
    finally:
        stub.close()


async def test_router_prefers_by_policy_and_falls_back_on_outage(kernel):
    k = kernel
    calls = {"n": 0}

    def handler(n, body):
        calls["n"] += 1
        return 200, {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "from backup"}}],
                     "usage": {"prompt_tokens": 1000, "completion_tokens": 1000}}

    stub = Stub(handler)
    try:
        for p in await k.models.providers():  # only the two providers under test are enabled
            await k.models.upsert_provider(name=p.name, kind=p.kind, location=p.location, base_url=p.base_url, enabled=False)
        down = await k.models.upsert_provider(name="primary", kind="openai_compat", location="cloud", base_url="http://127.0.0.1:1/v1")
        up = await k.models.upsert_provider(name="backup", kind="openai_compat", location="local", base_url=stub.url + "/v1")
        await k.models.upsert_model(provider_id=down.id, model_name="big", tier="deep", price_in=5, price_out=25, latency_class=3)
        cheap = await k.models.upsert_model(provider_id=up.id, model_name="small", tier="fast", price_in=0.1, price_out=0.5, latency_class=1)

        quality = await k.models.candidates({"mode": "auto", "tier": "deep", "prefer": "quality"})
        cost = await k.models.candidates({"mode": "auto", "tier": "fast", "prefer": "cost"})
        assert quality[0].ref == "primary/big" and cost[0].ref == "backup/small"

        result, used, _ = await k.models.chat(quality, system="", messages=[user_message("hi")], tools=[], namespace_chain=["ns_root"])
        assert result.text == "from backup" and used.ref == "backup/small" and calls["n"] == 1
        assert k.models.cost(cheap, 1000, 1000) == pytest.approx(0.0006)
        # The failed provider is cooling down, so the next routing decision avoids it up front.
        assert (await k.models.candidates({"mode": "auto", "tier": "deep", "prefer": "quality"}))[0].ref == "backup/small"
        assert any(e.kind == "model.fallback" for e in await k.events.list())
    finally:
        stub.close()
