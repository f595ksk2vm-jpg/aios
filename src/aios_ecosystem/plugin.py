"""Layer 3 services and entry point."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from pathlib import Path

from sqlalchemy import func, select, update

from aios_kernel.db import new_id, utcnow
from aios_kernel.errors import AiosError, Conflict, NotFound
from aios_kernel.models import ROOT_NS, Agent, Event, Objective, Task, UsageRecord
from aios_kernel.security import policy
from aios_kernel.tools import builtin
from aios_kernel.tools.base import Tool, ToolContext, schema

from .models import Business, Customer, Deliverable, Kpi, KpiPoint, LedgerEntry, Project, Workflow

log = logging.getLogger("aios.ecosystem")

PROJECT_KINDS = ["website", "saas", "mobile_app", "api", "platform", "ai_app", "automation", "internal_tool",
                 "digital_product", "research", "software"]
MAX_AGENT_OPENED_PROJECTS = 5

KICKOFF = """\
Establish the foundation for a new business.

Business: {name}
Idea: {idea}

Produce, as real documents in the business workspace:
- docs/market-research.md — the market, target customers, their needs, competitors (facts with sources where you can reach them; label estimates as estimates)
- docs/business-model.md — value proposition, customer segments, revenue model, cost structure, pricing hypothesis
- docs/strategy.md — positioning, go-to-market, first milestones, the main risks and how to test them
- docs/org-design.md — which roles this business needs now and later, and why

Then record the essentials in the system: update the business profile (mission, business model, strategy) and define the 3–5 KPIs \
this business should be run on. If a first product or project is clearly warranted, open it as a project."""

PROJECT_KICKOFF = """\
Deliver the project "{name}" ({kind}).

{description}

## Requirements
{requirements}

The project workspace is a git repository. Deliver working, verified results in it: source code with tests that have actually \
been run for software, well-structured documents for research. Keep docs/ up to date so someone new could pick the project up."""

# Which Layer 3 capabilities an agent receives when it joins a business, by role.
_ALL = ["business_profile_update", "kpi_define", "kpi_record", "ledger_record", "crm_list_customers", "crm_upsert_customer",
        "crm_delete_customer", "project_create"]
ROLE_TOOLS: dict[str, list[str]] = {
    **{r: _ALL for r in ("ceo", "coo", "cfo")},
    "cto": ["kpi_record", "project_create"],
    "strategy_director": ["business_profile_update", "kpi_define", "kpi_record", "project_create"],
    "project_manager": ["kpi_record", "project_create"],
    "product_designer": ["project_create"],
    "finance_agent": ["ledger_record", "kpi_record"],
    "business_intelligence_analyst": ["kpi_define", "kpi_record", "crm_list_customers"],
    "data_analyst": ["kpi_record", "crm_list_customers"],
    "operations_analyst": ["kpi_record"],
    "marketing_strategist": ["kpi_record", "crm_list_customers"],
    "sales_agent": ["crm_list_customers", "crm_upsert_customer"],
    "lead_generation_agent": ["crm_list_customers", "crm_upsert_customer"],
    "customer_support_agent": ["crm_list_customers", "crm_upsert_customer"],
}
TOOL_PERMS = {"business_profile_update": "biz.profile", "kpi_define": "biz.kpi", "kpi_record": "biz.kpi", "ledger_record": "biz.finance",
              "crm_list_customers": "biz.crm.read", "crm_upsert_customer": "biz.crm.write", "crm_delete_customer": "biz.crm.delete",
              "project_create": "biz.projects"}


class Ecosystem:
    def __init__(self, kernel):
        self.k = kernel
        self._scheduler: asyncio.Task | None = None
        self._repo_locks: dict[str, asyncio.Lock] = {}

    @property
    def workforce(self):
        return self.k.extensions["workforce"]

    # ====================================================================== businesses

    async def create_business(self, *, name: str, idea: str, created_by: str, org_preset: str = "lean", autonomy: str = "supervised",
                              compute: str = "any", models: str = "any", monthly_budget_usd: float | None = None,
                              auto_provision: bool = True, kickoff: bool = True) -> Business:
        k = self.k
        if autonomy not in policy.AUTONOMY_LEVELS:
            raise Conflict(f"autonomy must be one of {policy.AUTONOMY_LEVELS}")
        ns = await k.namespaces.create("business", name, compute=compute, models=models,
                                       meta={"auto_provision": auto_provision, "default_autonomy": autonomy})
        biz = Business(id=new_id("biz"), namespace_id=ns.id, name=name, idea=idea, created_by=created_by)
        async with k.db.session() as s:
            s.add(biz)
        ws = k.workspace_for(ns.id)
        await builtin.ensure_repo(ws)
        (ws / "README.md").write_text(f"# {name}\n\n{idea}\n")
        (ws / "docs").mkdir(exist_ok=True)
        await builtin.commit_all(ws, "Initialize business workspace")
        await self.workforce.factory.create_org(ns.id, org_preset, autonomy=autonomy, created_by=created_by)
        if monthly_budget_usd is not None:
            await k.budget.set_budget("namespace", ns.id, "month", monthly_budget_usd)
        await k.memory.add(namespace_id=ns.id, title=f"About {name}", content=f"{name}: {idea}", kind="fact")
        await k.audit.record(created_by, "business.create", resource=biz.id, namespace_id=ns.id,
                             detail={"name": name, "preset": org_preset, "autonomy": autonomy, "compute": compute, "models": models})
        await k.events.emit("business.created", f"Business created: {name}", namespace_id=ns.id, actor=created_by, subject=biz.id,
                            data={"business_id": biz.id})
        if kickoff:
            await k.orchestrator.create_objective(namespace_id=ns.id, title=f"Establish the foundation for {name}",
                                                  description=KICKOFF.format(name=name, idea=idea), created_by=created_by,
                                                  meta={"kickoff": True})
        return biz

    async def businesses(self, include_archived: bool = False) -> list[Business]:
        q = select(Business).order_by(Business.created_at)
        if not include_archived:
            q = q.where(Business.status != "archived")
        async with self.k.db.session() as s:
            return list((await s.execute(q)).scalars())

    async def business(self, business_id: str) -> Business:
        async with self.k.db.session() as s:
            b = await s.get(Business, business_id)
        if not b:
            raise NotFound("business not found")
        return b

    async def business_for_namespace(self, namespace_id: str) -> Business | None:
        chain = await self.k.namespaces.chain_ids(namespace_id)
        async with self.k.db.session() as s:
            return (await s.execute(select(Business).where(Business.namespace_id.in_(chain)))).scalars().first()

    async def update_business(self, business_id: str, **fields) -> Business:
        allowed = {"name", "idea", "mission", "business_model", "strategy", "stage"}
        async with self.k.db.session() as s:
            b = await s.get(Business, business_id)
            if not b:
                raise NotFound("business not found")
            for key, v in fields.items():
                if key in allowed and v is not None:
                    setattr(b, key, v)
        return b

    async def set_business_status(self, business_id: str, status: str, by: str) -> Business:
        if status not in {"active", "paused", "archived"}:
            raise Conflict("status must be active|paused|archived")
        k = self.k
        async with k.db.session() as s:
            b = await s.get(Business, business_id)
            if not b:
                raise NotFound("business not found")
            b.status = status
        # Pausing the namespace halts every run beneath it at its next step.
        await k.namespaces.update(b.namespace_id, status="active" if status == "active" else status)
        await k.audit.record(by, f"business.{status}", resource=b.id, namespace_id=b.namespace_id)
        await k.events.emit(f"business.{status}", f"{b.name} is now {status}", namespace_id=b.namespace_id, actor=by,
                            level="warn" if status != "active" else "info")
        if status == "active":
            await k.orchestrator.resume_halted()
        return b

    async def namespace_tree(self, business: Business) -> list[str]:
        return [business.namespace_id] + await self.k.namespaces.descendants(business.namespace_id)

    async def overview(self, business_id: str) -> dict:
        k = self.k
        b = await self.business(business_id)
        tree = await self.namespace_tree(b)
        async with k.db.session() as s:
            task_rows = (await s.execute(select(Task.status, func.count()).where(Task.namespace_id.in_(tree), Task.kind == "work")
                                         .group_by(Task.status))).all()
            obj_rows = (await s.execute(select(Objective.status, func.count()).where(Objective.namespace_id.in_(tree))
                                        .group_by(Objective.status))).all()
            agents = (await s.execute(select(func.count()).select_from(Agent).where(Agent.namespace_id.in_(tree),
                                                                                    Agent.status == "active"))).scalar_one()
            projects = (await s.execute(select(func.count()).select_from(Project).where(Project.business_id == b.id,
                                                                                        Project.status != "archived"))).scalar_one()
            customers = (await s.execute(select(Customer.stage, func.count()).where(Customer.business_id == b.id)
                                         .group_by(Customer.stage))).all()
            ai_cost = (await s.execute(select(func.coalesce(func.sum(UsageRecord.cost_usd), 0.0))
                                       .where(UsageRecord.namespace_id.in_(tree)))).scalar_one()
            deliverables = (await s.execute(select(func.count()).select_from(Deliverable).where(Deliverable.namespace_id.in_(tree)))).scalar_one()
        ns = await k.namespaces.get(b.namespace_id)
        return {"business": b.to_dict(), "namespace": ns.to_dict(), "agents": agents, "projects": projects,
                "tasks": dict(task_rows), "objectives": dict(obj_rows), "customers": dict(customers),
                "deliverables": deliverables, "ai_cost_usd": round(float(ai_cost), 6),
                "finance": await self.finance_summary(b.id), "kpis": await self.kpis(b.id)}

    # ====================================================================== projects

    async def create_project(self, *, name: str, kind: str = "software", description: str = "", requirements: str = "",
                             business_id: str | None = None, created_by: str, kickoff: bool = True,
                             owner_agent_id: str | None = None) -> Project:
        k = self.k
        if kind not in PROJECT_KINDS:
            raise Conflict(f"kind must be one of {PROJECT_KINDS}")
        parent = ROOT_NS
        if business_id:
            parent = (await self.business(business_id)).namespace_id
        ns = await k.namespaces.create("project", name, parent_id=parent)
        proj = Project(id=new_id("prj"), business_id=business_id, namespace_id=ns.id, name=name, kind=kind, description=description,
                       requirements=requirements, created_by=created_by)
        async with k.db.session() as s:
            s.add(proj)
        ws = k.workspace_for(ns.id)
        await builtin.ensure_repo(ws)
        (ws / "docs").mkdir(exist_ok=True)
        (ws / "README.md").write_text(f"# {name}\n\n{description}\n")
        (ws / "docs" / "REQUIREMENTS.md").write_text(f"# Requirements — {name}\n\n{requirements or '(to be defined)'}\n")
        await builtin.commit_all(ws, "Initialize project workspace")
        await k.memory.add(namespace_id=ns.id, title=f"Project brief: {name}", kind="fact",
                           content=f"{name} ({kind}). {description}\nRequirements: {requirements}")
        await k.events.emit("project.created", f"Project created: {name}", namespace_id=ns.id, actor=created_by, subject=proj.id,
                            data={"project_id": proj.id, "business_id": business_id})
        if kickoff:
            await k.orchestrator.create_objective(
                namespace_id=ns.id, title=f"Deliver project: {name}", created_by=created_by, owner_agent_id=owner_agent_id,
                description=PROJECT_KICKOFF.format(name=name, kind=kind, description=description or "(no description)",
                                                   requirements=requirements or "(derive sensible requirements from the description)"),
                meta={"kickoff": True, "project_id": proj.id})
        return proj

    async def projects(self, business_id: str | None = None, include_archived: bool = False) -> list[Project]:
        q = select(Project).order_by(Project.created_at.desc())
        if business_id:
            q = q.where(Project.business_id == business_id)
        if not include_archived:
            q = q.where(Project.status != "archived")
        async with self.k.db.session() as s:
            return list((await s.execute(q)).scalars())

    async def project(self, project_id: str) -> Project:
        async with self.k.db.session() as s:
            p = await s.get(Project, project_id)
        if not p:
            raise NotFound("project not found")
        return p

    async def update_project(self, project_id: str, by: str, **fields) -> Project:
        allowed = {"name", "description", "requirements", "status", "kind"}
        async with self.k.db.session() as s:
            p = await s.get(Project, project_id)
            if not p:
                raise NotFound("project not found")
            for key, v in fields.items():
                if key in allowed and v is not None:
                    if key == "status" and v not in {"active", "paused", "completed", "archived"}:
                        raise Conflict("invalid status")
                    setattr(p, key, v)
        if "status" in fields and fields["status"]:
            await self.k.namespaces.update(p.namespace_id, status="paused" if p.status in {"paused", "archived"} else "active")
            if p.status == "active":
                await self.k.orchestrator.resume_halted()
        return p

    async def project_progress(self, project: Project) -> dict:
        async with self.k.db.session() as s:
            rows = dict((await s.execute(select(Task.status, func.count()).where(Task.namespace_id == project.namespace_id,
                                                                                 Task.kind == "work").group_by(Task.status))).all())
            objectives = list((await s.execute(select(Objective).where(Objective.namespace_id == project.namespace_id)
                                               .order_by(Objective.created_at.desc()))).scalars())
            agent_ids = [r[0] for r in (await s.execute(select(Task.assigned_agent_id).where(
                Task.namespace_id == project.namespace_id, Task.assigned_agent_id.is_not(None)).distinct())).all()]
        total = sum(rows.values())
        return {"tasks": rows, "total": total, "done": rows.get("done", 0),
                "percent": round(100 * rows.get("done", 0) / total) if total else 0,
                "objectives": [o.to_dict() for o in objectives], "assigned_agents": agent_ids}

    def workspace_files(self, namespace_id: str, rel: str = ".") -> list[dict]:
        ws = self.k.workspace_for(namespace_id)
        base = builtin.resolve_path(ws, rel)
        if not base.is_dir():
            raise NotFound("directory not found")
        out = []
        for p in sorted(base.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            if p.name in builtin.PROTECTED_DIRS:
                continue
            out.append({"name": p.name, "path": str(p.relative_to(ws.resolve())), "dir": p.is_dir(),
                        "size": p.stat().st_size if p.is_file() else None})
        return out

    def workspace_file(self, namespace_id: str, rel: str) -> dict:
        ws = self.k.workspace_for(namespace_id)
        p = builtin.resolve_path(ws, rel)
        if not p.is_file():
            raise NotFound("file not found")
        data = p.read_bytes()
        binary = b"\x00" in data[:4096]
        return {"path": rel, "size": len(data), "binary": binary,
                "content": None if binary else data[:400_000].decode(errors="replace"), "truncated": len(data) > 400_000}

    async def workspace_history(self, namespace_id: str, limit: int = 50) -> list[dict]:
        ws = self.k.workspace_for(namespace_id)
        if not (ws / ".git").exists():
            return []
        raw = await builtin.git(ws, "log", f"-{limit}", "--date=iso-strict", "--format=%H%x1f%ad%x1f%an%x1f%s", check=False)
        return [dict(zip(("commit", "date", "author", "message"), line.split("\x1f"))) for line in raw.splitlines() if line]

    async def deliverables(self, namespace_ids: list[str]) -> list[Deliverable]:
        async with self.k.db.session() as s:
            return list((await s.execute(select(Deliverable).where(Deliverable.namespace_id.in_(namespace_ids))
                                         .order_by(Deliverable.updated_at.desc()))).scalars())

    async def _on_task_completed(self, ev: Event) -> None:
        """Version the workspace after each finished task and register what changed as deliverables."""
        if ev.data.get("kind") != "work":
            return
        k = self.k
        ns = await k.namespaces.get(ev.namespace_id)
        if ns.kind not in {"business", "project"}:
            return
        ws = k.workspace_for(ns.id)
        agent_id = ev.data.get("agent_id")
        author = "AI-OS"
        if agent_id:
            try:
                author = (await k.agents.get(agent_id)).name
            except NotFound:
                pass
        title = ev.message.split("completed: ", 1)[-1]
        task_id = ev.data.get("task_id")
        # Files this task wrote explicitly, read from its persisted transcript (so it works across workers).
        touched: list[str] = []
        for run in await k.orchestrator.runs_for(task_id):
            for m in run.messages:
                for c in m.get("tool_calls", []) if m["role"] == "assistant" else []:
                    path = c["arguments"].get("path")
                    if c["name"] in {"fs_write", "fs_delete"} and path and path not in touched:
                        touched.append(path)
        async with k.db.session() as s:
            others = (await s.execute(select(func.count()).select_from(Task).where(
                Task.namespace_id == ns.id, Task.id != task_id, Task.kind == "work",
                Task.status.in_(["running", "waiting", "awaiting_approval", "review", "paused"])))).scalar_one()
        async with self._repo_locks.setdefault(ns.id, asyncio.Lock()):
            res = await builtin.commit_all(ws, f"{title}\n\nTask: {task_id}", author=author, paths=touched)
            if not others:
                # Nobody else is working here, so whatever else changed (build outputs, generated files) is this task's too.
                extra = await builtin.commit_all(ws, f"{title} (generated files)\n\nTask: {task_id}", author=author)
                if extra:
                    res = {"commit": extra["commit"], "files": (res["files"] if res else []) + extra["files"]}
        if not res:
            return
        chain = await k.namespaces.chain_ids(ns.id)
        async with k.db.session() as s:
            proj = (await s.execute(select(Project).where(Project.namespace_id == ns.id))).scalar_one_or_none()
            biz = (await s.execute(select(Business).where(Business.namespace_id.in_(chain)))).scalars().first()
            for f in res["files"]:
                path = f["path"]
                row = (await s.execute(select(Deliverable).where(Deliverable.namespace_id == ns.id, Deliverable.path == path))).scalar_one_or_none()
                if f["status"].startswith("D"):
                    if row:
                        await s.delete(row)
                    continue
                size = (ws / path).stat().st_size if (ws / path).is_file() else 0
                if row:
                    row.version, row.commit, row.size_bytes, row.updated_at = row.version + 1, res["commit"], size, utcnow()
                    row.task_id, row.agent_id = ev.data.get("task_id"), agent_id
                else:
                    s.add(Deliverable(id=new_id("dlv"), namespace_id=ns.id, business_id=biz.id if biz else None,
                                      project_id=proj.id if proj else None, path=path, kind=_kind_of(path), size_bytes=size,
                                      commit=res["commit"], task_id=ev.data.get("task_id"), agent_id=agent_id))
        await k.events.emit("deliverable.updated", f"{len(res['files'])} file(s) delivered by {author}", namespace_id=ns.id,
                            actor=f"agent:{agent_id}" if agent_id else "system", subject=res["commit"][:10],
                            data={"commit": res["commit"], "files": [f["path"] for f in res["files"]][:40], "task_id": ev.data.get("task_id")})

    async def _on_agent_created(self, ev: Event) -> None:
        """Agents that join a business receive that business's operating tools, by role."""
        k = self.k
        if not await self.business_for_namespace(ev.namespace_id):
            return
        agent = await k.agents.get(ev.data["agent_id"])
        tools = [t for t in ROLE_TOOLS.get(agent.role, []) if k.tools.get(t)]
        perms = sorted({TOOL_PERMS[t] for t in tools})
        if agent.created_by.startswith("agent:"):
            # Created by another agent: never beyond what the creator holds.
            try:
                creator = await k.agents.get(agent.created_by.split(":", 1)[1])
            except NotFound:
                return
            perms = [p for p in perms if policy.covered_by(p, creator.permissions)]
            tools = [t for t in tools if TOOL_PERMS[t] in perms]
        if tools:
            await k.agents.update(agent.id, tools=agent.tools + [t for t in tools if t not in agent.tools],
                                  permissions=agent.permissions + [p for p in perms if p not in agent.permissions])

    # ====================================================================== finance, KPIs, customers

    async def record_ledger(self, business_id: str, *, kind: str, amount: float, category: str = "general", memo: str = "",
                            recorded_by: str = "", ts: dt.datetime | None = None) -> LedgerEntry:
        if kind not in {"revenue", "expense"}:
            raise Conflict("kind must be revenue|expense")
        if amount <= 0:
            raise Conflict("amount must be positive")
        await self.business(business_id)
        e = LedgerEntry(id=new_id("led"), business_id=business_id, kind=kind, amount=float(amount), category=category, memo=memo,
                        recorded_by=recorded_by, ts=ts or utcnow())
        async with self.k.db.session() as s:
            s.add(e)
        return e

    async def ledger(self, business_id: str, limit: int = 200) -> list[LedgerEntry]:
        async with self.k.db.session() as s:
            return list((await s.execute(select(LedgerEntry).where(LedgerEntry.business_id == business_id)
                                         .order_by(LedgerEntry.ts.desc()).limit(limit))).scalars())

    async def finance_summary(self, business_id: str) -> dict:
        entries = await self.ledger(business_id, limit=5000)
        revenue = sum(e.amount for e in entries if e.kind == "revenue")
        expense = sum(e.amount for e in entries if e.kind == "expense")
        months: dict[str, dict] = {}
        cats: dict[str, float] = {}
        for e in entries:
            m = months.setdefault(e.ts.strftime("%Y-%m"), {"month": e.ts.strftime("%Y-%m"), "revenue": 0.0, "expense": 0.0})
            m[e.kind] += e.amount
            if e.kind == "expense":
                cats[e.category] = cats.get(e.category, 0.0) + e.amount
        return {"revenue": round(revenue, 2), "expense": round(expense, 2), "net": round(revenue - expense, 2),
                "by_month": [months[m] for m in sorted(months)], "expense_by_category": cats, "entries": len(entries)}

    async def define_kpi(self, business_id: str, *, key: str, name: str, unit: str = "", target: float | None = None,
                         direction: str = "up") -> Kpi:
        if direction not in {"up", "down"}:
            raise Conflict("direction must be up|down")
        await self.business(business_id)
        key = key.strip().lower().replace(" ", "_")[:60]
        async with self.k.db.session() as s:
            kpi = (await s.execute(select(Kpi).where(Kpi.business_id == business_id, Kpi.key == key))).scalar_one_or_none()
            if kpi:
                kpi.name, kpi.unit, kpi.target, kpi.direction = name, unit, target, direction
            else:
                kpi = Kpi(id=new_id("kpi"), business_id=business_id, key=key, name=name, unit=unit, target=target, direction=direction)
                s.add(kpi)
        return kpi

    async def record_kpi(self, business_id: str, key: str, value: float, *, note: str = "", recorded_by: str = "") -> KpiPoint:
        async with self.k.db.session() as s:
            kpi = (await s.execute(select(Kpi).where(Kpi.business_id == business_id, Kpi.key == key))).scalar_one_or_none()
            if not kpi:
                raise NotFound(f"no KPI {key!r}; define it first")
            pt = KpiPoint(kpi_id=kpi.id, value=float(value), note=note[:255], recorded_by=recorded_by)
            s.add(pt)
        return pt

    async def kpis(self, business_id: str) -> list[dict]:
        async with self.k.db.session() as s:
            rows = list((await s.execute(select(Kpi).where(Kpi.business_id == business_id).order_by(Kpi.created_at))).scalars())
            out = []
            for kpi in rows:
                pts = list((await s.execute(select(KpiPoint).where(KpiPoint.kpi_id == kpi.id).order_by(KpiPoint.ts.desc()).limit(60))).scalars())
                series = [{"ts": p.ts.isoformat() + "Z", "value": p.value} for p in reversed(pts)]
                out.append({**kpi.to_dict(), "latest": series[-1]["value"] if series else None, "series": series})
        return out

    async def upsert_customer(self, business_id: str, *, name: str, email: str = "", company: str = "", stage: str = "lead",
                              notes: str = "", customer_id: str | None = None) -> Customer:
        if stage not in {"lead", "qualified", "customer", "churned"}:
            raise Conflict("stage must be lead|qualified|customer|churned")
        await self.business(business_id)
        async with self.k.db.session() as s:
            c = await s.get(Customer, customer_id) if customer_id else None
            if not c and email:
                c = (await s.execute(select(Customer).where(Customer.business_id == business_id, Customer.email == email))).scalars().first()
            if c and c.business_id != business_id:
                raise NotFound("customer not found")
            if c:
                c.name, c.company, c.stage, c.updated_at = name, company or c.company, stage, utcnow()
                c.email = email or c.email
                c.notes = notes or c.notes
            else:
                c = Customer(id=new_id("cus"), business_id=business_id, name=name, email=email, company=company, stage=stage, notes=notes)
                s.add(c)
        return c

    async def customers(self, business_id: str, stage: str | None = None) -> list[Customer]:
        q = select(Customer).where(Customer.business_id == business_id).order_by(Customer.updated_at.desc())
        if stage:
            q = q.where(Customer.stage == stage)
        async with self.k.db.session() as s:
            return list((await s.execute(q)).scalars())

    async def delete_customer(self, business_id: str, customer_id: str) -> None:
        async with self.k.db.session() as s:
            c = await s.get(Customer, customer_id)
            if not c or c.business_id != business_id:
                raise NotFound("customer not found")
            await s.delete(c)

    # ====================================================================== workflows

    async def create_workflow(self, *, namespace_id: str, name: str, objective_title: str, objective_description: str = "",
                              interval_minutes: int | None = None, created_by: str = "") -> Workflow:
        await self.k.namespaces.get(namespace_id)
        if interval_minutes is not None and interval_minutes < 5:
            raise Conflict("interval must be at least 5 minutes")
        wf = Workflow(id=new_id("wfl"), namespace_id=namespace_id, name=name, objective_title=objective_title,
                      objective_description=objective_description, interval_minutes=interval_minutes, created_by=created_by)
        async with self.k.db.session() as s:
            s.add(wf)
        return wf

    async def workflows(self, namespace_id: str | None = None) -> list[Workflow]:
        """Workflows owned by a namespace tree plus those other namespaces explicitly shared with it."""
        q = select(Workflow).order_by(Workflow.created_at.desc())
        if namespace_id:
            whole, single = await self.k.namespaces.visible(namespace_id, "workflow")
            ids = {rid for _, rid in single}
            async with self.k.db.session() as s:
                rows = list((await s.execute(q)).scalars())
            return [w for w in rows if w.namespace_id in whole or w.id in ids]
        async with self.k.db.session() as s:
            return list((await s.execute(q)).scalars())

    async def update_workflow(self, workflow_id: str, **fields) -> Workflow:
        async with self.k.db.session() as s:
            wf = await s.get(Workflow, workflow_id)
            if not wf:
                raise NotFound("workflow not found")
            for key in ("name", "objective_title", "objective_description", "interval_minutes", "enabled"):
                if key in fields and fields[key] is not None:
                    setattr(wf, key, fields[key])
        return wf

    async def delete_workflow(self, workflow_id: str) -> None:
        async with self.k.db.session() as s:
            wf = await s.get(Workflow, workflow_id)
            if not wf:
                raise NotFound("workflow not found")
            await s.delete(wf)

    async def run_workflow(self, workflow_id: str, *, by: str, namespace_id: str | None = None) -> Objective:
        """Start the workflow as an objective, in its own namespace or in one it has been shared with."""
        async with self.k.db.session() as s:
            wf = await s.get(Workflow, workflow_id)
        if not wf:
            raise NotFound("workflow not found")
        target = namespace_id or wf.namespace_id
        if target != wf.namespace_id and wf.id not in {w.id for w in await self.workflows(target)}:
            raise AiosError("this workflow has not been shared with that namespace")
        obj = await self.k.orchestrator.create_objective(namespace_id=target, title=wf.objective_title,
                                                         description=wf.objective_description, created_by=by,
                                                         meta={"workflow_id": wf.id})
        async with self.k.db.session() as s:
            await s.execute(update(Workflow).where(Workflow.id == wf.id).values(run_count=Workflow.run_count + 1, last_run_at=utcnow()))
        return obj

    async def _schedule_loop(self) -> None:
        k = self.k
        while True:
            await asyncio.sleep(30)
            try:
                if await k.killswitch.active():
                    continue
                now = utcnow()
                async with k.db.session() as s:
                    due = [w for w in (await s.execute(select(Workflow).where(Workflow.enabled.is_(True),
                                                                              Workflow.interval_minutes.is_not(None)))).scalars()
                           # The first run comes one interval after creation, not the moment the workflow is saved.
                           if now - (w.last_run_at or w.created_at) >= dt.timedelta(minutes=w.interval_minutes)]
                for w in due:
                    if await k.namespaces.is_paused(w.namespace_id):
                        continue
                    async with k.db.session() as s:
                        # Compare-and-set on last_run_at so only one control-plane process fires each occurrence.
                        res = await s.execute(update(Workflow).where(Workflow.id == w.id, Workflow.last_run_at.is_(w.last_run_at) if w.last_run_at is None
                                                                     else Workflow.last_run_at == w.last_run_at).values(last_run_at=now))
                    if res.rowcount == 1:
                        await self.run_workflow(w.id, by="system:scheduler")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("workflow scheduler error")

    def start_scheduler(self) -> None:
        if not self._scheduler:
            self._scheduler = asyncio.create_task(self._schedule_loop(), name="workflow-scheduler")

    async def stop(self) -> None:
        if self._scheduler:
            self._scheduler.cancel()
            await asyncio.gather(self._scheduler, return_exceptions=True)
            self._scheduler = None

    # ====================================================================== agent tools

    async def _biz(self, ctx: ToolContext) -> Business:
        b = await self.business_for_namespace(ctx.namespace_id)
        if not b:
            raise AiosError("this task is not running inside a business")
        return b

    async def _t_profile(self, ctx: ToolContext, a: dict) -> str:
        b = await self._biz(ctx)
        await self.update_business(b.id, **{key: a.get(key) for key in ("mission", "business_model", "strategy", "stage")})
        return "Business profile updated: " + ", ".join(key for key in ("mission", "business_model", "strategy", "stage") if a.get(key))

    async def _t_kpi_define(self, ctx: ToolContext, a: dict) -> str:
        b = await self._biz(ctx)
        kpi = await self.define_kpi(b.id, key=a["key"], name=a["name"], unit=a.get("unit", ""), target=a.get("target"),
                                    direction=a.get("direction", "up"))
        return f"KPI '{kpi.key}' defined."

    async def _t_kpi_record(self, ctx: ToolContext, a: dict) -> str:
        b = await self._biz(ctx)
        await self.record_kpi(b.id, a["key"], a["value"], note=a.get("note", ""), recorded_by=ctx.actor)
        return f"Recorded {a['key']} = {a['value']}."

    async def _t_ledger(self, ctx: ToolContext, a: dict) -> str:
        b = await self._biz(ctx)
        e = await self.record_ledger(b.id, kind=a["kind"], amount=a["amount"], category=a.get("category", "general"),
                                     memo=a.get("memo", ""), recorded_by=ctx.actor)
        return f"Ledger entry {e.id}: {e.kind} {e.amount:.2f} {b.currency} ({e.category})."

    async def _t_crm_list(self, ctx: ToolContext, a: dict) -> str:
        b = await self._biz(ctx)
        rows = await self.customers(b.id, a.get("stage"))
        return "\n".join(f"- {c.name} <{c.email}> [{c.stage}] {c.company} (id {c.id}) {c.notes[:120]}" for c in rows[:200]) or "(no customers yet)"

    async def _t_crm_upsert(self, ctx: ToolContext, a: dict) -> str:
        b = await self._biz(ctx)
        c = await self.upsert_customer(b.id, name=a["name"], email=a.get("email", ""), company=a.get("company", ""),
                                       stage=a.get("stage", "lead"), notes=a.get("notes", ""), customer_id=a.get("customer_id"))
        return f"Customer saved: {c.name} [{c.stage}] (id {c.id})."

    async def _t_crm_delete(self, ctx: ToolContext, a: dict) -> str:
        b = await self._biz(ctx)
        await self.delete_customer(b.id, a["customer_id"])
        return "Customer record deleted."

    async def _t_project_create(self, ctx: ToolContext, a: dict) -> str:
        b = await self._biz(ctx)
        async with self.k.db.session() as s:
            n = (await s.execute(select(func.count()).select_from(Project).where(
                Project.business_id == b.id, Project.status == "active", Project.created_by.like("agent:%")))).scalar_one()
        if n >= MAX_AGENT_OPENED_PROJECTS:
            raise AiosError("this business already has the maximum number of agent-opened active projects")
        p = await self.create_project(name=a["name"], kind=a.get("kind", "software"), description=a.get("description", ""),
                                      requirements=a.get("requirements", ""), business_id=b.id, created_by=ctx.actor,
                                      kickoff=a.get("start", True))
        return f"Project '{p.name}' opened (id {p.id})" + ("; its delivery objective is being planned." if a.get("start", True) else ".")

    def register_tools(self, registry) -> None:
        st, num = {"type": "string"}, {"type": "number"}

        def add(name, desc, props, required, handler, category):
            registry.register(Tool(name, desc, schema(props, required), handler, TOOL_PERMS[name], category, group="business",
                                   layer="ecosystem"))

        add("business_profile_update", "Record the business's mission, business model, strategy or stage in its profile.",
            {"mission": st, "business_model": st, "strategy": st, "stage": {"type": "string", "enum": ["idea", "validation", "building", "operating"]}},
            [], self._t_profile, "write_internal")
        add("kpi_define", "Define (or update) a KPI the business is run on.",
            {"key": st, "name": st, "unit": st, "target": num, "direction": {"type": "string", "enum": ["up", "down"]}}, ["key", "name"],
            self._t_kpi_define, "write_internal")
        add("kpi_record", "Record a new measured value for a KPI.", {"key": st, "value": num, "note": st}, ["key", "value"],
            self._t_kpi_record, "write_internal")
        add("ledger_record", "Record revenue or an expense in the business ledger. Recording an expense commits the business "
            "to that spend and requires human approval.",
            {"kind": {"type": "string", "enum": ["revenue", "expense"]}, "amount": {"type": "number", "exclusiveMinimum": 0},
             "category": st, "memo": st}, ["kind", "amount"], self._t_ledger,
            lambda a: "financial" if a.get("kind") == "expense" else "write_internal")
        add("crm_list_customers", "List the business's leads and customers.",
            {"stage": {"type": "string", "enum": ["lead", "qualified", "customer", "churned"]}}, [], self._t_crm_list, "read")
        add("crm_upsert_customer", "Create or update a lead or customer record.",
            {"name": st, "email": st, "company": st, "stage": {"type": "string", "enum": ["lead", "qualified", "customer", "churned"]},
             "notes": st, "customer_id": st}, ["name"], self._t_crm_upsert, "write_internal")
        add("crm_delete_customer", "Permanently delete a customer record (personal data).", {"customer_id": st}, ["customer_id"],
            self._t_crm_delete, "sensitive_data")
        add("project_create", "Open a new project in this business. By default its delivery objective starts immediately.",
            {"name": st, "kind": {"type": "string", "enum": PROJECT_KINDS}, "description": st, "requirements": st,
             "start": {"type": "boolean"}}, ["name"], self._t_project_create, "write_internal")


def _kind_of(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in {".md", ".txt", ".rst", ".pdf", ".docx"}:
        return "document"
    if ext in {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".sh", ".sql", ".c", ".cpp", ".mjs"}:
        return "code"
    if ext in {".html", ".css", ".svg"}:
        return "web"
    if ext in {".json", ".yaml", ".yml", ".toml", ".csv", ".xml", ".env"}:
        return "data"
    return "file"


def install(kernel) -> Ecosystem:
    """Attach Layer 3. Requires Layer 2 (``aios_workforce.install``) to be installed first."""
    if "workforce" not in kernel.extensions:
        raise RuntimeError("install aios_workforce before aios_ecosystem")
    eco = Ecosystem(kernel)
    eco.register_tools(kernel.tools)
    kernel.events.subscribe("task.completed", eco._on_task_completed)
    kernel.events.subscribe("agent.created", eco._on_agent_created)
    kernel.extensions["ecosystem"] = eco
    return eco
