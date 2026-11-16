"""Layer 1 API: models, tools & integrations, memory, and the work queue (objectives, tasks, runs, approvals)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from aios_kernel.errors import NotFound
from aios_kernel.models import ROOT_NS, Run
from aios_kernel.security import policy
from aios_kernel.security.auth import Principal

from ..deps import dicts, get_kernel, require

router = APIRouter(prefix="/api/v1")


# --- models ----------------------------------------------------------------------------

class ProviderBody(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,58}$")
    kind: str
    location: str = "cloud"
    base_url: str | None = None
    secret_name: str | None = None
    enabled: bool = True
    meta: dict | None = None


class ModelBody(BaseModel):
    provider_id: str
    model_name: str
    display_name: str | None = None
    tier: str = "balanced"
    context_window: int | None = None
    price_in: float | None = None
    price_out: float | None = None
    latency_class: int | None = Field(default=None, ge=1, le=3)
    supports_tools: bool | None = None
    enabled: bool | None = None
    meta: dict | None = None


@router.get("/models")
async def models(p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    secret_names = {s.name for s in await k.secrets.list()}
    providers = [{**pr.to_dict(), "credential_set": (not pr.secret_name) or pr.secret_name in secret_names}
                 for pr in await k.models.providers()]
    return {"providers": providers, "models": dicts(await k.models.catalog())}


@router.put("/models/providers")
async def put_provider(body: ProviderBody, p: Principal = Depends(require("models:manage")), k=Depends(get_kernel)):
    pr = await k.models.upsert_provider(**body.model_dump())
    await k.audit.record(p.actor, "model.provider.put", resource=pr.name, detail={"kind": pr.kind, "location": pr.location,
                                                                                   "base_url": pr.base_url, "enabled": pr.enabled})
    return pr.to_dict()


@router.delete("/models/providers/{provider_id}")
async def delete_provider(provider_id: str, p: Principal = Depends(require("models:manage")), k=Depends(get_kernel)):
    await k.models.delete_provider(provider_id)
    await k.audit.record(p.actor, "model.provider.delete", resource=provider_id)
    return {"ok": True}


@router.post("/models/providers/{provider_id}/health")
async def provider_health(provider_id: str, p: Principal = Depends(require("models:manage")), k=Depends(get_kernel)):
    return await k.models.health(provider_id)


@router.post("/models/providers/{provider_id}/discover")
async def provider_discover(provider_id: str, p: Principal = Depends(require("models:manage")), k=Depends(get_kernel)):
    return {"models": await k.models.discover(provider_id)}


@router.put("/models/catalog")
async def put_model(body: ModelBody, p: Principal = Depends(require("models:manage")), k=Depends(get_kernel)):
    m = await k.models.upsert_model(**body.model_dump())
    await k.audit.record(p.actor, "model.put", resource=m.model_name)
    return m.to_dict()


@router.delete("/models/catalog/{model_id}")
async def delete_model(model_id: str, p: Principal = Depends(require("models:manage")), k=Depends(get_kernel)):
    await k.models.delete_model(model_id)
    return {"ok": True}


class RouteBody(BaseModel):
    model: dict = {}
    namespace_id: str = ROOT_NS


@router.post("/models/route")
async def route_preview(body: RouteBody, p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    """Explain which model a given agent configuration would get in a namespace, and the fallback order."""
    _, placement = await k.namespaces.placement(body.namespace_id)
    cands = await k.models.candidates(body.model, models_placement=placement)
    return {"models_placement": placement, "candidates": [{"ref": c.ref, "tier": c.entry.tier, "location": c.provider.location,
                                                           "price_in": c.entry.price_in, "price_out": c.entry.price_out} for c in cands]}


# --- tools & integrations --------------------------------------------------------------------

@router.get("/tools")
async def tools(p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    return {"tools": [t.describe() for t in k.tools.all()], "integrations": await k.connectors.status(),
            "categories": policy.CATEGORIES, "autonomy_levels": policy.AUTONOMY_LEVELS,
            "policy": {lvl: {c: policy.decide(lvl, c) for c in policy.CATEGORIES} for lvl in policy.AUTONOMY_LEVELS}}


class ConnectorBody(BaseModel):
    name: str
    base_url: str
    secret_name: str | None = None
    auth: str = "Bearer {secret}"
    auth_header: str = "Authorization"
    description: str = ""
    group: str = "custom"


@router.post("/integrations")
async def add_connector(body: ConnectorBody, p: Principal = Depends(require("system:control")), k=Depends(get_kernel)):
    c = await k.connectors.add(body.model_dump())
    await k.audit.record(p.actor, "integration.add", resource=c["name"], detail={"base_url": c["base_url"]})
    return c


@router.delete("/integrations/{name}")
async def remove_connector(name: str, p: Principal = Depends(require("system:control")), k=Depends(get_kernel)):
    await k.connectors.remove(name)
    await k.audit.record(p.actor, "integration.remove", resource=name)
    return {"ok": True}


# --- memory ----------------------------------------------------------------------------

class MemoryBody(BaseModel):
    namespace_id: str = ROOT_NS
    title: str = ""
    content: str = Field(min_length=1)
    kind: str = "note"


class DocumentBody(BaseModel):
    namespace_id: str = ROOT_NS
    title: str
    content: str = Field(min_length=1)
    source: str = ""


@router.get("/memory/search")
async def memory_search(q: str, namespace_id: str = ROOT_NS, k_: int = 8, strict: bool = False,
                        p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    return {"results": await k.memory.search(q, namespace_id=namespace_id, k=min(k_, 50), strict_namespace=strict),
            "embedder": k.memory.embedder.id}


@router.get("/memory")
async def memory_recent(namespace_id: str | None = None, p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    ns = [namespace_id] + await k.namespaces.descendants(namespace_id) if namespace_id else None
    return {"items": dicts(await k.memory.recent(ns, 150)), "documents": [
        {key: v for key, v in d.to_dict().items() if key != "content"} for d in await k.memory.documents(ns)],
        "embedder": k.memory.embedder.id}


@router.post("/memory")
async def memory_add(body: MemoryBody, p: Principal = Depends(require("memory:write")), k=Depends(get_kernel)):
    await k.namespaces.get(body.namespace_id)
    items = await k.memory.add(namespace_id=body.namespace_id, content=body.content, title=body.title, kind=body.kind,
                               meta={"added_by": p.actor})
    return {"stored": len(items)}


@router.post("/memory/documents")
async def document_add(body: DocumentBody, p: Principal = Depends(require("memory:write")), k=Depends(get_kernel)):
    await k.namespaces.get(body.namespace_id)
    doc = await k.memory.add_document(namespace_id=body.namespace_id, title=body.title, content=body.content, source=body.source,
                                      meta={"added_by": p.actor})
    return {key: v for key, v in doc.to_dict().items() if key != "content"}


@router.delete("/memory/documents/{document_id}")
async def document_delete(document_id: str, p: Principal = Depends(require("memory:write")), k=Depends(get_kernel)):
    await k.memory.delete_document(document_id)
    return {"ok": True}


@router.delete("/memory/{item_id}")
async def memory_delete(item_id: str, p: Principal = Depends(require("memory:write")), k=Depends(get_kernel)):
    await k.memory.delete(item_id)
    return {"ok": True}


# --- objectives, tasks, runs -----------------------------------------------------------------

class ObjectiveBody(BaseModel):
    namespace_id: str = ROOT_NS
    title: str = Field(min_length=3, max_length=255)
    description: str = ""
    owner_agent_id: str | None = None


class TaskBody(BaseModel):
    namespace_id: str = ROOT_NS
    title: str = Field(min_length=3, max_length=255)
    description: str = ""
    agent_id: str | None = None
    role: str = ""
    priority: int = Field(default=5, ge=1, le=9)
    needs_review: bool = False


async def _tree(k, namespace_id: str | None) -> list[str] | None:
    return [namespace_id] + await k.namespaces.descendants(namespace_id) if namespace_id else None


@router.get("/objectives")
async def objectives(namespace_id: str | None = None, p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    return {"objectives": dicts(await k.orchestrator.list_objectives(await _tree(k, namespace_id)))}


@router.post("/objectives")
async def create_objective(body: ObjectiveBody, p: Principal = Depends(require("tasks:run")), k=Depends(get_kernel)):
    obj = await k.orchestrator.create_objective(namespace_id=body.namespace_id, title=body.title, description=body.description,
                                                created_by=p.actor, owner_agent_id=body.owner_agent_id)
    await k.audit.record(p.actor, "objective.create", resource=obj.id, namespace_id=body.namespace_id, detail={"title": body.title})
    return obj.to_dict()


@router.get("/objectives/{objective_id}")
async def objective(objective_id: str, p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    obj = await k.orchestrator.get_objective(objective_id)
    tasks = await k.orchestrator.list_tasks(objective_id=objective_id)
    return {"objective": obj.to_dict(), "tasks": dicts(sorted(tasks, key=lambda t: t.created_at))}


@router.post("/objectives/{objective_id}/cancel")
async def cancel_objective(objective_id: str, p: Principal = Depends(require("tasks:run")), k=Depends(get_kernel)):
    obj = await k.orchestrator.cancel_objective(objective_id)
    await k.audit.record(p.actor, "objective.cancel", resource=objective_id, namespace_id=obj.namespace_id)
    return obj.to_dict()


@router.get("/tasks")
async def tasks(namespace_id: str | None = None, agent_id: str | None = None, status: str | None = None, limit: int = 200,
                p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    rows = await k.orchestrator.list_tasks(namespace_ids=await _tree(k, namespace_id), agent_id=agent_id,
                                           statuses=status.split(",") if status else None, limit=min(limit, 1000))
    return {"tasks": dicts(rows)}


@router.post("/tasks")
async def create_task(body: TaskBody, p: Principal = Depends(require("tasks:run")), k=Depends(get_kernel)):
    """Hand a single task directly to an agent (or a role), outside any objective."""
    if body.agent_id:
        await k.agents.get(body.agent_id)
    t = await k.orchestrator.create_task(namespace_id=body.namespace_id, title=body.title, description=body.description,
                                         agent_id=body.agent_id, role=body.role, priority=body.priority,
                                         meta={"needs_review": body.needs_review, "created_by": p.actor})
    await k.audit.record(p.actor, "task.create", resource=t.id, namespace_id=body.namespace_id, detail={"title": body.title})
    return t.to_dict()


@router.get("/tasks/{task_id}")
async def task(task_id: str, p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    t = await k.orchestrator.get_task(task_id)
    runs = await k.orchestrator.runs_for(task_id)
    children = [c for c in await k.orchestrator.list_tasks(namespace_ids=[t.namespace_id], limit=1000) if c.parent_task_id == task_id]
    return {"task": t.to_dict(), "children": dicts(children),
            "runs": [{key: v for key, v in r.to_dict().items() if key != "messages"} | {"message_count": len(r.messages)} for r in runs]}


@router.get("/runs/{run_id}")
async def run(run_id: str, p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    async with k.db.session() as s:
        r = await s.get(Run, run_id)
    if not r:
        raise NotFound("run not found")
    out = r.to_dict()
    # Provider-native payloads are for replay, not for display.
    out["messages"] = [{key: v for key, v in m.items() if key != "raw"} for m in r.messages]
    return out


@router.post("/tasks/{task_id}/retry")
async def retry_task(task_id: str, p: Principal = Depends(require("tasks:run")), k=Depends(get_kernel)):
    t = await k.orchestrator.retry_task(task_id)
    await k.audit.record(p.actor, "task.retry", resource=task_id, namespace_id=t.namespace_id)
    return t.to_dict()


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, p: Principal = Depends(require("tasks:run")), k=Depends(get_kernel)):
    t = await k.orchestrator.cancel_task(task_id)
    await k.audit.record(p.actor, "task.cancel", resource=task_id, namespace_id=t.namespace_id)
    return t.to_dict()


# --- approvals -------------------------------------------------------------------------

class Decision(BaseModel):
    approve: bool
    reason: str = ""


@router.get("/approvals")
async def approvals(status: str | None = None, p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    return {"approvals": dicts(await k.approvals.list(status))}


@router.post("/approvals/{approval_id}/decide")
async def decide(approval_id: str, body: Decision, p: Principal = Depends(require("approvals:decide")), k=Depends(get_kernel)):
    a = await k.approvals.decide(approval_id, approve=body.approve, decided_by=p.user.id, reason=body.reason)
    await k.audit.record(p.actor, "approval.decide", resource=approval_id, namespace_id=a.namespace_id,
                         detail={"decision": a.status, "action": a.action, "category": a.category, "reason": body.reason})
    await k.events.emit("approval.decided", f"{a.action} {a.status} by {p.user.name}", namespace_id=a.namespace_id, actor=p.actor,
                        subject=a.id, level="info" if body.approve else "warn", data={"approval_id": a.id, "status": a.status})
    await k.orchestrator.on_approval_decided(a.run_id)
    return a.to_dict()
