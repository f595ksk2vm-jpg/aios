"""Layer 3 API: businesses, projects, workspaces, deliverables, finance, KPIs, customers, workflows."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from aios_ecosystem.plugin import PROJECT_KINDS
from aios_kernel.security.auth import Principal

from ..deps import dicts, get_kernel, require

router = APIRouter(prefix="/api/v1")


def eco(k):
    return k.extensions["ecosystem"]


class BusinessBody(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    idea: str = Field(min_length=5)
    org_preset: str = "lean"
    autonomy: str = "supervised"
    compute: str = "any"
    models: str = "any"
    monthly_budget_usd: float | None = Field(default=None, ge=0)
    auto_provision: bool = True
    kickoff: bool = True


class BusinessPatch(BaseModel):
    name: str | None = None
    idea: str | None = None
    mission: str | None = None
    business_model: str | None = None
    strategy: str | None = None
    stage: str | None = None


class ProjectBody(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    kind: str = "software"
    description: str = ""
    requirements: str = ""
    business_id: str | None = None
    kickoff: bool = True
    owner_agent_id: str | None = None


class ProjectPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    requirements: str | None = None
    status: str | None = None


class LedgerBody(BaseModel):
    kind: str
    amount: float = Field(gt=0)
    category: str = "general"
    memo: str = ""
    date: dt.date | None = None


class KpiBody(BaseModel):
    key: str
    name: str
    unit: str = ""
    target: float | None = None
    direction: str = "up"


class KpiValue(BaseModel):
    value: float
    note: str = ""


class CustomerBody(BaseModel):
    name: str
    email: str = ""
    company: str = ""
    stage: str = "lead"
    notes: str = ""
    customer_id: str | None = None


class WorkflowBody(BaseModel):
    namespace_id: str
    name: str
    objective_title: str
    objective_description: str = ""
    interval_minutes: int | None = None


@router.get("/businesses")
async def businesses(p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    out = []
    for b in await eco(k).businesses():
        o = await eco(k).overview(b.id)
        out.append({**b.to_dict(), "agents": o["agents"], "projects": o["projects"], "tasks": o["tasks"],
                    "objectives": o["objectives"], "finance": {key: o["finance"][key] for key in ("revenue", "expense", "net")},
                    "ai_cost_usd": o["ai_cost_usd"], "compute": o["namespace"]["compute"], "models": o["namespace"]["models"]})
    return {"businesses": out}


@router.post("/businesses")
async def create_business(body: BusinessBody, p: Principal = Depends(require("business:manage")), k=Depends(get_kernel)):
    """Describe an idea; AI-OS creates the organization, staffs it and starts the founding objective."""
    b = await eco(k).create_business(created_by=p.actor, **body.model_dump())
    return b.to_dict()


@router.get("/businesses/{business_id}")
async def business(business_id: str, p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    e = eco(k)
    o = await e.overview(business_id)
    b = await e.business(business_id)
    tree = await e.namespace_tree(b)
    o["projects_list"] = [{**pr.to_dict(), "progress": await e.project_progress(pr)} for pr in await e.projects(business_id)]
    o["deliverables_list"] = dicts((await e.deliverables(tree))[:100])
    o["workflows"] = dicts(await e.workflows(b.namespace_id))
    o["budgets"] = [x for x in await k.budget.budgets() if x["scope_kind"] == "namespace" and x["scope_id"] in tree]
    return o


@router.patch("/businesses/{business_id}")
async def patch_business(business_id: str, body: BusinessPatch, p: Principal = Depends(require("business:manage")), k=Depends(get_kernel)):
    return (await eco(k).update_business(business_id, **body.model_dump(exclude_unset=True))).to_dict()


@router.post("/businesses/{business_id}/status/{status}")
async def business_status(business_id: str, status: str, p: Principal = Depends(require("business:manage")), k=Depends(get_kernel)):
    return (await eco(k).set_business_status(business_id, status, p.actor)).to_dict()


@router.get("/businesses/{business_id}/ledger")
async def ledger(business_id: str, p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    return {"entries": dicts(await eco(k).ledger(business_id)), "summary": await eco(k).finance_summary(business_id)}


@router.post("/businesses/{business_id}/ledger")
async def add_ledger(business_id: str, body: LedgerBody, p: Principal = Depends(require("business:manage")), k=Depends(get_kernel)):
    ts = dt.datetime.combine(body.date, dt.time(12)) if body.date else None
    e = await eco(k).record_ledger(business_id, kind=body.kind, amount=body.amount, category=body.category, memo=body.memo,
                                   recorded_by=p.actor, ts=ts)
    await k.audit.record(p.actor, "ledger.record", resource=e.id, detail={"kind": e.kind, "amount": e.amount})
    return e.to_dict()


@router.get("/businesses/{business_id}/kpis")
async def kpis(business_id: str, p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    return {"kpis": await eco(k).kpis(business_id)}


@router.put("/businesses/{business_id}/kpis")
async def define_kpi(business_id: str, body: KpiBody, p: Principal = Depends(require("business:manage")), k=Depends(get_kernel)):
    return (await eco(k).define_kpi(business_id, **body.model_dump())).to_dict()


@router.post("/businesses/{business_id}/kpis/{key}")
async def record_kpi(business_id: str, key: str, body: KpiValue, p: Principal = Depends(require("business:manage")), k=Depends(get_kernel)):
    await eco(k).record_kpi(business_id, key, body.value, note=body.note, recorded_by=p.actor)
    return {"ok": True}


@router.get("/businesses/{business_id}/customers")
async def customers(business_id: str, p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    return {"customers": dicts(await eco(k).customers(business_id))}


@router.put("/businesses/{business_id}/customers")
async def upsert_customer(business_id: str, body: CustomerBody, p: Principal = Depends(require("business:manage")), k=Depends(get_kernel)):
    return (await eco(k).upsert_customer(business_id, **body.model_dump())).to_dict()


@router.delete("/businesses/{business_id}/customers/{customer_id}")
async def delete_customer(business_id: str, customer_id: str, p: Principal = Depends(require("business:manage")), k=Depends(get_kernel)):
    await eco(k).delete_customer(business_id, customer_id)
    await k.audit.record(p.actor, "customer.delete", resource=customer_id)
    return {"ok": True}


# --- projects --------------------------------------------------------------------------

@router.get("/projects")
async def projects(business_id: str | None = None, p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    e = eco(k)
    names = {b.id: b.name for b in await e.businesses(include_archived=True)}
    return {"projects": [{**pr.to_dict(), "business_name": names.get(pr.business_id), "progress": await e.project_progress(pr)}
                         for pr in await e.projects(business_id)], "kinds": PROJECT_KINDS}


@router.post("/projects")
async def create_project(body: ProjectBody, p: Principal = Depends(require("business:manage")), k=Depends(get_kernel)):
    pr = await eco(k).create_project(created_by=p.actor, **body.model_dump())
    await k.audit.record(p.actor, "project.create", resource=pr.id, namespace_id=pr.namespace_id, detail={"name": pr.name})
    return pr.to_dict()


@router.get("/projects/{project_id}")
async def project(project_id: str, p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    e = eco(k)
    pr = await e.project(project_id)
    tasks = await k.orchestrator.list_tasks(namespace_ids=[pr.namespace_id], limit=500)
    return {"project": pr.to_dict(), "progress": await e.project_progress(pr), "tasks": dicts(sorted(tasks, key=lambda t: t.created_at)),
            "deliverables": dicts(await e.deliverables([pr.namespace_id])), "history": await e.workspace_history(pr.namespace_id),
            "workflows": dicts(await e.workflows(pr.namespace_id))}


@router.patch("/projects/{project_id}")
async def patch_project(project_id: str, body: ProjectPatch, p: Principal = Depends(require("business:manage")), k=Depends(get_kernel)):
    return (await eco(k).update_project(project_id, p.actor, **body.model_dump(exclude_unset=True))).to_dict()


# --- workspaces (files of a business or project namespace) ------------------------------------

@router.get("/workspaces/{namespace_id}/files")
async def files(namespace_id: str, path: str = ".", p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    await k.namespaces.get(namespace_id)
    return {"path": path, "entries": eco(k).workspace_files(namespace_id, path)}


@router.get("/workspaces/{namespace_id}/file")
async def file(namespace_id: str, path: str, p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    await k.namespaces.get(namespace_id)
    return eco(k).workspace_file(namespace_id, path)


@router.get("/workspaces/{namespace_id}/history")
async def history(namespace_id: str, p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    await k.namespaces.get(namespace_id)
    return {"commits": await eco(k).workspace_history(namespace_id)}


# --- workflows -------------------------------------------------------------------------

@router.get("/workflows")
async def workflows(namespace_id: str | None = None, p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    return {"workflows": dicts(await eco(k).workflows(namespace_id))}


@router.post("/workflows")
async def create_workflow(body: WorkflowBody, p: Principal = Depends(require("business:manage")), k=Depends(get_kernel)):
    return (await eco(k).create_workflow(created_by=p.actor, **body.model_dump())).to_dict()


@router.patch("/workflows/{workflow_id}")
async def patch_workflow(workflow_id: str, body: dict, p: Principal = Depends(require("business:manage")), k=Depends(get_kernel)):
    return (await eco(k).update_workflow(workflow_id, **body)).to_dict()


@router.delete("/workflows/{workflow_id}")
async def delete_workflow(workflow_id: str, p: Principal = Depends(require("business:manage")), k=Depends(get_kernel)):
    await eco(k).delete_workflow(workflow_id)
    return {"ok": True}


@router.post("/workflows/{workflow_id}/run")
async def run_workflow(workflow_id: str, body: dict | None = None, p: Principal = Depends(require("tasks:run")), k=Depends(get_kernel)):
    obj = await eco(k).run_workflow(workflow_id, by=p.actor, namespace_id=(body or {}).get("namespace_id"))
    return obj.to_dict()
