"""Agent infrastructure: lifecycle, hierarchy, discovery and inter-agent messaging."""
from __future__ import annotations

import re

from sqlalchemy import select

from ..db import Database, new_id, utcnow
from ..errors import Conflict, NotFound, PermissionDenied
from ..models import Agent, AgentMessage
from ..security import policy

EDITABLE = {"name", "title", "department", "manager_id", "system_prompt", "model", "tools", "permissions", "autonomy",
            "exec_env", "meta", "role"}


def norm_role(role: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", role.strip().lower()).strip("_")


class AgentRegistry:
    def __init__(self, db: Database, namespaces, tools):
        self.db = db
        self.namespaces = namespaces
        self.tools = tools

    def _validate(self, *, autonomy: str, exec_env: str, tools: list[str]) -> None:
        if autonomy not in policy.AUTONOMY_LEVELS:
            raise Conflict(f"autonomy must be one of {policy.AUTONOMY_LEVELS}")
        if exec_env not in {"any", "local", "cloud"}:
            raise Conflict("exec_env must be any|local|cloud")
        unknown = [t for t in tools if not self.tools.get(t)]
        if unknown:
            raise Conflict(f"unknown tools: {unknown}")

    async def create(self, *, namespace_id: str, name: str, role: str, title: str = "", department: str = "",
                     manager_id: str | None = None, system_prompt: str = "", model: dict | None = None,
                     tools: list[str] | None = None, permissions: list[str] | None = None, autonomy: str = "supervised",
                     exec_env: str = "any", created_by: str = "system", meta: dict | None = None) -> Agent:
        tools, permissions = list(tools or []), list(permissions or [])
        self._validate(autonomy=autonomy, exec_env=exec_env, tools=tools)
        await self.namespaces.get(namespace_id)
        if manager_id:
            await self.get(manager_id)
        a = Agent(id=new_id("agt"), namespace_id=namespace_id, name=name, role=norm_role(role), title=title or role,
                  department=department, manager_id=manager_id, system_prompt=system_prompt,
                  model=model or {"mode": "auto", "tier": "balanced", "prefer": "balanced"}, tools=tools, permissions=permissions,
                  autonomy=autonomy, exec_env=exec_env, created_by=created_by, meta=meta or {})
        async with self.db.session() as s:
            s.add(a)
        return a

    async def create_by_agent(self, creator: Agent, **spec) -> Agent:
        """Agent-initiated creation with attenuation: the child can never exceed its creator.

        This is the only path by which an agent influences another agent's
        privileges, and it can only narrow them.
        """
        if not policy.has_permission(creator.permissions, "agents.create"):
            raise PermissionDenied("creator lacks agents.create")
        requested = list(spec.get("permissions") or [])
        too_broad = [p for p in requested if not policy.covered_by(p, creator.permissions)]
        if too_broad:
            raise PermissionDenied(f"cannot grant permissions the creator does not hold: {too_broad}")
        if policy.autonomy_rank(spec.get("autonomy", "supervised")) > policy.autonomy_rank(creator.autonomy):
            raise PermissionDenied("cannot create an agent with a higher autonomy level than the creator")
        if spec.get("namespace_id", creator.namespace_id) != creator.namespace_id:
            allowed = set(await self.namespaces.descendants(creator.namespace_id))
            if spec["namespace_id"] not in allowed:
                raise PermissionDenied("agents may only create agents inside their own namespace")
        spec.setdefault("namespace_id", creator.namespace_id)
        spec.setdefault("manager_id", creator.id)
        spec["created_by"] = f"agent:{creator.id}"
        return await self.create(**spec)

    async def get(self, agent_id: str) -> Agent:
        async with self.db.session() as s:
            a = await s.get(Agent, agent_id)
        if not a:
            raise NotFound(f"agent {agent_id} not found")
        return a

    async def list(self, *, namespace_ids: list[str] | None = None, status: str | None = None) -> list[Agent]:
        q = select(Agent).order_by(Agent.created_at)
        if namespace_ids is not None:
            q = q.where(Agent.namespace_id.in_(namespace_ids))
        if status:
            q = q.where(Agent.status == status)
        async with self.db.session() as s:
            return list((await s.execute(q)).scalars())

    async def update(self, agent_id: str, **fields) -> Agent:
        """Reconfigure an agent. Reachable only from the human-authenticated control API."""
        bad = set(fields) - EDITABLE
        if bad:
            raise Conflict(f"fields not editable: {sorted(bad)}")
        async with self.db.session() as s:
            a = await s.get(Agent, agent_id)
            if not a:
                raise NotFound("agent not found")
            self._validate(autonomy=fields.get("autonomy", a.autonomy), exec_env=fields.get("exec_env", a.exec_env),
                           tools=fields.get("tools", a.tools))
            if fields.get("manager_id"):
                # Walk up from the proposed manager; hitting this agent would make a cycle.
                cur, seen = fields["manager_id"], set()
                while cur and cur not in seen:
                    if cur == agent_id:
                        raise Conflict("an agent cannot be in its own management chain")
                    seen.add(cur)
                    m = await s.get(Agent, cur)
                    if not m:
                        raise NotFound("manager not found")
                    cur = m.manager_id
            for k, v in fields.items():
                setattr(a, k, norm_role(v) if k == "role" else v)
            a.updated_at = utcnow()
        return a

    async def set_status(self, agent_id: str, status: str) -> Agent:
        if status not in {"active", "paused", "deactivated"}:
            raise Conflict("status must be active|paused|deactivated")
        async with self.db.session() as s:
            a = await s.get(Agent, agent_id)
            if not a:
                raise NotFound("agent not found")
            a.status, a.updated_at = status, utcnow()
        return a

    async def duplicate(self, agent_id: str, *, name: str | None = None, namespace_id: str | None = None, created_by: str = "system") -> Agent:
        src = await self.get(agent_id)
        return await self.create(namespace_id=namespace_id or src.namespace_id, name=name or f"{src.name} (copy)", role=src.role,
                                 title=src.title, department=src.department,
                                 manager_id=src.manager_id if (namespace_id or src.namespace_id) == src.namespace_id else None,
                                 system_prompt=src.system_prompt, model=dict(src.model), tools=list(src.tools),
                                 permissions=list(src.permissions), autonomy=src.autonomy, exec_env=src.exec_env,
                                 created_by=created_by, meta={**src.meta, "duplicated_from": src.id})

    async def reports(self, agent_id: str) -> list[Agent]:
        async with self.db.session() as s:
            return list((await s.execute(select(Agent).where(Agent.manager_id == agent_id, Agent.status != "deactivated"))).scalars())

    async def usable(self, namespace_id: str) -> list[Agent]:
        """Active agents that may work on tasks in ``namespace_id`` (own tree, ancestors, and explicit grants)."""
        whole, single = await self.namespaces.visible(namespace_id, "agent")
        granted_ids = {rid for _, rid in single}
        async with self.db.session() as s:
            rows = list((await s.execute(select(Agent).where(Agent.status == "active"))).scalars())
        return [a for a in rows if a.namespace_id in whole or a.id in granted_ids]

    async def find_for_role(self, namespace_id: str, role: str) -> Agent | None:
        """Best available agent for a role: exact role key first, then title match; nearest namespace wins."""
        want = norm_role(role)
        if not want:
            return None
        chain = await self.namespaces.chain_ids(namespace_id)
        pool = await self.usable(namespace_id)

        def distance(a: Agent) -> int:
            return chain.index(a.namespace_id) if a.namespace_id in chain else len(chain) + 1

        exact = [a for a in pool if a.role == want or norm_role(a.title) == want]
        loose = [a for a in pool if want in a.role or a.role in want or want in norm_role(a.title)]
        for group in (exact, loose):
            if group:
                return sorted(group, key=distance)[0]
        return None

    async def lead(self, namespace_id: str) -> Agent | None:
        """The most senior agent of the nearest namespace that has any agents (no manager within that namespace)."""
        for ns in await self.namespaces.chain_ids(namespace_id):
            agents = await self.list(namespace_ids=[ns], status="active")
            if not agents:
                continue
            ids = {a.id for a in agents}
            tops = [a for a in agents if not a.manager_id or a.manager_id not in ids]
            rank = {"ceo": 0, "coo": 1, "cto": 2, "project_manager": 3, "orchestrator": 9}
            return sorted(tops or agents, key=lambda a: (rank.get(a.role, 5), a.created_at))[0]
        return None


class Messaging:
    """Durable inter-agent mailbox. Unread messages are placed in front of an agent when its next run starts."""

    def __init__(self, db: Database, events):
        self.db = db
        self.events = events

    async def send(self, *, namespace_id: str, from_agent: str | None, to_agent: str | None, body: str, subject: str = "",
                   kind: str = "message", task_id: str | None = None) -> AgentMessage:
        m = AgentMessage(id=new_id("msg"), namespace_id=namespace_id, from_agent=from_agent, to_agent=to_agent, kind=kind,
                         subject=subject[:255], body=body, task_id=task_id)
        async with self.db.session() as s:
            s.add(m)
        await self.events.emit(f"agent.{kind}", subject or body[:160], namespace_id=namespace_id,
                               actor=f"agent:{from_agent}" if from_agent else "system", subject=to_agent or "humans",
                               level="warn" if kind == "escalation" else "info",
                               data={"message_id": m.id, "from": from_agent, "to": to_agent, "task_id": task_id, "body": body[:1500]})
        return m

    async def mark_read(self, message_ids: list[str]) -> None:
        async with self.db.session() as s:
            for m in (await s.execute(select(AgentMessage).where(AgentMessage.id.in_(message_ids)))).scalars():
                m.read_at = m.read_at or utcnow()

    async def take_unread(self, agent_id: str, limit: int = 10) -> list[AgentMessage]:
        async with self.db.session() as s:
            rows = list((await s.execute(select(AgentMessage).where(AgentMessage.to_agent == agent_id, AgentMessage.read_at.is_(None))
                                         .order_by(AgentMessage.created_at).limit(limit))).scalars())
            for m in rows:
                m.read_at = utcnow()
        return rows

    async def list(self, *, namespace_ids: list[str] | None = None, agent_id: str | None = None, limit: int = 200) -> list[AgentMessage]:
        q = select(AgentMessage).order_by(AgentMessage.created_at.desc()).limit(limit)
        if namespace_ids is not None:
            q = q.where(AgentMessage.namespace_id.in_(namespace_ids))
        if agent_id:
            q = q.where((AgentMessage.to_agent == agent_id) | (AgentMessage.from_agent == agent_id))
        async with self.db.session() as s:
            return list((await s.execute(q)).scalars())
