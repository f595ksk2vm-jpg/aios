"""Central orchestration: objective → plan → task graph → parallel execution → evaluation.

The orchestrator owns the task state machine. Agents influence it only through
a handful of structured tools (``plan_submit``, ``review_submit``,
``objective_complete``) whose inputs are validated here.
"""
from __future__ import annotations

import logging

from sqlalchemy import select, update

from ..db import new_id, utcnow
from ..errors import AiosError, Conflict, NotFound
from ..models import Agent, Objective, Run, Task
from ..tools.base import Tool, ToolContext, schema
from ..agents.runtime import TERMINAL_TASK, RunOutcome

log = logging.getLogger("aios.orchestrator")
MAX_PLAN_TASKS = 30

PLANNER_BRIEF = """\
Plan the work needed to achieve the objective below, then call `plan_submit` exactly once.

Objective: {title}
{description}

## Who is available
{roster}

## Planning guidance
- Break the objective into concrete tasks, each small enough for one agent to finish and each producing something verifiable (a file, a tested program, a decision record).
- Give every task a self-contained description: the assignee sees only that description, the objective, and the results of the tasks it depends on.
- Use `depends_on` only for real prerequisites. Tasks without dependencies run in parallel.
- Assign by `agent_id` when a suitable agent is listed, otherwise by `role`{provision_note}.
- Set `needs_review` on work where a second pair of eyes materially reduces risk.
- Prefer the smallest plan that fully achieves the objective."""

FINALIZE_BRIEF = """\
All planned tasks for the objective "{title}" have finished. Their results are below.

Evaluate honestly whether the objective was achieved, then call `objective_complete` with:
- a summary a stakeholder can act on (what was delivered and where to find it),
- whether the objective was met, and a 0–1 score,
- lessons worth remembering for similar work, and any follow-up work you recommend.

{results}"""


class Orchestrator:
    def __init__(self, kernel):
        self.k = kernel
        # Layer 2 installs a provisioner: async (namespace_id, role, requested_by: Agent) -> Agent | None
        self.provisioner = None
        self.retry_delay_s = 5.0

    # --- queries -------------------------------------------------------------------

    async def get_task(self, task_id: str) -> Task:
        async with self.k.db.session() as s:
            t = await s.get(Task, task_id)
        if not t:
            raise NotFound(f"task {task_id} not found")
        return t

    async def get_objective(self, objective_id: str) -> Objective:
        async with self.k.db.session() as s:
            o = await s.get(Objective, objective_id)
        if not o:
            raise NotFound(f"objective {objective_id} not found")
        return o

    async def list_objectives(self, namespace_ids: list[str] | None = None, limit: int = 200) -> list[Objective]:
        q = select(Objective).order_by(Objective.created_at.desc()).limit(limit)
        if namespace_ids is not None:
            q = q.where(Objective.namespace_id.in_(namespace_ids))
        async with self.k.db.session() as s:
            return list((await s.execute(q)).scalars())

    async def list_tasks(self, *, objective_id: str | None = None, namespace_ids: list[str] | None = None,
                         agent_id: str | None = None, statuses: list[str] | None = None, limit: int = 500) -> list[Task]:
        q = select(Task).order_by(Task.created_at.desc()).limit(limit)
        if objective_id:
            q = q.where(Task.objective_id == objective_id)
        if namespace_ids is not None:
            q = q.where(Task.namespace_id.in_(namespace_ids))
        if agent_id:
            q = q.where(Task.assigned_agent_id == agent_id)
        if statuses:
            q = q.where(Task.status.in_(statuses))
        async with self.k.db.session() as s:
            return list((await s.execute(q)).scalars())

    async def runs_for(self, task_id: str) -> list[Run]:
        async with self.k.db.session() as s:
            return list((await s.execute(select(Run).where(Run.task_id == task_id).order_by(Run.started_at))).scalars())

    # --- creation ------------------------------------------------------------------

    async def create_objective(self, *, namespace_id: str, title: str, description: str = "", created_by: str = "system",
                               owner_agent_id: str | None = None, meta: dict | None = None) -> Objective:
        k = self.k
        await k.namespaces.get(namespace_id)
        owner = await k.agents.get(owner_agent_id) if owner_agent_id else await k.agents.lead(namespace_id)
        if not owner:
            raise Conflict("no active agent is available to own this objective")
        obj = Objective(id=new_id("obj"), namespace_id=namespace_id, title=title, description=description,
                        owner_agent_id=owner.id, created_by=created_by, meta=meta or {})
        async with k.db.session() as s:
            s.add(obj)
        await k.events.emit("objective.created", title, namespace_id=namespace_id, actor=created_by, subject=obj.id,
                            data={"objective_id": obj.id, "owner": owner.id})
        roster = await self._roster(namespace_id)
        note = ", and a suitable agent will be created if none exists" if self.provisioner else ""
        await self.create_task(
            namespace_id=namespace_id, objective_id=obj.id, kind="plan", title=f"Plan: {title}",
            description=PLANNER_BRIEF.format(title=title, description=description, roster=roster, provision_note=note),
            agent_id=owner.id, priority=2, meta={"run_options": {"run_tools": ["plan_submit"], "required_tool": "plan_submit"}})
        return obj

    async def _roster(self, namespace_id: str) -> str:
        agents = await self.k.agents.usable(namespace_id)
        if not agents:
            return "(no agents yet)"
        return "\n".join(f"- {a.name} — {a.title or a.role} (role `{a.role}`, id `{a.id}`"
                         + (f", {a.department}" if a.department else "") + ")" for a in agents[:80])

    async def create_task(self, *, namespace_id: str, title: str, description: str = "", objective_id: str | None = None,
                          parent_task_id: str | None = None, kind: str = "work", agent_id: str | None = None, role: str = "",
                          depends_on: list[str] | None = None, priority: int = 5, meta: dict | None = None,
                          max_attempts: int = 2, enqueue: bool = True) -> Task:
        depends_on = list(depends_on or [])
        t = Task(id=new_id("tsk"), namespace_id=namespace_id, objective_id=objective_id, parent_task_id=parent_task_id, kind=kind,
                 title=title[:255], description=description, assigned_agent_id=agent_id, required_role=role, depends_on=depends_on,
                 priority=priority, meta=meta or {}, max_attempts=max_attempts, status="pending" if depends_on else "ready")
        async with self.k.db.session() as s:
            s.add(t)
        await self.k.events.emit("task.created", t.title, namespace_id=namespace_id, subject=t.id,
                                 data={"task_id": t.id, "objective_id": objective_id, "agent_id": agent_id, "kind": kind})
        if enqueue and t.status == "ready":
            await self._enqueue(t)
        return t

    async def _enqueue(self, task: Task, delay_s: float = 0) -> None:
        k = self.k
        compute, _ = await k.namespaces.placement(task.namespace_id)
        affinity = compute
        if affinity == "any" and task.assigned_agent_id:
            try:
                affinity = (await k.agents.get(task.assigned_agent_id)).exec_env
            except NotFound:
                pass
        await k.queue.enqueue("task.run", {"task_id": task.id}, priority=task.priority, affinity=affinity, delay_s=delay_s)

    async def _resume_job(self, run: Run, task: Task) -> None:
        compute, _ = await self.k.namespaces.placement(task.namespace_id)
        affinity = compute
        if affinity == "any":
            try:
                affinity = (await self.k.agents.get(run.agent_id)).exec_env
            except NotFound:
                pass
        await self.k.queue.enqueue("run.resume", {"run_id": run.id}, priority=max(1, task.priority - 1), affinity=affinity)

    # --- execution (job handlers) ----------------------------------------------------

    async def handle_task_run(self, payload: dict) -> None:
        k = self.k
        async with k.db.session() as s:
            # Compare-and-set so a duplicate job cannot start the same task twice.
            res = await s.execute(update(Task).where(Task.id == payload["task_id"], Task.status == "ready")
                                  .values(status="running", started_at=utcnow(), attempts=Task.attempts + 1))
            task = await s.get(Task, payload["task_id"])
        if not task:
            return
        if res.rowcount != 1:
            # Redelivery after a worker crash: the task is still marked running and its run is persisted — continue it.
            if payload.get("_attempt", 1) > 1 and task.status == "running":
                async with k.db.session() as s:
                    run = (await s.execute(select(Run).where(Run.task_id == task.id, Run.status == "running")
                                           .order_by(Run.started_at.desc()))).scalars().first()
                if run:
                    await self._after_run(task, run, await k.runtime.execute(run.id))
            return
        try:
            agent = await self._assignee(task)
        except AiosError as e:
            await self._fail(task, str(e), retryable=False)
            return
        await k.events.emit("task.started", f"{agent.name} started: {task.title}", namespace_id=task.namespace_id,
                            actor=f"agent:{agent.id}", subject=task.id, data={"task_id": task.id, "agent_id": agent.id})
        run = await k.runtime.start(task, agent, task.meta.get("run_options"))
        await self._after_run(task, run, await k.runtime.execute(run.id))

    async def handle_run_resume(self, payload: dict) -> None:
        k = self.k
        async with k.db.session() as s:
            res = await s.execute(update(Run).where(Run.id == payload["run_id"], Run.status == "suspended").values(status="running"))
            run = await s.get(Run, payload["run_id"])
            task = await s.get(Task, run.task_id) if run else None
            if run and task and res.rowcount == 1 and task.status not in TERMINAL_TASK:
                task.status = "running"
        if not run or not task:
            return
        if res.rowcount != 1 and not (payload.get("_attempt", 1) > 1 and run.status == "running"):
            return
        if task.status in TERMINAL_TASK:  # cancelled while suspended
            await k.runtime._save(run, status="cancelled", finished_at=utcnow())
            return
        await self._after_run(task, run, await k.runtime.execute(run.id))

    async def _assignee(self, task: Task) -> Agent:
        k = self.k
        if task.assigned_agent_id:
            agent = await k.agents.get(task.assigned_agent_id)
        else:
            agent = await k.agents.find_for_role(task.namespace_id, task.required_role) or await k.agents.lead(task.namespace_id)
            if not agent:
                raise AiosError(f"no agent available for role {task.required_role!r}")
            async with k.db.session() as s:
                (await s.get(Task, task.id)).assigned_agent_id = agent.id
            task.assigned_agent_id = agent.id
        if agent.status == "deactivated":
            raise AiosError(f"assigned agent {agent.name} is deactivated")
        return agent

    async def _after_run(self, task: Task, run: Run, outcome: RunOutcome) -> None:
        if outcome.status == "completed":
            await self._complete(task, outcome.result)
        elif outcome.status == "suspended":
            status = {"approval": "awaiting_approval", "children": "waiting", "halted": "paused"}.get(outcome.reason, "waiting")
            async with self.k.db.session() as s:
                await s.execute(update(Task).where(Task.id == task.id, Task.status == "running").values(status=status))
            await self.k.events.emit("task.suspended", f"{task.title} — {status.replace('_', ' ')}", namespace_id=task.namespace_id,
                                     subject=task.id, data={"task_id": task.id, "reason": outcome.reason})
            if outcome.reason == "children":
                await self._wake_parent_if_ready(task.id)  # children may have finished while we were suspending
        else:
            await self._fail(task, outcome.error or "run failed", retryable=outcome.retryable)

    # --- state transitions -----------------------------------------------------------

    async def _complete(self, task: Task, result: str) -> None:
        k = self.k
        needs_review = task.kind == "work" and task.meta.get("needs_review") and not task.meta.get("review_passed")
        async with k.db.session() as s:
            row = await s.get(Task, task.id)
            if row.status in TERMINAL_TASK:
                return
            row.result = result
            if needs_review:
                row.status = "review"
            else:
                row.status, row.finished_at = "done", utcnow()
        if needs_review:
            await self._start_review(task, result)
            return
        agent = await k.agents.get(task.assigned_agent_id) if task.assigned_agent_id else None
        await k.events.emit("task.completed", f"{agent.name if agent else 'agent'} completed: {task.title}",
                            namespace_id=task.namespace_id, actor=f"agent:{agent.id}" if agent else "system", subject=task.id,
                            data={"task_id": task.id, "objective_id": task.objective_id, "agent_id": task.assigned_agent_id,
                                  "kind": task.kind, "result": result[:2000]})
        if task.kind == "work":
            # Execution history becomes searchable organizational memory.
            try:
                await k.memory.add(namespace_id=task.namespace_id, title=f"Result: {task.title}", content=result[:8000], kind="result",
                                   agent_id=task.assigned_agent_id, meta={"task_id": task.id, "objective_id": task.objective_id})
            except Exception:
                log.exception("could not store task result in memory")
            if agent and agent.manager_id and not task.parent_task_id:
                await k.messaging.send(namespace_id=task.namespace_id, from_agent=agent.id, to_agent=agent.manager_id, kind="report",
                                       subject=f"Completed: {task.title}", body=result[:3000], task_id=task.id)
        if task.kind == "review":
            await self._on_review_done(task)
        if task.kind == "finalize" and task.objective_id:
            await self._close_objective(task.objective_id, f"agent:{agent.id}" if agent else "system")
        if task.parent_task_id:
            await self._wake_parent_if_ready(task.parent_task_id)
        if task.objective_id:
            await self.advance(task.objective_id)

    async def _fail(self, task: Task, error: str, *, retryable: bool) -> None:
        k = self.k
        async with k.db.session() as s:
            row = await s.get(Task, task.id)
            if row.status in TERMINAL_TASK:
                return
            retry = retryable and row.attempts < row.max_attempts
            if retry:
                row.status, row.error = "ready", error
            else:
                row.status, row.error, row.finished_at = "failed", error, utcnow()
        if retry:
            await k.events.emit("task.retry", f"{task.title}: {error[:300]}", namespace_id=task.namespace_id, subject=task.id,
                                level="warn", data={"task_id": task.id, "error": error[:1000]})
            await self._enqueue(task, delay_s=self.retry_delay_s)
            return
        await k.events.emit("task.failed", f"{task.title}: {error[:400]}", namespace_id=task.namespace_id, subject=task.id,
                            level="error", data={"task_id": task.id, "objective_id": task.objective_id, "error": error[:2000],
                                                 "agent_id": task.assigned_agent_id})
        # Escalate up the hierarchy; with no manager it reaches the humans' notification feed.
        manager_id = None
        if task.assigned_agent_id:
            try:
                manager_id = (await k.agents.get(task.assigned_agent_id)).manager_id
            except NotFound:
                pass
        await k.messaging.send(namespace_id=task.namespace_id, from_agent=task.assigned_agent_id, to_agent=manager_id, kind="escalation",
                               subject=f"Task failed: {task.title}", body=error[:3000], task_id=task.id)
        if task.kind == "review":
            await self._on_review_done(task)
        if task.parent_task_id:
            await self._wake_parent_if_ready(task.parent_task_id)
        if task.objective_id and not task.parent_task_id:
            async with k.db.session() as s:
                # Everything downstream of a failed task cannot proceed.
                tasks = list((await s.execute(select(Task).where(Task.objective_id == task.objective_id))).scalars())
                dead = {task.id}
                changed = True
                while changed:
                    changed = False
                    for t in tasks:
                        if t.id not in dead and t.status == "pending" and dead & set(t.depends_on):
                            t.status, t.error = "blocked", "a prerequisite task failed"
                            dead.add(t.id)
                            changed = True
                obj = await s.get(Objective, task.objective_id)
                if obj and obj.status in {"planning", "running", "finalizing"}:
                    obj.status = "needs_attention"
            await k.events.emit("objective.needs_attention", f"A task failed: {task.title}", namespace_id=task.namespace_id,
                                subject=task.objective_id, level="error", data={"objective_id": task.objective_id, "task_id": task.id})

    async def _wake_parent_if_ready(self, parent_task_id: str) -> None:
        async with self.k.db.session() as s:
            parent = await s.get(Task, parent_task_id)
            if not parent or parent.status != "waiting":
                return
            children = list((await s.execute(select(Task).where(Task.parent_task_id == parent_task_id))).scalars())
            if any(c.status not in TERMINAL_TASK for c in children):
                return
            run = (await s.execute(select(Run).where(Run.task_id == parent_task_id, Run.status == "suspended")
                                   .order_by(Run.started_at.desc()))).scalars().first()
        if run:
            await self._resume_job(run, parent)

    async def advance(self, objective_id: str) -> None:
        """Release tasks whose prerequisites are met; start finalization when all planned work is done."""
        k = self.k
        to_enqueue: list[Task] = []
        finalize = False
        async with k.db.session() as s:
            obj = await s.get(Objective, objective_id)
            if not obj or obj.status not in {"planning", "running", "needs_attention"}:
                return
            tasks = list((await s.execute(select(Task).where(Task.objective_id == objective_id, Task.kind == "work",
                                                             Task.parent_task_id.is_(None)))).scalars())
            if not tasks:
                return
            status = {t.id: t.status for t in tasks}
            for t in tasks:
                if t.status == "pending" and all(status.get(d) == "done" for d in t.depends_on):
                    res = await s.execute(update(Task).where(Task.id == t.id, Task.status == "pending").values(status="ready"))
                    if res.rowcount == 1:
                        to_enqueue.append(t)
            has_finalize = (await s.execute(select(Task.id).where(Task.objective_id == objective_id, Task.kind == "finalize",
                                                                  Task.status != "cancelled"))).first()
            if all(t.status == "done" for t in tasks) and not has_finalize:
                res = await s.execute(update(Objective).where(Objective.id == objective_id, Objective.status.in_(["running", "needs_attention"]))
                                      .values(status="finalizing"))
                finalize = res.rowcount == 1
        for t in to_enqueue:
            await self._enqueue(t)
        if finalize:
            ordered = sorted(tasks, key=lambda t: t.created_at)
            results = "\n\n".join(f"## {t.title}\n{t.result[:5000]}" for t in ordered)
            await self.create_task(
                namespace_id=obj.namespace_id, objective_id=objective_id, kind="finalize", title=f"Evaluate and report: {obj.title}",
                description=FINALIZE_BRIEF.format(title=obj.title, results=results), agent_id=obj.owner_agent_id, priority=2,
                meta={"run_options": {"run_tools": ["objective_complete"], "required_tool": "objective_complete"}})

    # --- review --------------------------------------------------------------------

    async def _start_review(self, task: Task, result: str) -> None:
        k = self.k
        author = await k.agents.get(task.assigned_agent_id)
        reviewer = await k.agents.get(task.meta["reviewer_id"]) if task.meta.get("reviewer_id") else None
        if not reviewer and author.department == "Development":
            qa = await k.agents.find_for_role(task.namespace_id, "qa_engineer")
            reviewer = qa if qa and qa.id != author.id else None
        if not reviewer and author.manager_id:
            reviewer = await k.agents.get(author.manager_id)
        if not reviewer:
            lead = await k.agents.lead(task.namespace_id)
            reviewer = lead if lead and lead.id != author.id else None
        if not reviewer:
            # Nobody independent is available: accept, but leave a visible trace rather than pretending it was reviewed.
            await k.events.emit("review.skipped", f"No independent reviewer for: {task.title}", namespace_id=task.namespace_id,
                                subject=task.id, level="warn", data={"task_id": task.id})
            async with k.db.session() as s:
                row = await s.get(Task, task.id)
                row.meta = {**row.meta, "review_passed": True, "review_skipped": True}
                row.status = "running"
            task.meta = {**task.meta, "review_passed": True}
            await self._complete(task, result)
            return
        await self.create_task(
            namespace_id=task.namespace_id, objective_id=task.objective_id, parent_task_id=task.id, kind="review",
            title=f"Review: {task.title}", agent_id=reviewer.id, priority=max(1, task.priority - 1),
            description=(f"Review the work {author.name} ({author.title}) delivered for the task below. Inspect the actual outputs "
                         "in the workspace where you can (read the files, run the code) rather than trusting the report. "
                         "Then call `review_submit`.\n\n"
                         f"## Task\n{task.title}\n{task.description}\n\n## Author's report\n{result[:8000]}"),
            meta={"run_options": {"run_tools": ["review_submit"], "required_tool": "review_submit"}, "reviews_task": task.id})

    async def _on_review_done(self, review: Task) -> None:
        k = self.k
        target_id = review.meta.get("reviews_task")
        if not target_id:
            return
        async with k.db.session() as s:
            fresh = await s.get(Task, review.id)
            target = await s.get(Task, target_id)
            if not target or target.status != "review":
                return
            verdict = fresh.meta.get("verdict") or {}
            review_failed = fresh.status != "done"  # a broken review must not deadlock the work it reviews
            approved = verdict.get("verdict") == "approve" or review_failed
            exhausted = target.attempts >= target.max_attempts
            if approved or exhausted:
                note = "review could not be completed" if review_failed else ("max attempts reached" if not approved else None)
                target.meta = {**target.meta, "review_passed": True, "review": verdict, **({"review_overridden": note} if note else {})}
                target.status = "running"
            else:
                target.meta = {**target.meta, "review": verdict,
                               "feedback": f"A reviewer requested changes: {verdict.get('comments', '')}"}
                target.status = "ready"
            result = target.result
        if approved or exhausted:
            await self._complete(target, result)
        else:
            await k.events.emit("review.changes_requested", f"Changes requested: {target.title}", namespace_id=target.namespace_id,
                                subject=target.id, level="warn", data={"task_id": target.id, "comments": verdict.get("comments", "")[:1000]})
            await self._enqueue(target)

    # --- human control ---------------------------------------------------------------

    async def retry_task(self, task_id: str) -> Task:
        async with self.k.db.session() as s:
            t = await s.get(Task, task_id)
            if not t:
                raise NotFound("task not found")
            if t.status not in {"failed", "blocked", "cancelled"}:
                raise Conflict(f"only failed, blocked or cancelled tasks can be retried (task is {t.status})")
            t.status, t.error, t.attempts, t.finished_at = ("ready" if not t.depends_on else "pending"), "", 0, None
            if t.objective_id:
                for other in (await s.execute(select(Task).where(Task.objective_id == t.objective_id, Task.status == "blocked"))).scalars():
                    other.status, other.error = "pending", ""
                obj = await s.get(Objective, t.objective_id)
                if obj and obj.status in {"needs_attention", "failed"}:
                    obj.status = "running"
        if t.status == "ready":
            await self._enqueue(t)
        elif t.objective_id:
            await self.advance(t.objective_id)
        return t

    async def cancel_task(self, task_id: str) -> Task:
        async with self.k.db.session() as s:
            t = await s.get(Task, task_id)
            if not t:
                raise NotFound("task not found")
            if t.status in {"done", "failed", "cancelled"}:
                raise Conflict(f"task is already {t.status}")
            t.status, t.finished_at = "cancelled", utcnow()
        await self.k.events.emit("task.cancelled", t.title, namespace_id=t.namespace_id, subject=t.id, level="warn", data={"task_id": t.id})
        return t

    async def cancel_objective(self, objective_id: str) -> Objective:
        async with self.k.db.session() as s:
            obj = await s.get(Objective, objective_id)
            if not obj:
                raise NotFound("objective not found")
            obj.status, obj.finished_at = "cancelled", utcnow()
            for t in (await s.execute(select(Task).where(Task.objective_id == objective_id))).scalars():
                if t.status not in {"done", "failed", "cancelled"}:
                    t.status, t.finished_at = "cancelled", utcnow()
        await self.k.events.emit("objective.cancelled", obj.title, namespace_id=obj.namespace_id, subject=obj.id, level="warn")
        return obj

    async def on_approval_decided(self, run_id: str | None) -> None:
        if not run_id:
            return
        async with self.k.db.session() as s:
            run = await s.get(Run, run_id)
            task = await s.get(Task, run.task_id) if run else None
        if run and task and run.status == "suspended":
            await self._resume_job(run, task)

    async def resume_halted(self) -> int:
        """After an emergency stop or pause is lifted, put halted runs back on the queue."""
        async with self.k.db.session() as s:
            runs = list((await s.execute(select(Run).where(Run.status == "suspended"))).scalars())
            pairs = [(r, await s.get(Task, r.task_id)) for r in runs if (r.pending or {}).get("reason") == "halted"]
        n = 0
        for run, task in pairs:
            if task and task.status == "paused" and not await self.k.namespaces.is_paused(task.namespace_id):
                await self._resume_job(run, task)
                n += 1
        return n

    # --- structured hand-back tools ---------------------------------------------------

    async def _tool_plan_submit(self, ctx: ToolContext, a: dict) -> str:
        k = self.k
        plan_task = await self.get_task(ctx.task_id)
        obj = await self.get_objective(plan_task.objective_id)
        items = a["tasks"]
        if not items:
            raise AiosError("a plan needs at least one task")
        if len(items) > MAX_PLAN_TASKS:
            raise AiosError(f"plan too large ({len(items)} tasks, limit {MAX_PLAN_TASKS}); merge related work")
        keys = [t["key"] for t in items]
        if len(set(keys)) != len(keys):
            raise AiosError("task keys must be unique")
        for t in items:
            unknown = [d for d in t.get("depends_on", []) if d not in keys]
            if unknown or t["key"] in t.get("depends_on", []):
                raise AiosError(f"task {t['key']!r} has invalid dependencies {unknown or [t['key']]}")
        # Kahn's algorithm: reject cycles before anything is created.
        indeg = {t["key"]: len(t.get("depends_on", [])) for t in items}
        ready, seen = [key for key, d in indeg.items() if d == 0], 0
        while ready:
            cur = ready.pop()
            seen += 1
            for t in items:
                if cur in t.get("depends_on", []):
                    indeg[t["key"]] -= 1
                    if indeg[t["key"]] == 0:
                        ready.append(t["key"])
        if seen != len(items):
            raise AiosError("the plan's dependencies form a cycle")

        usable = {ag.id: ag for ag in await k.agents.usable(obj.namespace_id)}
        assignments: dict[str, Agent] = {}
        for t in items:
            agent = usable.get(t.get("agent_id") or "")
            if not agent and t.get("role"):
                agent = await k.agents.find_for_role(obj.namespace_id, t["role"])
                if not agent and self.provisioner:
                    agent = await self.provisioner(obj.namespace_id, t["role"], ctx.agent)
            assignments[t["key"]] = agent or ctx.agent

        ids = {t["key"]: new_id("tsk") for t in items}
        async with k.db.session() as s:
            for t in items:
                deps = [ids[d] for d in t.get("depends_on", [])]
                s.add(Task(id=ids[t["key"]], namespace_id=obj.namespace_id, objective_id=obj.id, kind="work", title=t["title"][:255],
                           description=t["description"], assigned_agent_id=assignments[t["key"]].id, required_role=t.get("role", ""),
                           depends_on=deps, priority=int(t.get("priority", 5)), status="pending",
                           meta={"needs_review": bool(t.get("needs_review")), "plan_key": t["key"]}))
            row = await s.get(Objective, obj.id)
            row.status = "running"
            row.meta = {**row.meta, "plan_summary": a.get("summary", "")}
        await k.events.emit("objective.planned", f"{obj.title}: {len(items)} tasks", namespace_id=obj.namespace_id, actor=ctx.actor,
                            subject=obj.id, data={"objective_id": obj.id, "tasks": [
                                {"key": t["key"], "title": t["title"], "agent": assignments[t["key"]].name} for t in items]})
        return "Plan accepted: " + "; ".join(f"{t['title']} → {assignments[t['key']].name}" for t in items)

    async def _tool_review_submit(self, ctx: ToolContext, a: dict) -> str:
        async with self.k.db.session() as s:
            row = await s.get(Task, ctx.task_id)
            row.meta = {**row.meta, "verdict": {"verdict": a["verdict"], "score": a.get("score"), "comments": a.get("comments", ""),
                                                "reviewer": ctx.agent.id}}
        await self.k.events.emit("review.submitted", f"{ctx.agent.name}: {a['verdict']}", namespace_id=ctx.namespace_id, actor=ctx.actor,
                                 subject=ctx.task_id, data={"task_id": ctx.task_id, "verdict": a["verdict"], "score": a.get("score")})
        return f"Review recorded: {a['verdict']}. {a.get('comments', '')}"

    async def _tool_objective_complete(self, ctx: ToolContext, a: dict) -> str:
        task = await self.get_task(ctx.task_id)
        async with self.k.db.session() as s:
            obj = await s.get(Objective, task.objective_id)
            obj.result = a["summary"]
            obj.evaluation = {"achieved": bool(a["achieved"]), "score": a.get("score"), "lessons": a.get("lessons", []),
                              "follow_ups": a.get("follow_ups", []), "evaluated_by": ctx.agent.id}
        return a["summary"]

    async def _close_objective(self, objective_id: str, actor: str) -> None:
        """Runs once the finalize task itself is done, so an objective is never 'completed' with work still open."""
        k = self.k
        async with k.db.session() as s:
            obj = await s.get(Objective, objective_id)
            if not obj or obj.status == "completed":
                return
            obj.status, obj.finished_at = "completed", utcnow()
        ev = obj.evaluation or {}
        try:
            await k.memory.add(namespace_id=obj.namespace_id, title=f"Objective outcome: {obj.title}", content=obj.result,
                               kind="result", meta={"objective_id": obj.id})
            for lesson in ev.get("lessons", [])[:10]:
                await k.memory.add(namespace_id=obj.namespace_id, title=f"Lesson from: {obj.title}", content=lesson, kind="lesson",
                                   meta={"objective_id": obj.id})
        except Exception:
            log.exception("could not store objective outcome in memory")
        await k.events.emit("objective.completed", obj.title, namespace_id=obj.namespace_id, actor=actor, subject=obj.id,
                            level="info" if ev.get("achieved") else "warn",
                            data={"objective_id": obj.id, "achieved": bool(ev.get("achieved")), "score": ev.get("score"),
                                  "follow_ups": ev.get("follow_ups", [])})

    async def _tool_request_approval(self, ctx: ToolContext, a: dict) -> str:
        # Reaching this handler means the gateway obtained a human "approve" for this exact request.
        return f"A human approved: {a['action']}. You may proceed with it and should record that approval was given."

    def register_tools(self, registry) -> None:
        st = {"type": "string"}
        registry.register(Tool(
            "plan_submit", "Submit the task plan for the objective. Call exactly once.",
            schema({"summary": st, "tasks": {"type": "array", "minItems": 1, "items": {
                "type": "object", "additionalProperties": False, "required": ["key", "title", "description"],
                "properties": {"key": {"type": "string", "description": "short unique id used in depends_on"}, "title": st,
                               "description": st, "role": st, "agent_id": st, "depends_on": {"type": "array", "items": st},
                               "priority": {"type": "integer", "minimum": 1, "maximum": 9}, "needs_review": {"type": "boolean"}}}}},
                   ["tasks"]),
            self._tool_plan_submit, "orchestration.plan", "write_internal", "orchestration", terminal=True))
        registry.register(Tool(
            "review_submit", "Record your review verdict for the work under review.",
            schema({"verdict": {"type": "string", "enum": ["approve", "request_changes"]},
                    "score": {"type": "number", "minimum": 0, "maximum": 1}, "comments": st}, ["verdict", "comments"]),
            self._tool_review_submit, "orchestration.review", "write_internal", "orchestration", terminal=True))
        registry.register(Tool(
            "objective_complete", "Close the objective with your evaluation and final report.",
            schema({"summary": st, "achieved": {"type": "boolean"}, "score": {"type": "number", "minimum": 0, "maximum": 1},
                    "lessons": {"type": "array", "items": st}, "follow_ups": {"type": "array", "items": st}}, ["summary", "achieved"]),
            self._tool_objective_complete, "orchestration.finalize", "write_internal", "orchestration", terminal=True))
        registry.register(Tool(
            "request_approval",
            "Ask a human to authorise a consequential action that happens outside this system: spending or committing money, "
            "entering a legal commitment, or handling sensitive personal data. "
            "Describe exactly what would be done and why. The call returns only once a human has decided.",
            schema({"action": st, "category": {"type": "string", "enum": ["financial", "legal", "sensitive_data"]},
                    "justification": st, "amount_usd": {"type": "number"}}, ["action", "category", "justification"]),
            self._tool_request_approval, "approvals.request", lambda a: a.get("category", "financial"), "governance"))
