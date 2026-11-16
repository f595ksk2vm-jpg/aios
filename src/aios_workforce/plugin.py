"""Layer 2 entry point."""
from __future__ import annotations

from sqlalchemy import func, select

from aios_kernel.models import Agent, Approval, Run, Task, UsageRecord

from . import models as _models  # noqa: F401  (registers Layer 2 tables on the shared metadata)
from .collaboration import Collaboration
from .factory import AgentFactory


class Workforce:
    def __init__(self, kernel):
        self.k = kernel
        self.factory = AgentFactory(kernel)
        self.collaboration = Collaboration(kernel, self.factory)

    async def performance(self, agent_id: str) -> dict:
        """Operational metrics for one agent, computed from the kernel's execution records."""
        async with self.k.db.session() as s:
            rows = (await s.execute(select(Task.status, func.count()).where(Task.assigned_agent_id == agent_id, Task.kind == "work")
                                    .group_by(Task.status))).all()
            by_status = {st: n for st, n in rows}
            done = list((await s.execute(select(Task).where(Task.assigned_agent_id == agent_id, Task.status == "done",
                                                            Task.kind == "work").limit(500))).scalars())
            cost, tin, tout, calls = (await s.execute(select(
                func.coalesce(func.sum(UsageRecord.cost_usd), 0.0), func.coalesce(func.sum(UsageRecord.input_tokens), 0),
                func.coalesce(func.sum(UsageRecord.output_tokens), 0), func.count()).where(
                UsageRecord.agent_id == agent_id, UsageRecord.kind == "llm"))).one()
            steps = (await s.execute(select(func.coalesce(func.avg(Run.steps), 0.0)).where(Run.agent_id == agent_id))).scalar_one()
            approvals = (await s.execute(select(func.count()).select_from(Approval).where(Approval.agent_id == agent_id))).scalar_one()
        durations = [(t.finished_at - t.started_at).total_seconds() for t in done if t.finished_at and t.started_at]
        scores = [t.meta["review"]["score"] for t in done if isinstance((t.meta.get("review") or {}).get("score"), (int, float))]
        finished = by_status.get("done", 0) + by_status.get("failed", 0)
        return {
            "agent_id": agent_id, "tasks_done": by_status.get("done", 0), "tasks_failed": by_status.get("failed", 0),
            "tasks_active": sum(n for st, n in by_status.items() if st in {"ready", "running", "waiting", "awaiting_approval", "review"}),
            "success_rate": round(by_status.get("done", 0) / finished, 3) if finished else None,
            "avg_duration_s": round(sum(durations) / len(durations), 1) if durations else None,
            "avg_review_score": round(sum(scores) / len(scores), 3) if scores else None,
            "avg_steps_per_run": round(float(steps), 1), "llm_calls": int(calls), "input_tokens": int(tin), "output_tokens": int(tout),
            "cost_usd": round(float(cost), 6), "approvals_requested": int(approvals),
        }

    async def leaderboard(self, namespace_ids: list[str] | None = None) -> list[dict]:
        q = select(Agent).where(Agent.status != "deactivated")
        if namespace_ids is not None:
            q = q.where(Agent.namespace_id.in_(namespace_ids))
        async with self.k.db.session() as s:
            agents = list((await s.execute(q)).scalars())
        return [{"name": a.name, "title": a.title, **(await self.performance(a.id))} for a in agents]


def install(kernel) -> Workforce:
    """Attach Layer 2 to a kernel. Call before ``kernel.start()`` so its tables are created."""
    wf = Workforce(kernel)
    wf.collaboration.register(kernel.tools)
    kernel.orchestrator.provisioner = wf.factory.provision
    kernel.extensions["workforce"] = wf
    return wf
