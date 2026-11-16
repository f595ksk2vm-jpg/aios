"""The agent run loop.

A *run* is one agent working one task: call a model, execute the tool calls it
asks for through the gateway, feed back results, repeat until it answers.

Runs are durable state machines. The transcript is persisted after every
step, so a run can be suspended (human approval pending, delegated work in
flight, emergency stop) and resumed later by any worker, in any location.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select

from ..db import new_id, utcnow
from ..errors import AiosError, BudgetExceeded, Halted
from ..llm.types import ProviderError, ToolCall, tool_results_message, user_message
from ..models import Agent, Approval, Namespace, Objective, Run, Task
from ..tools.base import ToolContext

log = logging.getLogger("aios.runtime")

TERMINAL_TASK = {"done", "failed", "cancelled", "blocked"}

OPERATING_RULES = """\
# How you work here
- You are part of an AI workforce running on AI-OS. Your tools are your hands: do the work with them rather than describing what could be done.
- File tools and code execution operate on a shared workspace for this {ns_kind}. Deliverables are real files there; verify what you build by running it.
- Some actions pause for human approval. When that happens you will receive the decision as the tool's result. A denial or rejection is final for that action: do not look for another route to the same effect.
- You have exactly the permissions you were given. If a task needs a capability you lack, say so or escalate; never try to obtain it yourself.
- When you finish, reply with a short report for your manager: what you did, where the outputs are (paths), and anything unresolved. That reply is recorded as the task result and passed to dependent tasks."""


@dataclass
class RunOutcome:
    status: str  # completed | suspended | failed
    result: str = ""
    error: str = ""
    reason: str = ""  # for suspended: approval | children | halted
    retryable: bool = False


class AgentRuntime:
    def __init__(self, kernel):
        self.k = kernel

    # --- prompt assembly -----------------------------------------------------------

    async def system_prompt(self, agent: Agent, ns: Namespace) -> str:
        k = self.k
        lines = [agent.system_prompt.strip(), "", "# Your position",
                 f"You are {agent.name}, {agent.title or agent.role}"
                 + (f" in the {agent.department} department" if agent.department else "") + f' of "{ns.name}".']
        if agent.manager_id:
            try:
                m = await k.agents.get(agent.manager_id)
                lines.append(f"You report to {m.name} ({m.title or m.role}).")
            except AiosError:
                pass
        reports = await k.agents.reports(agent.id)
        if reports:
            lines.append("Your direct reports: " + "; ".join(f"{r.name} — {r.title or r.role} (id {r.id})" for r in reports) + ".")
        lines.append(f"Autonomy level: {agent.autonomy}.")
        lines += ["", OPERATING_RULES.format(ns_kind=ns.kind)]
        return "\n".join(lines).strip()

    async def task_brief(self, task: Task, agent: Agent) -> str:
        k = self.k
        parts = [f"# Task: {task.title}", task.description.strip() or "(no further description)"]
        async with k.db.session() as s:
            if task.objective_id:
                obj = await s.get(Objective, task.objective_id)
                if obj and task.kind != "plan":
                    parts.append(f"## Objective this contributes to\n{obj.title}\n{obj.description.strip()[:3000]}")
            deps = []
            if task.depends_on:
                deps = list((await s.execute(select(Task).where(Task.id.in_(task.depends_on)))).scalars())
        if deps:
            parts.append("## Results of prerequisite tasks\n" + "\n\n".join(
                f"### {d.title}\n{(d.result or '(no result recorded)')[:6000]}" for d in deps))
        if task.meta.get("feedback"):
            parts.append(f"## Feedback on your previous attempt\n{task.meta['feedback']}")
        try:
            hits = await k.memory.search(f"{task.title}\n{task.description[:500]}", namespace_id=task.namespace_id, agent_id=agent.id, k=5)
        except Exception:  # recall is best-effort; an embedder outage must not block work
            log.exception("memory recall failed")
            hits = []
        if hits:
            parts.append("## Possibly relevant memory\n" + "\n\n".join(f"- {h['title'] or h['kind']}: {h['content'][:700]}" for h in hits))
        inbox = await k.messaging.take_unread(agent.id)
        if inbox:
            names = {a.id: a.name for a in await k.agents.list()}
            parts.append("## Unread messages\n" + "\n".join(
                f"- [{m.kind}] from {names.get(m.from_agent, 'the system') if m.from_agent else 'the system'}: "
                f"{m.subject + ' — ' if m.subject else ''}{m.body[:1500]}" for m in inbox))
        return "\n\n".join(parts)

    # --- lifecycle -----------------------------------------------------------------

    async def start(self, task: Task, agent: Agent, options: dict | None = None) -> Run:
        brief = await self.task_brief(task, agent)
        run = Run(id=new_id("run"), task_id=task.id, agent_id=agent.id, namespace_id=task.namespace_id,
                  messages=[user_message(brief)], options=options or {})
        async with self.k.db.session() as s:
            s.add(run)
        return run

    async def _save(self, run: Run, **fields) -> None:
        async with self.k.db.session() as s:
            row = await s.get(Run, run.id)
            for key, v in fields.items():
                setattr(row, key, v)
                setattr(run, key, v)

    async def _guard(self, agent_id: str, namespace_id: str, task_id: str | None = None) -> None:
        k = self.k
        if task_id:
            async with k.db.session() as s:
                row = await s.get(Task, task_id)
            if row and row.status == "cancelled":
                raise AiosError("task was cancelled")
        if await k.killswitch.active():
            raise Halted("emergency stop is active")
        if await k.namespaces.is_paused(namespace_id):
            raise Halted("namespace is paused")
        agent = await k.agents.get(agent_id)
        if agent.status != "active":
            raise Halted(f"agent is {agent.status}")

    async def execute(self, run_id: str) -> RunOutcome:
        """Advance a run until it completes, fails or has to wait. Never raises for expected conditions."""
        k = self.k
        async with k.db.session() as s:
            run = await s.get(Run, run_id)
            task = await s.get(Task, run.task_id) if run else None
        if not run or not task:
            return RunOutcome("failed", error="run or task no longer exists")
        if run.status in {"completed", "failed", "cancelled"}:
            return RunOutcome(run.status if run.status != "cancelled" else "failed", result=task.result, error=run.error)
        try:
            agent = await k.agents.get(run.agent_id)
            return await self._loop(run, task, agent)
        except Halted as e:
            pending = run.pending if run.pending.get("type") == "tools" else {"type": "halted"}
            await self._save(run, status="suspended", pending={**pending, "reason": "halted", "detail": str(e)})
            return RunOutcome("suspended", reason="halted")
        except BudgetExceeded as e:
            await self._save(run, status="failed", error=str(e), finished_at=utcnow())
            return RunOutcome("failed", error=str(e))
        except ProviderError as e:
            await self._save(run, status="failed", error=str(e), finished_at=utcnow())
            return RunOutcome("failed", error=str(e), retryable=e.retryable)
        except AiosError as e:
            await self._save(run, status="failed", error=str(e), finished_at=utcnow())
            return RunOutcome("failed", error=str(e))
        except Exception as e:
            log.exception("run %s crashed", run_id)
            await self._save(run, status="failed", error=f"{type(e).__name__}: {e}", finished_at=utcnow())
            return RunOutcome("failed", error=f"internal error: {type(e).__name__}: {e}", retryable=True)

    async def _loop(self, run: Run, task: Task, agent: Agent) -> RunOutcome:
        k = self.k
        opts = run.options or {}
        run_tools: list[str] = list(opts.get("run_tools", []))
        required: str | None = opts.get("required_tool")
        max_steps = int(opts.get("max_steps") or k.settings.max_run_steps)
        ns = await k.namespaces.get(task.namespace_id)
        chain = await k.namespaces.chain_ids(task.namespace_id)
        _, models_placement = await k.namespaces.placement(task.namespace_id)
        ctx = ToolContext(kernel=k, agent=agent, namespace_id=task.namespace_id, workspace=k.workspace_for(task.namespace_id),
                          run_id=run.id, task_id=task.id)
        messages: list[dict] = list(run.messages)
        steps, nudges = run.steps, 0
        system = await self.system_prompt(agent, ns)
        specs = k.tools.specs_for(agent, run_tools)
        await self._save(run, status="running")

        # Pick up where a suspended run left off.
        pending = dict(run.pending or {})
        if pending.get("type") == "tools":
            done = await self._finish_tool_turn(run, ctx, messages, pending, run_tools)
            if isinstance(done, RunOutcome):
                return done
        elif pending.get("type") == "children":
            gate = await self._children_gate(task, messages)
            if gate == "wait":
                await self._save(run, status="suspended")
                return RunOutcome("suspended", reason="children")
            if gate == "none":
                return await self._complete(run, messages, self._last_text(messages))
            await self._save(run, messages=messages, pending={})
        elif pending:
            await self._save(run, pending={})

        while True:
            await self._guard(agent.id, task.namespace_id, task.id)
            if steps >= max_steps:
                raise AiosError(f"run exceeded its step limit ({max_steps}) without finishing")
            await k.budget.check(task.namespace_id, agent.id)

            candidates = await k.models.candidates(agent.model, models_placement=models_placement, needs_tools=bool(specs))
            result, cand, latency = await k.models.chat(candidates, system=system, messages=messages, tools=specs,
                                                        namespace_chain=chain, effort=(agent.model or {}).get("effort"))
            cost = k.models.cost(cand.entry, result.usage.input_tokens, result.usage.output_tokens)
            await k.budget.record(namespace_id=task.namespace_id, agent_id=agent.id, task_id=task.id, run_id=run.id, kind="llm",
                                  provider=cand.provider.name, model=cand.ref, input_tokens=result.usage.input_tokens,
                                  output_tokens=result.usage.output_tokens, cost_usd=cost, latency_ms=latency)
            messages.append(result.to_message())
            steps += 1
            await self._save(run, messages=messages, steps=steps, model=cand.ref,
                             input_tokens=run.input_tokens + result.usage.input_tokens,
                             output_tokens=run.output_tokens + result.usage.output_tokens, cost_usd=run.cost_usd + cost)
            if result.text.strip():
                await k.events.emit("agent.thought", result.text.strip()[:600], namespace_id=task.namespace_id,
                                    actor=f"agent:{agent.id}", subject=task.id, data={"task_id": task.id, "run_id": run.id})

            if result.stop_reason == "max_tokens":
                # The turn was cut off: any tool input may be incomplete, so nothing from it is executed.
                if result.tool_calls:
                    messages.append(tool_results_message([
                        {"tool_call_id": c.id, "name": c.name, "is_error": True,
                         "content": "Not executed: your response hit the output limit, so this call may be incomplete. "
                                    "Re-issue it, splitting large content across several smaller calls."}
                        for c in result.tool_calls]))
                else:
                    messages.append(user_message("Your response was cut off by the output limit. Continue from where you stopped, more concisely."))
                await self._save(run, messages=messages)
                continue

            if result.tool_calls:
                pending = {"type": "tools", "results": [], "calls": [{"call": c.to_dict(), "approval_id": None} for c in result.tool_calls]}
                done = await self._finish_tool_turn(run, ctx, messages, pending, run_tools)
                if isinstance(done, RunOutcome):
                    return done
                continue

            if result.stop_reason == "pause":
                continue

            # The model answered without calling a tool: it considers the task finished.
            if required and not self._called(messages, required):
                nudges += 1
                if nudges > 2:
                    raise AiosError(f"agent finished without calling the required tool {required!r}")
                messages.append(user_message(f"You have not called `{required}` yet. This task is only complete once you call it."))
                await self._save(run, messages=messages)
                continue
            gate = await self._children_gate(task, messages)
            if gate == "wait":
                await self._save(run, status="suspended", pending={"type": "children"}, messages=messages)
                return RunOutcome("suspended", reason="children")
            if gate == "injected":
                await self._save(run, messages=messages)
                continue
            return await self._complete(run, messages, result.text)

    # --- helpers -------------------------------------------------------------------

    async def _finish_tool_turn(self, run: Run, ctx: ToolContext, messages: list[dict], pending: dict, run_tools: list[str]):
        """Execute (or keep waiting on) the tool calls of the current assistant turn.

        Returns a RunOutcome if the run must stop here, otherwise None after
        appending the tool results to ``messages``.
        """
        k = self.k
        results: list[dict] = list(pending.get("results", []))
        waiting: list[dict] = []
        terminal: str | None = None
        calls = pending.get("calls", [])
        for i, item in enumerate(calls):
            call = ToolCall(**item["call"])
            approved = False
            if item.get("approval_id"):
                async with k.db.session() as s:
                    apr = await s.get(Approval, item["approval_id"])
                if not apr or apr.status == "pending":
                    waiting.append(item)
                    continue
                if apr.status == "rejected":
                    results.append({"tool_call_id": call.id, "name": call.name, "is_error": True,
                                    "content": "A human reviewer rejected this action"
                                               + (f": {apr.reason}" if apr.reason else ".") + " Do not attempt it another way."})
                    continue
                approved = True
            try:
                outcome = await k.tools.invoke(ctx, call, run_tools=run_tools, approved=approved)
            except Halted:
                # Keep exact progress so nothing is skipped or repeated on resume.
                run.pending = {"type": "tools", "results": results, "calls": waiting + calls[i:]}
                raise
            if outcome.status == "needs_approval":
                waiting.append({"call": item["call"], "approval_id": outcome.approval_id})
                continue
            results.append({"tool_call_id": call.id, "name": call.name, "content": outcome.content, "is_error": outcome.is_error})
            tool = k.tools.get(call.name)
            if tool and tool.terminal and outcome.status == "ok":
                terminal = outcome.content
        if waiting:
            await self._save(run, status="suspended", pending={"type": "tools", "results": results, "calls": waiting, "reason": "approval"})
            return RunOutcome("suspended", reason="approval")
        # Results go back in the order the calls were made.
        order = {c["call"]["id"]: n for n, c in enumerate(self._all_calls(messages))}
        results.sort(key=lambda r: order.get(r["tool_call_id"], 0))
        messages.append(tool_results_message(results))
        await self._save(run, messages=messages, pending={})
        if terminal is not None:
            return await self._complete(run, messages, terminal)
        return None

    @staticmethod
    def _all_calls(messages: list[dict]) -> list[dict]:
        for m in reversed(messages):
            if m["role"] == "assistant":
                return [{"call": c} for c in m.get("tool_calls", [])]
        return []

    @staticmethod
    def _called(messages: list[dict], tool_name: str) -> bool:
        return any(r["name"] == tool_name and not r.get("is_error") for m in messages if m["role"] == "tool" for r in m["results"])

    @staticmethod
    def _last_text(messages: list[dict]) -> str:
        for m in reversed(messages):
            if m["role"] == "assistant" and m.get("content"):
                return m["content"]
        return ""

    async def _children_gate(self, task: Task, messages: list[dict]) -> str:
        """Delegated sub-tasks must finish before their parent can.

        ``wait``: some child is still in flight. ``injected``: all children are
        finished and their results were appended for the agent to integrate.
        ``none``: nothing outstanding.
        """
        async with self.k.db.session() as s:
            children = list((await s.execute(select(Task).where(Task.parent_task_id == task.id))).scalars())
            fresh = [c for c in children if c.meta.get("delegated") and not c.meta.get("reported_to_parent")]
            if not fresh:
                return "none"
            if any(c.status not in TERMINAL_TASK for c in fresh):
                return "wait"
            for c in fresh:
                c.meta = {**c.meta, "reported_to_parent": True}
        blocks = []
        for c in fresh:
            body = c.result if c.status == "done" else f"FAILED: {c.error or c.status}"
            blocks.append(f"### {c.title} — {c.status}\n{body[:6000]}")
        messages.append(user_message("Work you delegated has finished. Results:\n\n" + "\n\n".join(blocks)
                                     + "\n\nIntegrate these and complete your task (or delegate further if something is still missing)."))
        return "injected"

    async def _complete(self, run: Run, messages: list[dict], result: str) -> RunOutcome:
        await self._save(run, status="completed", messages=messages, pending={}, finished_at=utcnow())
        return RunOutcome("completed", result=result.strip() or "(the agent produced no final report)")

    async def consult(self, agent: Agent, question: str, *, asked_by: Agent, namespace_id: str, task_id: str | None = None) -> str:
        """One-shot question to another agent (no tools): the basis for asking for help and challenging assumptions."""
        k = self.k
        await self._guard(agent.id, namespace_id)
        await k.budget.check(namespace_id, agent.id)
        ns = await k.namespaces.get(agent.namespace_id)
        _, placement = await k.namespaces.placement(namespace_id)
        hits = await k.memory.search(question, namespace_id=namespace_id, agent_id=agent.id, k=4)
        context = ("\n\nPossibly relevant memory:\n" + "\n".join(f"- {h['content'][:500]}" for h in hits)) if hits else ""
        prompt = (f"{asked_by.name} ({asked_by.title or asked_by.role}) asks for your input:\n\n{question}{context}\n\n"
                  "Answer from your own expertise. If you disagree with a premise, say so and explain why.")
        candidates = await k.models.candidates(agent.model, models_placement=placement, needs_tools=False)
        result, cand, latency = await k.models.chat(candidates, system=await self.system_prompt(agent, ns), messages=[user_message(prompt)],
                                                    tools=[], namespace_chain=await k.namespaces.chain_ids(namespace_id), max_tokens=4000)
        await k.budget.record(namespace_id=namespace_id, agent_id=agent.id, task_id=task_id, kind="llm", provider=cand.provider.name,
                              model=cand.ref, input_tokens=result.usage.input_tokens, output_tokens=result.usage.output_tokens,
                              cost_usd=k.models.cost(cand.entry, result.usage.input_tokens, result.usage.output_tokens), latency_ms=latency)
        return result.text.strip()
