#!/usr/bin/env python3
"""A stand-in "model" for demos and development on machines with no LLM available.

    python scripts/standin_model.py --port 8701

It speaks the OpenAI chat-completions protocol, so AI-OS talks to it exactly as
it would to Ollama or any hosted model. IT IS NOT A MODEL: replies come from the
fixed rules below. What it is good for is watching the real machinery work —
planning into a task graph, parallel execution, delegation, review, approvals,
sandboxed code execution, files and git history — without spending tokens.
Everything the "agents" do through tools is real; only their words are canned.

Register it in the UI (AI-OS → Models → Add a provider) as an OpenAI-compatible
provider with base URL http://127.0.0.1:8701/v1 and add a model named "standin".
"""
from __future__ import annotations

import argparse
import json
import re
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CODE = '''"""Order pricing for the storefront."""


def line_total(unit_price: float, quantity: int) -> float:
    if quantity < 0:
        raise ValueError("quantity must not be negative")
    return round(unit_price * quantity, 2)


def order_total(lines: list[tuple[float, int]], discount_percent: float = 0) -> float:
    if not 0 <= discount_percent <= 100:
        raise ValueError("discount must be between 0 and 100")
    subtotal = sum(line_total(price, qty) for price, qty in lines)
    return round(subtotal * (1 - discount_percent / 100), 2)
'''

TESTS = '''import unittest

from pricing import line_total, order_total


class PricingTests(unittest.TestCase):
    def test_line_total(self):
        self.assertEqual(line_total(2.50, 4), 10.00)

    def test_order_total_with_discount(self):
        self.assertEqual(order_total([(2.50, 4), (1.25, 2)], discount_percent=10), 11.25)

    def test_rejects_negative_quantity(self):
        with self.assertRaises(ValueError):
            line_total(1.0, -1)

    def test_rejects_bad_discount(self):
        with self.assertRaises(ValueError):
            order_total([(1.0, 1)], discount_percent=120)


if __name__ == "__main__":
    unittest.main(verbosity=2)
'''


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "notes"


def called(messages: list[dict], name: str) -> bool:
    return any(tc["function"]["name"] == name for m in messages if m["role"] == "assistant" for tc in m.get("tool_calls") or [])


def last_tool_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m["role"] == "tool":
            return m["content"]
    return ""


def call(tool: str, **arguments) -> dict:
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"call_{uuid.uuid4().hex[:10]}", "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments)}}]}


def say(text: str) -> dict:
    return {"role": "assistant", "content": text}


def plan_for(brief: str) -> list[dict]:
    objective = (re.search(r"Objective: (.+)", brief) or re.search(r"# Task: Plan: (.+)", brief)).group(1).strip()
    low = objective.lower()
    if "foundation" in low:
        return [
            {"key": "market", "title": "Research the market", "role": "market_researcher", "needs_review": True,
             "description": "Describe the market, target customers and competitors. Write docs/market-research.md."},
            {"key": "model", "title": "Design the business model", "role": "cfo",
             "description": "Value proposition, revenue model, costs, pricing hypothesis. Write docs/business-model.md and record the first planned expense."},
            {"key": "org", "title": "Design the organization", "role": "coo",
             "description": "Which roles the business needs now and later. Write docs/org-design.md."},
            {"key": "strategy", "title": "Write the strategy and set KPIs", "role": "ceo", "depends_on": ["market", "model", "org"],
             "description": "Positioning, go-to-market and milestones in docs/strategy.md. Update the business profile and define KPIs."},
        ]
    if low.startswith("deliver project"):
        return [
            {"key": "design", "title": "Design the solution", "role": "software_architect",
             "description": "Write docs/design.md: components, interfaces and the test approach."},
            {"key": "build", "title": "Implement the pricing module", "role": "backend_developer", "depends_on": ["design"], "needs_review": True,
             "description": "Implement pricing.py with tests in test_pricing.py and run the tests."},
            {"key": "verify", "title": "Verify the build", "role": "qa_engineer", "depends_on": ["build"],
             "description": "Run the test suite independently and write docs/test-report.md with the actual output."},
        ]
    return [
        {"key": "research", "title": f"Research: {objective}"[:120], "role": "market_researcher",
         "description": f"Gather what is known that bears on: {objective}. Write the findings to docs/."},
        {"key": "deliver", "title": f"Deliver: {objective}"[:120], "depends_on": ["research"],
         "description": f"Using the research, produce the deliverable for: {objective}. Write it to docs/."},
    ]


def reply(messages: list[dict], tools: list[dict]) -> dict:
    names = {t["function"]["name"] for t in tools}
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    convo = [m for m in messages if m["role"] != "system"]
    brief = convo[0]["content"] if convo else ""
    title = brief.splitlines()[0].removeprefix("# Task: ") if brief else ""
    me = (re.search(r"You are (\w+),", system) or [None, "The agent"])[1]

    if "asks for your input" in brief:  # a colleague is consulting this agent
        return say("My view: the premise holds for small independent shops, but I would not assume the same for chains - "
                   "their purchasing is centralised. Treat the chain segment as unvalidated until we have spoken to two of them.")
    if "plan_submit" in names and not called(convo, "plan_submit"):
        return call("plan_submit", summary="Independent work in parallel, then integration.", tasks=plan_for(brief))
    if "objective_complete" in names and not called(convo, "objective_complete"):
        results = brief.split("recommend.", 1)[-1].split("## Objective this contributes to")[0]
        done = re.findall(r"^## (.+)$", results, flags=re.M)
        return call("objective_complete", achieved=True, score=0.86,
                    summary="Completed: " + "; ".join(done) + ". Outputs are in the workspace (see docs/ and the repository history).",
                    lessons=["Splitting independent work lets it run in parallel; integration tasks should depend on all of it."],
                    follow_ups=["Validate the key assumptions with real customers before committing further budget."])
    if "review_submit" in names and not called(convo, "review_submit"):
        if "fs_list" in names and not called(convo, "fs_list"):
            return call("fs_list", path=".", recursive=True)
        if "code_execute" in names and "test_pricing.py" in last_tool_text(convo) and not called(convo, "code_execute"):
            return call("code_execute", language="bash", code="python3 -m unittest -v test_pricing 2>&1 | tail -8")
        return call("review_submit", verdict="approve", score=0.9,
                    comments="Checked the delivered files in the workspace; they match the task and the claims in the report.")

    low = title.lower()
    if low.startswith("implement"):
        if not called(convo, "fs_write"):
            return call("fs_write", path="pricing.py", content=CODE)
        if sum(tc["function"]["name"] == "fs_write" for m in convo if m["role"] == "assistant" for tc in m.get("tool_calls") or []) < 2:
            return call("fs_write", path="test_pricing.py", content=TESTS)
        if not called(convo, "code_execute"):
            return call("code_execute", language="bash", code="python3 -m unittest -v test_pricing 2>&1")
        return say("Implemented pricing.py with unit tests in test_pricing.py and ran them. Test output:\n" + last_tool_text(convo)[-600:])
    if low.startswith("verify"):
        if not called(convo, "code_execute"):
            return call("code_execute", language="bash", code="python3 -m unittest -v test_pricing 2>&1")
        if not called(convo, "fs_write"):
            return call("fs_write", path="docs/test-report.md",
                        content="# Test report\n\nIndependent run of the suite:\n\n```\n" + last_tool_text(convo)[-900:] + "\n```\n")
        return say("Ran the suite independently; results are recorded in docs/test-report.md.")

    path = f"docs/{slug(title)}.md"
    if "business model" in low:
        path = "docs/business-model.md"
        if "ledger_record" in names and not called(convo, "ledger_record"):
            # Recording an expense is a financial action: the gateway pauses this run for a human decision.
            return call("ledger_record", kind="expense", amount=180, category="tooling", memo="First-year domain and hosting for the storefront")
    if "strategy" in low:
        path = "docs/strategy.md"
        if "agent_ask" in names and not called(convo, "agent_ask"):
            return call("agent_ask", to="market_researcher", question="Is it safe to assume chains buy the same way independents do?")
        if "business_profile_update" in names and not called(convo, "business_profile_update"):
            return call("business_profile_update", stage="validation", mission="Make ordering effortless for independent shops.",
                        business_model="Monthly subscription per location, with a free starter tier.",
                        strategy="Win one city first through direct outreach; expand once retention holds for three months.")
        if "kpi_define" in names and not called(convo, "kpi_define"):
            return call("kpi_define", key="paying_locations", name="Paying locations", unit="shops", target=50)
    if "research the market" in low:
        path = "docs/market-research.md"
    if "organization" in low:
        path = "docs/org-design.md"
    if "design the solution" in low:
        path = "docs/design.md"
    if "fs_write" in names and not called(convo, "fs_write"):
        body = brief.split("\n\n", 1)[-1][:900]
        return call("fs_write", path=path, content=f"# {title}\n\n_Prepared by {me} (stand-in model: placeholder content)._\n\n"
                                                   f"## Brief\n\n{body}\n\n## Findings\n\n- Placeholder finding one.\n- Placeholder finding two.\n")
    extra = ""
    if called(convo, "ledger_record"):
        extra = " The planned expense was " + ("recorded after human approval." if "Ledger entry" in "".join(
            m["content"] for m in convo if m["role"] == "tool") else "not recorded: a human rejected it, so I left it out.")
    return say(f"Done. The deliverable is at {path}.{extra}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send({"object": "list", "data": [{"id": "standin", "object": "model"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        try:
            message = reply(body.get("messages", []), body.get("tools") or [])
        except Exception as e:  # keep the demo alive and make the problem visible in the transcript
            message = say(f"(stand-in model error: {type(e).__name__}: {e})")
        prompt = sum(len(json.dumps(m)) for m in body.get("messages", [])) // 4
        self._send({"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion", "model": "standin",
                    "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
                    "usage": {"prompt_tokens": prompt, "completion_tokens": len(json.dumps(message)) // 4}})


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8701)
    args = ap.parse_args()
    print(f"stand-in model (NOT a real model) listening on http://{args.host}:{args.port}/v1")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
