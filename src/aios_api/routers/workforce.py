"""Layer 2 API: agent directory, configuration, factory, org charts, communication, performance."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from aios_kernel.models import ROOT_NS
from aios_kernel.security.auth import Principal
from aios_workforce.templates import DEPARTMENTS, KITS, ORG_PRESETS

from ..deps import dicts, get_kernel, require

router = APIRouter(prefix="/api/v1")


def wf(k):
    return k.extensions["workforce"]


class AgentBody(BaseModel):
    namespace_id: str = ROOT_NS
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=80)
    title: str = ""
    department: str = ""
    manager_id: str | None = None
    system_prompt: str = ""
    model: dict | None = None
    tools: list[str] = []
    permissions: list[str] = []
    autonomy: str = "supervised"
    exec_env: str = "any"


class AgentPatch(BaseModel):
    name: str | None = None
    title: str | None = None
    department: str | None = None
    manager_id: str | None = None
    system_prompt: str | None = None
    model: dict | None = None
    tools: list[str] | None = None
    permissions: list[str] | None = None
    autonomy: str | None = None
    exec_env: str | None = None


class FromTemplate(BaseModel):
    namespace_id: str = ROOT_NS
    template: str
    name: str | None = None
    manager_id: str | None = None
    autonomy: str | None = None
    model: dict | None = None


class DepartmentBody(BaseModel):
    namespace_id: str = ROOT_NS
    department: str
    roles: list[str] | None = None
    autonomy: str | None = None


class OrgBody(BaseModel):
    namespace_id: str
    preset: str = "lean"
    autonomy: str | None = None


@router.get("/agents")
async def agents(namespace_id: str | None = None, include_deactivated: bool = False,
                 p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    ns = [namespace_id] + await k.namespaces.descendants(namespace_id) if namespace_id else None
    rows = await k.agents.list(namespace_ids=ns)
    return {"agents": [a.to_dict() for a in rows if include_deactivated or a.status != "deactivated"]}


@router.post("/agents")
async def create_agent(body: AgentBody, p: Principal = Depends(require("workforce:manage")), k=Depends(get_kernel)):
    a = await k.agents.create(created_by=p.actor, **body.model_dump())
    await k.audit.record(p.actor, "agent.create", resource=a.id, namespace_id=a.namespace_id,
                         detail={"permissions": a.permissions, "autonomy": a.autonomy})
    await k.events.emit("agent.created", f"{a.name} joined as {a.title}", namespace_id=a.namespace_id, actor=p.actor, subject=a.id,
                        data={"agent_id": a.id, "role": a.role, "department": a.department})
    return (await k.agents.get(a.id)).to_dict()


@router.get("/agents/{agent_id}")
async def agent(agent_id: str, p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    a = await k.agents.get(agent_id)
    reports = await k.agents.reports(agent_id)
    tasks = await k.orchestrator.list_tasks(agent_id=agent_id, limit=40)
    return {"agent": a.to_dict(), "manager": (await k.agents.get(a.manager_id)).to_dict() if a.manager_id else None,
            "reports": dicts(reports), "performance": await wf(k).performance(agent_id), "recent_tasks": dicts(tasks),
            "messages": dicts(await k.messaging.list(agent_id=agent_id, limit=40))}


@router.patch("/agents/{agent_id}")
async def patch_agent(agent_id: str, body: AgentPatch, p: Principal = Depends(require("workforce:manage")), k=Depends(get_kernel)):
    """The only way an agent's permissions, tools or autonomy change after creation — and it requires a human."""
    fields = body.model_dump(exclude_unset=True)
    before = await k.agents.get(agent_id)
    a = await k.agents.update(agent_id, **fields)
    sensitive = {key: {"from": getattr(before, key), "to": fields[key]} for key in ("permissions", "tools", "autonomy") if key in fields}
    await k.audit.record(p.actor, "agent.update", resource=agent_id, namespace_id=a.namespace_id,
                         detail={"fields": sorted(fields), **sensitive})
    return a.to_dict()


@router.post("/agents/{agent_id}/status/{status}")
async def agent_status(agent_id: str, status: str, p: Principal = Depends(require("workforce:manage")), k=Depends(get_kernel)):
    a = await k.agents.set_status(agent_id, status)
    await k.audit.record(p.actor, f"agent.{status}", resource=agent_id, namespace_id=a.namespace_id)
    await k.events.emit(f"agent.status", f"{a.name} is now {status}", namespace_id=a.namespace_id, actor=p.actor, subject=a.id,
                        level="warn" if status != "active" else "info")
    if status == "active":
        await k.orchestrator.resume_halted()
    return a.to_dict()


@router.post("/agents/{agent_id}/duplicate")
async def duplicate(agent_id: str, body: dict | None = None, p: Principal = Depends(require("workforce:manage")), k=Depends(get_kernel)):
    body = body or {}
    a = await k.agents.duplicate(agent_id, name=body.get("name"), namespace_id=body.get("namespace_id"), created_by=p.actor)
    await k.audit.record(p.actor, "agent.duplicate", resource=a.id, namespace_id=a.namespace_id, detail={"source": agent_id})
    return a.to_dict()


@router.get("/workforce/templates")
async def templates(p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    return {"templates": list((await wf(k).factory.templates()).values()), "departments": DEPARTMENTS,
            "kits": {name: {"tools": t, "permissions": perms} for name, (t, perms) in KITS.items()}, "presets": ORG_PRESETS}


@router.put("/workforce/templates")
async def save_template(body: dict, p: Principal = Depends(require("workforce:manage")), k=Depends(get_kernel)):
    tpl = await wf(k).factory.save_template(body, p.actor)
    await k.audit.record(p.actor, "template.save", resource=tpl["key"])
    return tpl


@router.delete("/workforce/templates/{key}")
async def delete_template(key: str, p: Principal = Depends(require("workforce:manage")), k=Depends(get_kernel)):
    await wf(k).factory.delete_template(key)
    return {"ok": True}


@router.post("/workforce/agents")
async def from_template(body: FromTemplate, p: Principal = Depends(require("workforce:manage")), k=Depends(get_kernel)):
    a = await wf(k).factory.create_from_template(body.namespace_id, body.template, name=body.name, manager_id=body.manager_id,
                                                 autonomy=body.autonomy, model=body.model, created_by=p.actor)
    return (await k.agents.get(a.id)).to_dict()


@router.post("/workforce/departments")
async def create_department(body: DepartmentBody, p: Principal = Depends(require("workforce:manage")), k=Depends(get_kernel)):
    created = await wf(k).factory.create_department(body.namespace_id, body.department, roles=body.roles, autonomy=body.autonomy,
                                                    created_by=p.actor)
    await wf(k).factory.relink(body.namespace_id)
    return {"created": dicts(created)}


@router.post("/workforce/org")
async def create_org(body: OrgBody, p: Principal = Depends(require("workforce:manage")), k=Depends(get_kernel)):
    created = await wf(k).factory.create_org(body.namespace_id, body.preset, autonomy=body.autonomy, created_by=p.actor)
    return {"created": dicts(created)}


@router.get("/workforce/org-chart")
async def org_chart(namespace_id: str = ROOT_NS, p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    chart = await wf(k).factory.org_chart(namespace_id)
    chart["department_records"] = dicts(await wf(k).factory.departments([namespace_id]))
    return chart


@router.get("/workforce/performance")
async def performance(namespace_id: str | None = None, p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    ns = [namespace_id] + await k.namespaces.descendants(namespace_id) if namespace_id else None
    return {"agents": await wf(k).leaderboard(ns)}


@router.get("/messages")
async def messages(namespace_id: str | None = None, agent_id: str | None = None, limit: int = 100,
                   p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    ns = [namespace_id] + await k.namespaces.descendants(namespace_id) if namespace_id else None
    return {"messages": dicts(await k.messaging.list(namespace_ids=ns, agent_id=agent_id, limit=min(limit, 500)))}


class HumanMessage(BaseModel):
    to_agent: str
    subject: str = ""
    body: str = Field(min_length=1)


@router.post("/messages")
async def send_message(body: HumanMessage, p: Principal = Depends(require("tasks:run")), k=Depends(get_kernel)):
    """A human leaves a note in an agent's inbox; the agent reads it when it next starts a task."""
    a = await k.agents.get(body.to_agent)
    m = await k.messaging.send(namespace_id=a.namespace_id, from_agent=None, to_agent=a.id, subject=body.subject,
                               body=f"(from {p.user.name}, a human operator) {body.body}")
    return m.to_dict()
