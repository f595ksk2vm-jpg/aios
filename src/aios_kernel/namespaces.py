"""Namespaces: the kernel's isolation boundary.

A namespace sees its own resources, those of its ancestors (shared downward),
those of its descendants (it owns them) and whatever other namespaces have
explicitly granted to it. Siblings are invisible to each other by default.
"""
from __future__ import annotations

from sqlalchemy import select

from .db import Database, new_id, utcnow
from .errors import Conflict, NotFound
from .models import ROOT_NS, Grant, Namespace

# Resource kinds whose cross-namespace use is enforced today.
GRANT_KINDS = {"knowledge", "agent", "workflow"}
PLACEMENTS = {"any", "local", "cloud"}


class NamespaceService:
    def __init__(self, db: Database):
        self.db = db

    async def ensure_root(self) -> None:
        async with self.db.session() as s:
            if not await s.get(Namespace, ROOT_NS):
                s.add(Namespace(id=ROOT_NS, kind="system", name="AI-OS"))

    async def create(self, kind: str, name: str, *, parent_id: str = ROOT_NS, compute: str = "any",
                     models: str = "any", meta: dict | None = None) -> Namespace:
        if compute not in PLACEMENTS or models not in PLACEMENTS:
            raise Conflict("placement must be any|local|cloud")
        async with self.db.session() as s:
            parent = await s.get(Namespace, parent_id)
            if not parent:
                raise NotFound("parent namespace not found")
            # A child may never be less restricted than its parent.
            compute = parent.compute if parent.compute != "any" else compute
            models = parent.models if parent.models != "any" else models
            ns = Namespace(id=new_id("ns"), kind=kind, name=name, parent_id=parent_id, compute=compute, models=models, meta=meta or {})
            s.add(ns)
        return ns

    async def get(self, ns_id: str) -> Namespace:
        async with self.db.session() as s:
            ns = await s.get(Namespace, ns_id)
        if not ns:
            raise NotFound(f"namespace {ns_id} not found")
        return ns

    async def update(self, ns_id: str, **fields) -> Namespace:
        async with self.db.session() as s:
            ns = await s.get(Namespace, ns_id)
            if not ns:
                raise NotFound("namespace not found")
            for k, v in fields.items():
                if v is None:
                    continue
                if k in {"compute", "models"} and v not in PLACEMENTS:
                    raise Conflict("placement must be any|local|cloud")
                if k == "meta":
                    v = {**(ns.meta or {}), **v}
                setattr(ns, k, v)
        return ns

    async def all(self) -> list[Namespace]:
        async with self.db.session() as s:
            return list((await s.execute(select(Namespace).order_by(Namespace.created_at))).scalars())

    async def chain(self, ns_id: str) -> list[Namespace]:
        """The namespace followed by its ancestors up to the root."""
        by_id = {n.id: n for n in await self.all()}
        out, cur = [], by_id.get(ns_id)
        while cur:
            out.append(cur)
            cur = by_id.get(cur.parent_id) if cur.parent_id else None
        if not out:
            raise NotFound(f"namespace {ns_id} not found")
        return out

    async def chain_ids(self, ns_id: str) -> list[str]:
        return [n.id for n in await self.chain(ns_id)]

    async def descendants(self, ns_id: str) -> list[str]:
        nodes = await self.all()
        children: dict[str, list[str]] = {}
        for n in nodes:
            children.setdefault(n.parent_id or "", []).append(n.id)
        out, stack = [], list(children.get(ns_id, []))
        while stack:
            cur = stack.pop()
            out.append(cur)
            stack.extend(children.get(cur, []))
        return out

    async def is_paused(self, ns_id: str) -> bool:
        return any(n.status != "active" for n in await self.chain(ns_id))

    async def placement(self, ns_id: str) -> tuple[str, str]:
        """Effective (compute, models) placement: the most specific restriction on the chain wins."""
        compute = models = "any"
        for n in reversed(await self.chain(ns_id)):
            compute = n.compute if n.compute != "any" else compute
            models = n.models if n.models != "any" else models
        return compute, models

    # --- sharing -----------------------------------------------------------------

    async def grant(self, from_ns: str, to_ns: str, resource_kind: str, *, resource_id: str | None = None,
                    created_by: str = "") -> Grant:
        if resource_kind not in GRANT_KINDS:
            raise Conflict(f"resource_kind must be one of {sorted(GRANT_KINDS)}")
        if from_ns == to_ns:
            raise Conflict("cannot grant a namespace access to itself")
        await self.get(from_ns)
        await self.get(to_ns)
        g = Grant(id=new_id("grt"), from_ns=from_ns, to_ns=to_ns, resource_kind=resource_kind, resource_id=resource_id, created_by=created_by)
        async with self.db.session() as s:
            s.add(g)
        return g

    async def revoke(self, grant_id: str) -> None:
        async with self.db.session() as s:
            g = await s.get(Grant, grant_id)
            if not g:
                raise NotFound("grant not found")
            g.revoked_at = utcnow()

    async def grants(self, active_only: bool = True) -> list[Grant]:
        q = select(Grant).order_by(Grant.created_at.desc())
        if active_only:
            q = q.where(Grant.revoked_at.is_(None))
        async with self.db.session() as s:
            return list((await s.execute(q)).scalars())

    async def visible(self, ns_id: str, resource_kind: str) -> tuple[set[str], set[tuple[str, str]]]:
        """Namespaces whose ``resource_kind`` resources ``ns_id`` may use.

        Returns ``(whole_namespaces, {(namespace, resource_id)})`` — the second
        set holds grants limited to a single resource.
        """
        chain = await self.chain_ids(ns_id)
        whole = set(chain) | set(await self.descendants(ns_id))
        single: set[tuple[str, str]] = set()
        async with self.db.session() as s:
            rows = (await s.execute(select(Grant).where(Grant.to_ns.in_(chain), Grant.resource_kind == resource_kind,
                                                        Grant.revoked_at.is_(None)))).scalars()
            for g in rows:
                if g.resource_id:
                    single.add((g.from_ns, g.resource_id))
                else:
                    whole.add(g.from_ns)
        return whole, single
