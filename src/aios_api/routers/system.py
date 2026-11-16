"""Layer 1 API: identity, system status, events, audit, usage, budgets, secrets, namespaces, emergency control."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from aios_kernel import __version__, backup
from aios_kernel.db import utcnow
from aios_kernel.errors import AiosError, Conflict, PermissionDenied, Unauthenticated
from aios_kernel.models import ROOT_NS, Agent, Approval, Event, Objective, Task, UsageRecord, WorkerNode
from aios_kernel.security.auth import ROLE_PERMISSIONS, ROLES, Principal

from ..deps import current, dicts, get_kernel, require

router = APIRouter(prefix="/api/v1")
_login_attempts: dict[str, list[float]] = {}


class Credentials(BaseModel):
    email: str
    password: str


class NewUser(Credentials):
    name: str = ""
    role: str = "viewer"


@router.get("/auth/state")
async def auth_state(k=Depends(get_kernel)):
    return {"needs_setup": await k.auth.user_count() == 0, "version": __version__}


@router.post("/auth/setup")
async def auth_setup(body: NewUser, k=Depends(get_kernel)):
    """Create the first (owner) account. Only possible while no user exists."""
    if await k.auth.user_count() > 0:
        raise Conflict("setup already completed")
    user = await k.auth.create_user(body.email, body.password, name=body.name, role="owner")
    await k.audit.record(f"user:{user.id}", "auth.setup", resource=user.id)
    token, _ = await k.auth.login(body.email, body.password)
    return {"token": token, "user": user.to_dict()}


@router.post("/auth/login")
async def login(body: Credentials, request: Request, k=Depends(get_kernel)):
    key = f"{body.email.lower()}|{request.client.host if request.client else ''}"
    now = time.monotonic()
    recent = [t for t in _login_attempts.get(key, []) if now - t < 60]
    if len(recent) >= 8:
        raise AiosError("too many login attempts; wait a minute")
    _login_attempts[key] = recent + [now]
    try:
        token, user = await k.auth.login(body.email, body.password)
    except Unauthenticated:
        await k.audit.record("anonymous", "auth.login", resource=body.email.lower(), outcome="denied")
        raise
    _login_attempts.pop(key, None)
    await k.audit.record(f"user:{user.id}", "auth.login")
    return {"token": token, "user": user.to_dict(), "permissions": sorted(ROLE_PERMISSIONS[user.role])}


@router.post("/auth/logout")
async def logout(request: Request, p: Principal = Depends(current), k=Depends(get_kernel)):
    await k.auth.revoke(request.headers["authorization"][7:].strip())
    return {"ok": True}


@router.get("/auth/me")
async def me(p: Principal = Depends(current)):
    return {"user": p.user.to_dict(), "permissions": sorted(ROLE_PERMISSIONS[p.user.role])}


@router.post("/auth/api-keys")
async def create_api_key(body: dict, p: Principal = Depends(current), k=Depends(get_kernel)):
    token = await k.auth.create_api_key(p.user.id, str(body.get("name", "api key"))[:100])
    await k.audit.record(p.actor, "auth.api_key.create", detail={"name": body.get("name")})
    return {"token": token, "note": "Shown once. It carries your role's permissions."}


@router.get("/users")
async def users(p: Principal = Depends(require("users:manage")), k=Depends(get_kernel)):
    return {"users": dicts(await k.auth.list_users()), "roles": ROLES}


@router.post("/users")
async def create_user(body: NewUser, p: Principal = Depends(require("users:manage")), k=Depends(get_kernel)):
    if body.role == "owner" and not p.can("system:owner"):
        raise PermissionDenied("only an owner can create another owner")
    user = await k.auth.create_user(body.email, body.password, name=body.name, role=body.role)
    await k.audit.record(p.actor, "user.create", resource=user.id, detail={"role": body.role})
    return user.to_dict()


class UserPatch(BaseModel):
    role: str | None = None
    is_active: bool | None = None
    password: str | None = None


@router.patch("/users/{user_id}")
async def patch_user(user_id: str, body: UserPatch, p: Principal = Depends(require("users:manage")), k=Depends(get_kernel)):
    if body.role == "owner" and not p.can("system:owner"):
        raise PermissionDenied("only an owner can grant the owner role")
    if user_id == p.user.id and (body.is_active is False or (body.role and body.role != p.user.role)):
        raise Conflict("you cannot demote or deactivate yourself")
    user = await k.auth.update_user(user_id, **body.model_dump())
    await k.audit.record(p.actor, "user.update", resource=user_id, detail={"role": body.role, "is_active": body.is_active})
    return user.to_dict()


# --- status ----------------------------------------------------------------------------

@router.get("/system/status")
async def status(p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    month = today.replace(day=1)
    async with k.db.session() as s:
        async def count(model, *where):
            return (await s.execute(select(func.count()).select_from(model).where(*where))).scalar_one()

        async def spend(since):
            return float((await s.execute(select(func.coalesce(func.sum(UsageRecord.cost_usd), 0.0)).where(UsageRecord.ts >= since))).scalar_one())

        counts = {
            "agents_active": await count(Agent, Agent.status == "active"),
            "agents_total": await count(Agent, Agent.status != "deactivated"),
            "objectives_active": await count(Objective, Objective.status.in_(["planning", "running", "finalizing"])),
            "objectives_attention": await count(Objective, Objective.status == "needs_attention"),
            "tasks_running": await count(Task, Task.status == "running"),
            "tasks_queued": await count(Task, Task.status.in_(["ready", "pending"])),
            "tasks_waiting": await count(Task, Task.status.in_(["waiting", "awaiting_approval", "review", "paused"])),
            "tasks_done_today": await count(Task, Task.status == "done", Task.finished_at >= today),
            "tasks_failed_today": await count(Task, Task.status == "failed", Task.finished_at >= today),
            "approvals_pending": await count(Approval, Approval.status == "pending"),
        }
        usage = {"cost_today": round(await spend(today), 4), "cost_month": round(await spend(month), 4),
                 "llm_calls_today": await count(UsageRecord, UsageRecord.ts >= today, UsageRecord.kind == "llm"),
                 "exec_runs_today": await count(UsageRecord, UsageRecord.ts >= today, UsageRecord.kind == "compute")}
        cutoff = utcnow() - dt.timedelta(seconds=45)
        workers = [{**w.to_dict(), "online": w.heartbeat_at >= cutoff} for w in (await s.execute(select(WorkerNode))).scalars()]
        notices = dicts((await s.execute(select(Event).where(Event.level.in_(["warn", "error"])).order_by(Event.id.desc()).limit(15))).scalars())
    return {"version": __version__, "config": k.settings.public(), "emergency_stop": await k.killswitch.state(fresh=True),
            "sandbox": await k.sandbox.info(), "counts": counts, "usage": usage, "queue": await k.queue.stats(),
            "workers": workers, "notifications": notices}


class StopBody(BaseModel):
    reason: str = ""


@router.post("/system/emergency-stop")
async def emergency_stop(body: StopBody, p: Principal = Depends(require("system:control")), k=Depends(get_kernel)):
    return await k.emergency_stop(by=p.actor, reason=body.reason)


@router.post("/system/resume")
async def resume(p: Principal = Depends(require("system:control")), k=Depends(get_kernel)):
    return await k.resume_operations(by=p.actor)


@router.post("/system/backups")
async def make_backup(body: dict | None = None, p: Principal = Depends(require("system:control")), k=Depends(get_kernel)):
    info = await backup.create_backup(k.settings, include_key=bool((body or {}).get("include_key")))
    await k.audit.record(p.actor, "system.backup", resource=info["file"], detail={"includes_master_key": info["includes_master_key"]})
    await k.events.emit("system.backup", f"Backup created: {info['file']}", actor=p.actor)
    return info


@router.get("/system/backups")
async def backups(p: Principal = Depends(require("system:control")), k=Depends(get_kernel)):
    return {"backups": backup.list_backups(k.settings)}


# --- events, audit, usage ------------------------------------------------------------------

@router.get("/events")
async def events(after: int = 0, limit: int = 100, namespace_id: str | None = None, kind: str | None = None,
                 p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    ns = None
    if namespace_id:
        ns = [namespace_id] + await k.namespaces.descendants(namespace_id)
    rows = await k.events.list(after_id=after, limit=min(limit, 500), namespace_ids=ns, kind_prefix=kind, newest_first=not after)
    return {"events": dicts(rows)}


@router.get("/events/stream")
async def event_stream(request: Request, after: int = 0, p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    """Server-sent events. Tails the persisted event log, so it also shows activity from remote workers."""
    async def gen():
        cursor = after
        if not cursor:
            latest = await k.events.list(limit=1, newest_first=True)
            cursor = latest[0].id if latest else 0
        yield ": connected\n\n"
        idle = 0
        while not await request.is_disconnected():
            rows = await k.events.list(after_id=cursor, limit=200)
            for ev in rows:
                cursor = ev.id
                yield f"id: {ev.id}\ndata: {json.dumps(ev.to_dict())}\n\n"
            if rows:
                idle = 0
                continue
            idle += 1
            if idle % 15 == 0:
                yield ": keep-alive\n\n"
            await k.events.wait(1.0)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/audit")
async def audit(limit: int = 200, action: str | None = None, p: Principal = Depends(require("system:control")), k=Depends(get_kernel)):
    return {"entries": dicts(await k.audit.list(min(limit, 1000), action))}


@router.get("/audit/verify")
async def audit_verify(p: Principal = Depends(require("system:control")), k=Depends(get_kernel)):
    return await k.audit.verify()


@router.get("/usage")
async def usage(days: int = 30, namespace_id: str | None = None, p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    ns = [namespace_id] + await k.namespaces.descendants(namespace_id) if namespace_id else None
    return await k.budget.summary(days=min(days, 365), namespace_ids=ns)


class BudgetBody(BaseModel):
    scope_kind: str
    scope_id: str = ""
    period: str = "month"
    limit_usd: float = Field(ge=0)


@router.get("/budgets")
async def budgets(p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    return {"budgets": await k.budget.budgets()}


@router.put("/budgets")
async def set_budget(body: BudgetBody, p: Principal = Depends(require("system:control")), k=Depends(get_kernel)):
    b = await k.budget.set_budget(body.scope_kind, body.scope_id, body.period, body.limit_usd)
    await k.audit.record(p.actor, "budget.set", resource=b.id, detail=body.model_dump())
    return b.to_dict()


@router.delete("/budgets/{budget_id}")
async def delete_budget(budget_id: str, p: Principal = Depends(require("system:control")), k=Depends(get_kernel)):
    await k.budget.delete_budget(budget_id)
    await k.audit.record(p.actor, "budget.delete", resource=budget_id)
    return {"ok": True}


# --- secrets ---------------------------------------------------------------------------

class SecretBody(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{1,99}$")
    value: str = Field(min_length=1)
    namespace_id: str = ROOT_NS


@router.get("/secrets")
async def secrets(p: Principal = Depends(require("secrets:manage")), k=Depends(get_kernel)):
    return {"secrets": dicts(await k.secrets.list())}  # names and metadata only; values are write-only


@router.put("/secrets")
async def put_secret(body: SecretBody, p: Principal = Depends(require("secrets:manage")), k=Depends(get_kernel)):
    await k.namespaces.get(body.namespace_id)
    await k.secrets.put(body.name, body.value, namespace_id=body.namespace_id, created_by=p.actor)
    await k.audit.record(p.actor, "secret.put", resource=body.name, namespace_id=body.namespace_id)
    return {"ok": True}


@router.delete("/secrets/{name}")
async def delete_secret(name: str, namespace_id: str = ROOT_NS, p: Principal = Depends(require("secrets:manage")), k=Depends(get_kernel)):
    await k.secrets.delete(name, namespace_id)
    await k.audit.record(p.actor, "secret.delete", resource=name, namespace_id=namespace_id)
    return {"ok": True}


# --- namespaces & sharing ------------------------------------------------------------------

@router.get("/namespaces")
async def namespaces(p: Principal = Depends(require("read")), k=Depends(get_kernel)):
    return {"namespaces": dicts(await k.namespaces.all()), "grants": dicts(await k.namespaces.grants())}


class PlacementBody(BaseModel):
    compute: str | None = None
    models: str | None = None


@router.patch("/namespaces/{ns_id}")
async def patch_namespace(ns_id: str, body: PlacementBody, p: Principal = Depends(require("system:control")), k=Depends(get_kernel)):
    ns = await k.namespaces.update(ns_id, compute=body.compute, models=body.models)
    await k.audit.record(p.actor, "namespace.placement", resource=ns_id, namespace_id=ns_id, detail=body.model_dump())
    return ns.to_dict()


class GrantBody(BaseModel):
    from_ns: str
    to_ns: str
    resource_kind: str
    resource_id: str | None = None


@router.post("/grants")
async def create_grant(body: GrantBody, p: Principal = Depends(require("system:control")), k=Depends(get_kernel)):
    g = await k.namespaces.grant(body.from_ns, body.to_ns, body.resource_kind, resource_id=body.resource_id, created_by=p.actor)
    await k.audit.record(p.actor, "grant.create", resource=g.id, namespace_id=body.from_ns, detail=body.model_dump())
    await k.events.emit("grant.created", f"{body.resource_kind} shared across organizations", namespace_id=body.from_ns, actor=p.actor,
                        level="warn", data=body.model_dump())
    return g.to_dict()


@router.delete("/grants/{grant_id}")
async def revoke_grant(grant_id: str, p: Principal = Depends(require("system:control")), k=Depends(get_kernel)):
    await k.namespaces.revoke(grant_id)
    await k.audit.record(p.actor, "grant.revoke", resource=grant_id)
    return {"ok": True}
