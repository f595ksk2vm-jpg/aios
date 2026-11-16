"""How agents work together: delegation, consultation, messaging, review, escalation, initiatives.

Every tool here is ordinary kernel-gated capability: it needs a permission,
is subject to the caller's autonomy level, and is audited.
"""
from __future__ import annotations

from sqlalchemy import func, select

from aios_kernel.errors import AiosError
from aios_kernel.models import Agent, Objective, Task
from aios_kernel.security import policy
from aios_kernel.tools.base import Tool, ToolContext, schema

MAX_DELEGATION_DEPTH = 3
MAX_CHILDREN_PER_TASK = 12
MAX_OPEN_AGENT_INITIATIVES = 3


class Collaboration:
    def __init__(self, kernel, factory):
        self.k = kernel
        self.factory = factory

    async def _resolve(self, ctx: ToolContext, ref: str) -> Agent | None:
        """Find a colleague by id, name or role among the agents usable in this namespace."""
        pool = await self.k.agents.usable(ctx.namespace_id)
        ref_l = ref.strip().lower()
        for a in pool:
            if a.id == ref or a.name.lower() == ref_l:
                return a
        found = await self.k.agents.find_for_role(ctx.namespace_id, ref)
        return found

    async def _subordinates(self, agent_id: str) -> set[str]:
        out, stack = set(), [agent_id]
        while stack:
            for r in await self.k.agents.reports(stack.pop()):
                if r.id not in out:
                    out.add(r.id)
                    stack.append(r.id)
        return out

    async def _depth(self, task_id: str) -> int:
        depth, cur = 0, task_id
        async with self.k.db.session() as s:
            while cur and depth < 10:
                t = await s.get(Task, cur)
                cur = t.parent_task_id if t else None
                if cur:
                    depth += 1
        return depth

    # --- tools ---------------------------------------------------------------------

    async def team_directory(self, ctx: ToolContext, a: dict) -> str:
        pool = await self.k.agents.usable(ctx.namespace_id)
        mine = await self._subordinates(ctx.agent.id)
        lines = []
        for ag in sorted(pool, key=lambda x: (x.department, x.title)):
            if ag.id == ctx.agent.id:
                continue
            rel = "reports to you" if ag.id in mine else ("your manager" if ag.id == ctx.agent.manager_id else "colleague")
            lines.append(f"- {ag.name} — {ag.title} [{ag.department or 'no department'}] (id {ag.id}, role {ag.role}; {rel})")
        return "\n".join(lines) or "(you are the only agent here)"

    async def agent_delegate(self, ctx: ToolContext, a: dict) -> str:
        k = self.k
        if not ctx.task_id:
            raise AiosError("delegation is only possible while working on a task")
        if await self._depth(ctx.task_id) >= MAX_DELEGATION_DEPTH:
            raise AiosError("delegation depth limit reached; do this work yourself or escalate")
        async with k.db.session() as s:
            n = (await s.execute(select(func.count()).select_from(Task).where(Task.parent_task_id == ctx.task_id))).scalar_one()
            parent = await s.get(Task, ctx.task_id)
        if n >= MAX_CHILDREN_PER_TASK:
            raise AiosError("too many sub-tasks for one task; consolidate the remaining work")
        target = await self._resolve(ctx, a["to"])
        subs = await self._subordinates(ctx.agent.id)
        if target and target.id not in subs:
            target = None if target.id == ctx.agent.id else target
            if target:
                raise AiosError(f"{target.name} does not report to you. Delegate within your own team, or use agent_ask / "
                                "agent_message to work with colleagues.")
        if not target:
            # Nobody on the team has this role yet: have the factory staff it, under the delegator.
            if not any(ns.meta.get("auto_provision") for ns in await k.namespaces.chain(ctx.namespace_id)):
                raise AiosError(f"no one on your team matches {a['to']!r} and this organization does not auto-provision agents")
            tpl = await self.factory.match_template(a["to"])
            if not tpl:
                raise AiosError(f"no one on your team matches {a['to']!r} and no role template fits it")
            org = await self.factory.org_namespace(ctx.namespace_id)
            target = await self.factory.create_from_template(
                org, tpl["key"], manager_id=ctx.agent.id, created_by="system:provisioner",
                autonomy=min(ctx.agent.autonomy, tpl.get("autonomy", "supervised"), key=policy.autonomy_rank))
        child = await k.orchestrator.create_task(
            namespace_id=ctx.namespace_id, objective_id=parent.objective_id, parent_task_id=ctx.task_id, title=a["title"],
            description=a["instructions"], agent_id=target.id, priority=parent.priority,
            meta={"delegated": True, "delegated_by": ctx.agent.id, "needs_review": bool(a.get("needs_review"))})
        await k.messaging.send(namespace_id=ctx.namespace_id, from_agent=ctx.agent.id, to_agent=target.id, kind="delegation",
                               subject=a["title"], body=a["instructions"][:2000], task_id=child.id)
        return (f"Delegated to {target.name} ({target.title}) as task {child.id}. It runs in parallel with you; "
                "its result will be delivered to you automatically before your task can close.")

    async def agent_ask(self, ctx: ToolContext, a: dict) -> str:
        k = self.k
        target = await self._resolve(ctx, a["to"])
        if not target or target.id == ctx.agent.id:
            raise AiosError(f"no colleague matches {a['to']!r}; use team_directory to see who is available")
        q = await k.messaging.send(namespace_id=ctx.namespace_id, from_agent=ctx.agent.id, to_agent=target.id, kind="question",
                                   subject=a["question"][:120], body=a["question"], task_id=ctx.task_id)
        answer = await k.runtime.consult(target, a["question"], asked_by=ctx.agent, namespace_id=ctx.namespace_id, task_id=ctx.task_id)
        r = await k.messaging.send(namespace_id=ctx.namespace_id, from_agent=target.id, to_agent=ctx.agent.id, kind="answer",
                                   subject=f"Re: {a['question'][:110]}", body=answer, task_id=ctx.task_id)
        # The exchange was handled synchronously; keep it out of both agents' next inbox.
        await k.messaging.mark_read([q.id, r.id])
        return f"{target.name} ({target.title}) answered:\n{answer}"

    async def agent_message(self, ctx: ToolContext, a: dict) -> str:
        target = await self._resolve(ctx, a["to"])
        if not target or target.id == ctx.agent.id:
            raise AiosError(f"no colleague matches {a['to']!r}")
        await self.k.messaging.send(namespace_id=ctx.namespace_id, from_agent=ctx.agent.id, to_agent=target.id,
                                    subject=a.get("subject", ""), body=a["body"], task_id=ctx.task_id)
        return f"Message delivered to {target.name}'s inbox; they will see it when they next start work."

    async def escalate(self, ctx: ToolContext, a: dict) -> str:
        k = self.k
        to = ctx.agent.manager_id
        await k.messaging.send(namespace_id=ctx.namespace_id, from_agent=ctx.agent.id, to_agent=to, kind="escalation",
                               subject=f"[{a.get('severity', 'medium')}] {a['summary'][:200]}", body=a.get("details", a["summary"]),
                               task_id=ctx.task_id)
        return ("Escalated to your manager." if to else "Escalated to the human operators (you have no manager).") + \
               " Continue with whatever is not blocked."

    async def review_request(self, ctx: ToolContext, a: dict) -> str:
        k = self.k
        if not ctx.task_id:
            raise AiosError("reviews can only be requested while working on a task")
        reviewer = await self._resolve(ctx, a["reviewer"]) if a.get("reviewer") else None
        if not reviewer and ctx.agent.manager_id:
            reviewer = await k.agents.get(ctx.agent.manager_id)
        if not reviewer or reviewer.id == ctx.agent.id:
            raise AiosError("no independent reviewer available; name one with `reviewer`")
        parent = await k.orchestrator.get_task(ctx.task_id)
        child = await k.orchestrator.create_task(
            namespace_id=ctx.namespace_id, objective_id=parent.objective_id, parent_task_id=ctx.task_id, kind="review",
            title=f"Review for {ctx.agent.name}: {a['subject'][:150]}", agent_id=reviewer.id, priority=max(1, parent.priority - 1),
            description=(f"{ctx.agent.name} ({ctx.agent.title}) asks you to review their work in progress. Inspect the actual "
                         "artefacts in the workspace where possible, challenge weak assumptions, then call `review_submit`.\n\n"
                         f"## What to review\n{a['subject']}\n\n## Notes from the author\n{a.get('notes', '(none)')}"),
            meta={"delegated": True, "delegated_by": ctx.agent.id,
                  "run_options": {"run_tools": ["review_submit"], "required_tool": "review_submit"}})
        return f"Review requested from {reviewer.name} (task {child.id}). The verdict will be delivered to you before your task can close."

    async def agent_create(self, ctx: ToolContext, a: dict) -> str:
        org = await self.factory.org_namespace(ctx.namespace_id)
        if org != ctx.agent.namespace_id and org not in await self.k.namespaces.descendants(ctx.agent.namespace_id):
            org = ctx.agent.namespace_id
        tpl = await self.factory.match_template(a["role"])
        if not tpl:
            raise AiosError(f"no role template matches {a['role']!r}")
        agent = await self.factory.create_from_template(org, tpl["key"], name=a.get("name"), creator=ctx.agent,
                                                        extra_prompt=a.get("focus", ""))
        return (f"Created {agent.name}, {agent.title} (id {agent.id}), reporting to "
                f"{'you' if agent.manager_id == ctx.agent.id else 'their functional manager'}. "
                f"Autonomy {agent.autonomy}; permissions limited to what you hold yourself.")

    async def initiative_propose(self, ctx: ToolContext, a: dict) -> str:
        k = self.k
        async with k.db.session() as s:
            open_n = (await s.execute(select(func.count()).select_from(Objective).where(
                Objective.namespace_id == ctx.namespace_id, Objective.created_by.like("agent:%"),
                Objective.status.in_(["planning", "running", "finalizing", "needs_attention"])))).scalar_one()
        if open_n >= MAX_OPEN_AGENT_INITIATIVES:
            raise AiosError("this organization already has the maximum number of agent-initiated objectives in flight")
        obj = await k.orchestrator.create_objective(
            namespace_id=ctx.namespace_id, title=a["title"], created_by=ctx.actor, owner_agent_id=ctx.agent.id,
            description=f"{a['description']}\n\nRationale: {a['rationale']}\nExpected impact: {a.get('expected_impact', 'not stated')}",
            meta={"initiative": True, "proposed_by": ctx.agent.id})
        return f"Initiative launched as objective {obj.id}; planning has started."

    def register(self, registry) -> None:
        st = {"type": "string"}

        def add(name, desc, props, required, handler, perm, category, **kw):
            registry.register(Tool(name, desc, schema(props, required), handler, perm, category, group="collaboration",
                                   layer="workforce", **kw))

        add("team_directory", "List the colleagues you can work with: names, roles, ids and how they relate to you.",
            {}, [], self.team_directory, "collab.directory", "read")
        add("agent_delegate",
            "Hand a piece of work to someone who reports to you (by id, name or role). It becomes their task and runs in "
            "parallel with yours; you receive the result automatically. If nobody on your team has the role, a suitable "
            "agent is hired when the organization allows it. Write instructions that stand on their own.",
            {"to": st, "title": st, "instructions": st, "needs_review": {"type": "boolean"}}, ["to", "title", "instructions"],
            self.agent_delegate, "collab.delegate", "write_internal")
        add("agent_ask", "Ask a colleague a question and get their answer now. Use it for expertise you lack, a second "
            "opinion, or to have an assumption challenged.",
            {"to": st, "question": st}, ["to", "question"], self.agent_ask, "collab.ask", "read", timeout_s=900)
        add("agent_message", "Leave a message in a colleague's inbox (no immediate reply).",
            {"to": st, "subject": st, "body": st}, ["to", "body"], self.agent_message, "collab.message", "write_internal")
        add("escalate", "Raise a problem you cannot resolve to your manager (or to the human operators if you have none): "
            "a blocker, a risk, a decision above your authority.",
            {"summary": st, "details": st, "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]}},
            ["summary"], self.escalate, "collab.escalate", "read")
        add("review_request", "Ask a colleague (default: your manager) to review your work before you finish.",
            {"subject": st, "notes": st, "reviewer": st}, ["subject"], self.review_request, "collab.review", "write_internal")
        add("agent_create", "Add a new agent to your organization from a role template. The new agent can never hold more "
            "permissions or autonomy than you do.",
            {"role": st, "name": st, "focus": st}, ["role"], self.agent_create, "agents.create", "agent_mgmt")
        add("initiative_propose", "Propose a new initiative you believe the organization should pursue. Once authorised it "
            "becomes an objective that you own.",
            {"title": st, "description": st, "rationale": st, "expected_impact": st}, ["title", "description", "rationale"],
            self.initiative_propose, "strategy.propose", "initiative")
