"""Tool registry and the gateway every agent action passes through.

The gateway is the single enforcement point: an agent's model output is only
ever a *request*. Whether it runs is decided here, from data the agent cannot
modify — its permissions, its autonomy level, the tool's category, the
emergency stop — and every decision is audited.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import jsonschema

from ..errors import AiosError, Halted
from ..llm.types import ToolCall, ToolSpec
from ..models import Agent
from ..security import policy

log = logging.getLogger("aios.tools")
MAX_RESULT_CHARS = 24_000


@dataclass
class ToolContext:
    kernel: Any
    agent: Agent
    namespace_id: str  # namespace of the task being worked on
    workspace: Path
    run_id: str | None = None
    task_id: str | None = None

    @property
    def actor(self) -> str:
        return f"agent:{self.agent.id}"


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[ToolContext, dict], Awaitable[Any]]
    permission: str                      # permission an agent must hold, e.g. "fs.write"
    category: str | Callable[[dict], str] = "read"   # policy category, may depend on the arguments
    group: str = "core"
    layer: str = "kernel"
    terminal: bool = False               # a successful call ends the run (structured hand-back tools)
    timeout_s: int = 180

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.input_schema)

    def category_for(self, args: dict) -> str:
        return self.category(args) if callable(self.category) else self.category

    def describe(self) -> dict:
        return {"name": self.name, "description": self.description, "permission": self.permission, "group": self.group,
                "layer": self.layer, "category": "dynamic" if callable(self.category) else self.category,
                "input_schema": self.input_schema}


@dataclass
class ToolOutcome:
    status: str  # ok | error | denied | needs_approval
    content: str = ""
    approval_id: str | None = None
    data: dict = field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.status in {"error", "denied"}


def schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


class ToolRegistry:
    def __init__(self, kernel):
        self.kernel = kernel
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise AiosError(f"tool {tool.name!r} already registered")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return sorted(self._tools.values(), key=lambda t: (t.layer, t.group, t.name))

    def specs_for(self, agent: Agent, run_tools: list[str]) -> list[ToolSpec]:
        """Tools shown to the model: the agent's configured tools it is actually permitted to use, plus run-scoped ones."""
        names = [n for n in agent.tools if n in self._tools and policy.has_permission(agent.permissions, self._tools[n].permission)]
        names += [n for n in run_tools if n in self._tools and n not in names]
        return [self._tools[n].spec() for n in names]

    async def invoke(self, ctx: ToolContext, call: ToolCall, *, run_tools: list[str], approved: bool = False) -> ToolOutcome:
        k = self.kernel
        tool = self._tools.get(call.name)
        if await k.killswitch.active():
            raise Halted("emergency stop is active")
        if not tool:
            return ToolOutcome("error", f"unknown tool {call.name!r}")

        # 1. Authorisation. Run-scoped tools are granted by the kernel for one run (e.g. plan_submit for a planning task).
        run_scoped = call.name in run_tools
        if not run_scoped:
            if call.name not in ctx.agent.tools or not policy.has_permission(ctx.agent.permissions, tool.permission):
                await k.audit.record(ctx.actor, f"tool.{call.name}", namespace_id=ctx.namespace_id, outcome="denied",
                                     detail={"reason": "missing permission", "required": tool.permission, "task_id": ctx.task_id})
                await k.events.emit("tool.denied", f"{ctx.agent.name} was denied {call.name} (requires {tool.permission})",
                                    namespace_id=ctx.namespace_id, actor=ctx.actor, subject=call.name, level="warn")
                return ToolOutcome("denied", f"Permission denied: this agent is not authorised to use {call.name}. "
                                             "Do not retry; escalate to your manager if the capability is needed.")

        # 2. Input validation (also catches arguments truncated by an output limit).
        if "__invalid_json__" in call.arguments:
            return ToolOutcome("error", "Tool arguments were not valid JSON. Re-issue the call with well-formed arguments.")
        try:
            jsonschema.validate(call.arguments, tool.input_schema)
        except jsonschema.ValidationError as e:
            return ToolOutcome("error", f"Invalid arguments for {call.name}: {e.message}")

        # 3. Autonomy policy.
        category = tool.category_for(call.arguments)
        if not approved and not run_scoped and policy.decide(ctx.agent.autonomy, category) == "approve":
            summary = f"{ctx.agent.name} ({ctx.agent.title or ctx.agent.role}) wants to run {call.name} [{category}]"
            apr = await k.approvals.request(namespace_id=ctx.namespace_id, action=call.name, category=category, summary=summary,
                                            payload={"arguments": _redact(call.arguments)}, agent_id=ctx.agent.id,
                                            run_id=ctx.run_id, task_id=ctx.task_id)
            await k.events.emit("approval.requested", summary, namespace_id=ctx.namespace_id, actor=ctx.actor, subject=apr.id,
                                level="warn", data={"approval_id": apr.id, "category": category, "tool": call.name})
            return ToolOutcome("needs_approval", approval_id=apr.id, data={"category": category})

        # 4. Execute.
        try:
            raw = await asyncio.wait_for(tool.handler(ctx, call.arguments), tool.timeout_s)
            content = raw if isinstance(raw, str) else json.dumps(raw, indent=1, default=str)
            outcome = ToolOutcome("ok", _clip(content))
        except Halted:
            raise
        except asyncio.TimeoutError:
            outcome = ToolOutcome("error", f"{call.name} timed out after {tool.timeout_s}s")
        except AiosError as e:
            outcome = ToolOutcome("error", str(e))
        except Exception as e:  # tool bugs surface to the agent as tool errors, never crash the run
            log.exception("tool %s failed", call.name)
            outcome = ToolOutcome("error", f"{type(e).__name__}: {e}")
        if category != "read" or outcome.is_error:
            await k.audit.record(ctx.actor, f"tool.{call.name}", namespace_id=ctx.namespace_id,
                                 outcome="ok" if outcome.status == "ok" else "error",
                                 detail={"category": category, "task_id": ctx.task_id, "approved_by_human": approved,
                                         "arguments": _redact(call.arguments)})
        await k.events.emit("tool.call", f"{ctx.agent.name} → {call.name}" + (" (failed)" if outcome.is_error else ""),
                            namespace_id=ctx.namespace_id, actor=ctx.actor, subject=call.name,
                            level="warn" if outcome.is_error else "info",
                            data={"task_id": ctx.task_id, "category": category, "arguments": _redact(call.arguments, 300)})
        return outcome


def _clip(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + f"\n... [truncated {len(text) - MAX_RESULT_CHARS} characters]"


def _redact(args: dict, limit: int = 2000) -> dict:
    """Shorten long values for logs and approval cards; arguments never contain secret values by design."""
    out = {}
    for key, v in args.items():
        s = v if isinstance(v, str) else json.dumps(v, default=str)
        if len(s) <= limit:
            out[key] = v if isinstance(v, (str, int, float, bool)) or v is None else s
        else:
            out[key] = s[:limit] + f"… (+{len(s) - limit} chars)"
    return out
