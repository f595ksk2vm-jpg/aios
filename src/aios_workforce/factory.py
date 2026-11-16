"""Agent Factory: builds agents, departments and whole organizations from templates, on demand."""
from __future__ import annotations

from sqlalchemy import select

from aios_kernel.agents.registry import norm_role
from aios_kernel.db import new_id
from aios_kernel.errors import AiosError, Conflict, NotFound
from aios_kernel.models import ROOT_NS, Agent
from aios_kernel.security import policy

from .models import CustomTemplate, Department
from .templates import ALIASES, DEPARTMENTS, KITS, NAMES, ORG_PRESETS, TEMPLATES, kit

DEFAULT_MAX_AGENTS = 60


class AgentFactory:
    def __init__(self, kernel):
        self.k = kernel

    # --- templates -----------------------------------------------------------------

    async def templates(self) -> dict[str, dict]:
        out = {key: {**t, "builtin": True} for key, t in TEMPLATES.items()}
        async with self.k.db.session() as s:
            for row in (await s.execute(select(CustomTemplate))).scalars():
                out[row.key] = {**row.spec, "key": row.key, "builtin": False}
        return out

    async def save_template(self, spec: dict, created_by: str) -> dict:
        key = norm_role(spec.get("key") or spec.get("title", ""))
        if not key:
            raise Conflict("a template needs a key or title")
        if key in TEMPLATES:
            raise Conflict("built-in templates cannot be overwritten; choose another key")
        unknown = [n for n in spec.get("kits", []) if n not in KITS]
        if unknown:
            raise Conflict(f"unknown capability kits: {unknown}")
        clean = {"key": key, "title": spec.get("title") or key.replace("_", " ").title(), "department": spec.get("department", ""),
                 "reports_to": spec.get("reports_to"), "level": spec.get("level", "ic"), "kits": spec.get("kits") or ["base", "files"],
                 "tier": spec.get("tier", "balanced"), "autonomy": spec.get("autonomy", "supervised"), "prompt": spec.get("prompt", "")}
        if clean["autonomy"] not in policy.AUTONOMY_LEVELS:
            raise Conflict("invalid autonomy level")
        async with self.k.db.session() as s:
            row = await s.get(CustomTemplate, key)
            if row:
                row.spec = clean
            else:
                s.add(CustomTemplate(key=key, spec=clean, created_by=created_by))
        return clean

    async def delete_template(self, key: str) -> None:
        async with self.k.db.session() as s:
            row = await s.get(CustomTemplate, key)
            if not row:
                raise NotFound("custom template not found")
            await s.delete(row)

    async def match_template(self, role: str) -> dict | None:
        """Resolve a free-text role to a template: exact key, alias, then best word overlap."""
        templates = await self.templates()
        want = norm_role(role)
        if want in templates:
            return templates[want]
        if want in ALIASES:
            return templates[ALIASES[want]]
        words = set(want.split("_"))
        best, best_score = None, 0.0
        for t in templates.values():
            tw = set(t["key"].split("_")) | set(norm_role(t["title"]).split("_"))
            score = len(words & tw) / max(len(words | tw), 1)
            if score > best_score:
                best, best_score = t, score
        return best if best_score >= 0.5 else None

    # --- creation ------------------------------------------------------------------

    async def _pick_name(self, namespace_id: str, title: str) -> str:
        existing = {a.name for a in await self.k.agents.list(namespace_ids=[namespace_id])}
        start = sum(map(ord, title + namespace_id)) % len(NAMES)
        for i in range(len(NAMES)):
            name = NAMES[(start + i) % len(NAMES)]
            if name not in existing:
                return name
        return f"{title} {len(existing) + 1}"

    async def _find_manager(self, namespace_id: str, template: dict, templates: dict) -> Agent | None:
        """Nearest existing agent up the template's reporting line (CTO if no architect yet, CEO if no CTO, ...)."""
        role, hops = template.get("reports_to"), 0
        while role and hops < 6:
            found = await self.k.agents.find_for_role(namespace_id, role)
            if found and found.role == role:
                return found
            role, hops = (templates.get(role) or {}).get("reports_to"), hops + 1
        return None

    async def create_from_template(self, namespace_id: str, template_key: str, *, name: str | None = None,
                                   manager_id: str | None = None, autonomy: str | None = None, model: dict | None = None,
                                   created_by: str = "system", creator: Agent | None = None, extra_prompt: str = "") -> Agent:
        k = self.k
        templates = await self.templates()
        tpl = templates.get(norm_role(template_key))
        if not tpl:
            raise NotFound(f"no template {template_key!r}")
        tools, perms = kit(*tpl["kits"])
        tools = [t for t in tools if k.tools.get(t)]
        if not manager_id:
            mgr = await self._find_manager(namespace_id, tpl, templates)
            manager_id = mgr.id if mgr else None
        spec = dict(
            namespace_id=namespace_id, name=name or await self._pick_name(namespace_id, tpl["title"]), role=tpl["key"],
            title=tpl["title"], department=tpl.get("department", ""), manager_id=manager_id,
            system_prompt=(tpl.get("prompt", "") + ("\n\n" + extra_prompt if extra_prompt else "")).strip(),
            model=model or {"mode": "auto", "tier": tpl.get("tier", "balanced"), "prefer": "balanced"},
            tools=tools, permissions=perms, autonomy=autonomy or tpl.get("autonomy", "supervised"),
            meta={"template": tpl["key"], "level": tpl.get("level", "ic")})
        if creator:
            # Attenuation: an agent-created agent gets the template's kit only as far as the creator itself holds it.
            spec["permissions"] = [p for p in perms if policy.covered_by(p, creator.permissions)]
            if policy.autonomy_rank(spec["autonomy"]) > policy.autonomy_rank(creator.autonomy):
                spec["autonomy"] = creator.autonomy
            spec["manager_id"] = manager_id or creator.id
            agent = await k.agents.create_by_agent(creator, **spec)
        else:
            agent = await k.agents.create(created_by=created_by, **spec)
        if agent.department:
            await self.ensure_department(namespace_id, agent.department, head=agent if tpl.get("level") != "ic" else None)
        await k.audit.record(f"agent:{creator.id}" if creator else created_by, "agent.create", resource=agent.id,
                             namespace_id=namespace_id, detail={"template": tpl["key"], "permissions": agent.permissions,
                                                                "autonomy": agent.autonomy})
        await k.events.emit("agent.created", f"{agent.name} joined as {agent.title}", namespace_id=namespace_id,
                            actor=f"agent:{creator.id}" if creator else created_by, subject=agent.id,
                            data={"agent_id": agent.id, "role": agent.role, "department": agent.department})
        return agent

    async def ensure_department(self, namespace_id: str, name: str, head: Agent | None = None) -> Department:
        async with self.k.db.session() as s:
            d = (await s.execute(select(Department).where(Department.namespace_id == namespace_id, Department.name == name))).scalar_one_or_none()
            if not d:
                d = Department(id=new_id("dep"), namespace_id=namespace_id, name=name)
                s.add(d)
            if head and not d.head_agent_id:
                d.head_agent_id = head.id
        return d

    async def departments(self, namespace_ids: list[str] | None = None) -> list[Department]:
        q = select(Department).order_by(Department.name)
        if namespace_ids is not None:
            q = q.where(Department.namespace_id.in_(namespace_ids))
        async with self.k.db.session() as s:
            return list((await s.execute(q)).scalars())

    async def create_department(self, namespace_id: str, department: str, *, roles: list[str] | None = None,
                                autonomy: str | None = None, created_by: str = "system", creator: Agent | None = None) -> list[Agent]:
        """Staff a whole department: managers first so reporting lines resolve, then individual contributors."""
        templates = await self.templates()
        keys = [norm_role(r) for r in roles] if roles else [key for key, t in templates.items() if t.get("department") == department]
        missing = [key for key in keys if key not in templates]
        if missing or not keys:
            raise Conflict(f"unknown department or roles: {department!r} {missing}")
        order = {"executive": 0, "manager": 1, "ic": 2}
        created = []
        for key in sorted(keys, key=lambda x: order.get(templates[x].get("level", "ic"), 2)):
            if await self._exists(namespace_id, key):
                continue
            created.append(await self.create_from_template(namespace_id, key, autonomy=autonomy, created_by=created_by, creator=creator))
        await self.ensure_department(namespace_id, department)
        return created

    async def create_org(self, namespace_id: str, preset: str = "lean", *, autonomy: str | None = None, created_by: str = "system") -> list[Agent]:
        keys = ORG_PRESETS.get(preset)
        if not keys:
            raise Conflict(f"preset must be one of {sorted(ORG_PRESETS)}")
        templates = await self.templates()
        order = {"executive": 0, "manager": 1, "ic": 2}
        ordered = sorted(keys, key=lambda x: (order.get(templates[x].get("level", "ic"), 2), 0 if x == "ceo" else 1))
        created = []
        for key in ordered:
            if not await self._exists(namespace_id, key):
                created.append(await self.create_from_template(namespace_id, key, autonomy=autonomy, created_by=created_by))
        await self.relink(namespace_id)
        return created

    async def _exists(self, namespace_id: str, role: str) -> bool:
        return any(a.role == role and a.status != "deactivated" for a in await self.k.agents.list(namespace_ids=[namespace_id]))

    async def relink(self, namespace_id: str) -> None:
        """Re-resolve reporting lines after bulk creation (a manager may have been created after its report)."""
        templates = await self.templates()
        agents = await self.k.agents.list(namespace_ids=[namespace_id], status="active")
        by_role = {a.role: a for a in agents}
        for a in agents:
            tpl = templates.get(a.meta.get("template", ""))
            if not tpl or not a.created_by.startswith(("system", "user:")):
                continue
            role = tpl.get("reports_to")
            while role and role not in by_role:
                role = (templates.get(role) or {}).get("reports_to")
            target = by_role.get(role) if role else None
            if target and target.id != a.id and a.manager_id != target.id:
                await self.k.agents.update(a.id, manager_id=target.id)

    # --- on-demand provisioning (installed as the orchestrator's provisioner) -------------

    async def org_namespace(self, namespace_id: str) -> str:
        """The organization a namespace belongs to: its highest ancestor below the system root."""
        chain = await self.k.namespaces.chain_ids(namespace_id)
        return chain[-2] if len(chain) >= 2 else ROOT_NS

    async def provision(self, namespace_id: str, role: str, requested_by: Agent) -> Agent | None:
        """Create the agent a plan needs if the organization allows it. Returns None when it must not or cannot."""
        k = self.k
        chain = await k.namespaces.chain(namespace_id)
        if not any(ns.meta.get("auto_provision") for ns in chain):
            return None
        org = await self.org_namespace(namespace_id)
        org_ns = await k.namespaces.get(org)
        if len(await k.agents.list(namespace_ids=[org], status="active")) >= int(org_ns.meta.get("max_agents", DEFAULT_MAX_AGENTS)):
            await k.events.emit("agent.provision_refused", f"Agent limit reached; cannot add a {role}", namespace_id=org, level="warn")
            return None
        autonomy = org_ns.meta.get("default_autonomy")
        tpl = await self.match_template(role)
        try:
            if tpl:
                return await self.create_from_template(org, tpl["key"], autonomy=autonomy, created_by="system:provisioner")
            # No template fits: a generic specialist with the baseline kit and a role-specific brief.
            tools, perms = kit("base", "files", "research")
            title = role.replace("_", " ").strip().title()[:80]
            lead = await k.agents.lead(org)
            agent = await k.agents.create(
                namespace_id=org, name=await self._pick_name(org, title), role=role, title=title, department="Specialists",
                manager_id=lead.id if lead else None, tools=[t for t in tools if k.tools.get(t)], permissions=perms,
                autonomy=autonomy or "supervised", created_by="system:provisioner", meta={"template": None, "level": "ic"},
                system_prompt=f"You are a specialist {title}. Apply the standards of that profession, produce concrete "
                              "deliverables in the workspace, and be explicit about assumptions and limits.")
            await k.events.emit("agent.created", f"{agent.name} joined as {agent.title} (provisioned on demand)", namespace_id=org,
                                actor="system:provisioner", subject=agent.id, data={"agent_id": agent.id, "role": agent.role})
            return agent
        except AiosError as e:
            await k.events.emit("agent.provision_failed", f"Could not provision {role}: {e}", namespace_id=org, level="warn")
            return None

    async def org_chart(self, namespace_id: str) -> dict:
        """Hierarchy of a namespace's agents as a tree, plus department groupings."""
        agents = [a for a in await self.k.agents.list(namespace_ids=[namespace_id]) if a.status != "deactivated"]
        ids = {a.id for a in agents}
        children: dict[str | None, list[Agent]] = {}
        for a in agents:
            children.setdefault(a.manager_id if a.manager_id in ids else None, []).append(a)

        def node(a: Agent) -> dict:
            return {"id": a.id, "name": a.name, "title": a.title, "role": a.role, "department": a.department, "status": a.status,
                    "autonomy": a.autonomy, "reports": [node(c) for c in sorted(children.get(a.id, []), key=lambda x: x.title)]}

        depts = {d: [a.id for a in agents if a.department == d] for d in DEPARTMENTS + sorted({a.department for a in agents} - set(DEPARTMENTS))}
        return {"namespace_id": namespace_id, "roots": [node(a) for a in sorted(children.get(None, []), key=lambda x: x.created_at)],
                "departments": {d: ids_ for d, ids_ in depts.items() if ids_}, "headcount": len(agents)}
