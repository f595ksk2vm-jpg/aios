"""Usage metering and spending limits."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import String, func, select

from .db import Database, new_id, utcnow
from .errors import BudgetExceeded, Conflict, NotFound
from .models import Budget, UsageRecord

PERIODS = {"day", "month", "total"}


def period_start(period: str, now: dt.datetime | None = None) -> dt.datetime | None:
    now = now or utcnow()
    if period == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return None


class BudgetService:
    def __init__(self, db: Database, namespaces):
        self.db = db
        self.namespaces = namespaces

    async def record(self, **fields) -> None:
        async with self.db.session() as s:
            s.add(UsageRecord(**fields))

    async def set_budget(self, scope_kind: str, scope_id: str, period: str, limit_usd: float) -> Budget:
        if scope_kind not in {"global", "namespace", "agent"} or period not in PERIODS:
            raise Conflict("scope_kind must be global|namespace|agent and period day|month|total")
        if limit_usd < 0:
            raise Conflict("limit must be non-negative")
        scope_id = "" if scope_kind == "global" else scope_id
        async with self.db.session() as s:
            b = (await s.execute(select(Budget).where(Budget.scope_kind == scope_kind, Budget.scope_id == scope_id,
                                                      Budget.period == period))).scalar_one_or_none()
            if b:
                b.limit_usd = limit_usd
            else:
                b = Budget(id=new_id("bud"), scope_kind=scope_kind, scope_id=scope_id, period=period, limit_usd=limit_usd)
                s.add(b)
        return b

    async def delete_budget(self, budget_id: str) -> None:
        async with self.db.session() as s:
            b = await s.get(Budget, budget_id)
            if not b:
                raise NotFound("budget not found")
            await s.delete(b)

    async def _trees(self) -> dict[str, list[str]]:
        """namespace id -> itself plus all descendants (a namespace budget covers a business and all its projects)."""
        nodes = await self.namespaces.all()
        kids: dict[str, list[str]] = {}
        for n in nodes:
            kids.setdefault(n.parent_id or "", []).append(n.id)

        def tree(root: str) -> list[str]:
            out, stack = [root], list(kids.get(root, []))
            while stack:
                cur = stack.pop()
                out.append(cur)
                stack.extend(kids.get(cur, []))
            return out

        return {n.id: tree(n.id) for n in nodes}

    async def _spent(self, s, b: Budget, trees: dict[str, list[str]]) -> float:
        q = select(func.coalesce(func.sum(UsageRecord.cost_usd), 0.0))
        start = period_start(b.period)
        if start:
            q = q.where(UsageRecord.ts >= start)
        if b.scope_kind == "namespace":
            q = q.where(UsageRecord.namespace_id.in_(trees.get(b.scope_id, [b.scope_id])))
        elif b.scope_kind == "agent":
            q = q.where(UsageRecord.agent_id == b.scope_id)
        return float((await s.execute(q)).scalar_one())

    async def budgets(self) -> list[dict]:
        trees = await self._trees()
        async with self.db.session() as s:
            rows = list((await s.execute(select(Budget).order_by(Budget.scope_kind, Budget.scope_id))).scalars())
            return [{**b.to_dict(), "spent_usd": round(await self._spent(s, b, trees), 6)} for b in rows]

    async def check(self, namespace_id: str, agent_id: str | None) -> None:
        """Raise :class:`BudgetExceeded` if any limit covering this spend is exhausted."""
        chain = await self.namespaces.chain_ids(namespace_id)
        trees = await self._trees()
        async with self.db.session() as s:
            rows = list((await s.execute(select(Budget))).scalars())
            for b in rows:
                applies = (b.scope_kind == "global" or (b.scope_kind == "namespace" and b.scope_id in chain)
                           or (b.scope_kind == "agent" and b.scope_id == agent_id))
                if not applies:
                    continue
                spent = await self._spent(s, b, trees)
                if spent >= b.limit_usd:
                    scope = b.scope_kind if b.scope_kind == "global" else f"{b.scope_kind} {b.scope_id}"
                    raise BudgetExceeded(f"spending limit reached for {scope}: ${spent:.4f} of ${b.limit_usd:.2f} per {b.period}")

    async def summary(self, *, days: int = 30, namespace_ids: list[str] | None = None) -> dict:
        since = utcnow() - dt.timedelta(days=days)
        base = select(func.coalesce(func.sum(UsageRecord.cost_usd), 0.0), func.coalesce(func.sum(UsageRecord.input_tokens), 0),
                      func.coalesce(func.sum(UsageRecord.output_tokens), 0), func.count()).where(UsageRecord.ts >= since)
        if namespace_ids is not None:
            base = base.where(UsageRecord.namespace_id.in_(namespace_ids))
        async with self.db.session() as s:
            cost, tin, tout, calls = (await s.execute(base)).one()

            async def grouped(col):
                q = base.add_columns(col).group_by(col)
                return [{"key": r[4] or "", "cost_usd": round(float(r[0]), 6), "input_tokens": int(r[1]),
                         "output_tokens": int(r[2]), "calls": int(r[3])} for r in (await s.execute(q)).all()]

            by_model = await grouped(UsageRecord.model)
            by_ns = await grouped(UsageRecord.namespace_id)
            by_agent = await grouped(UsageRecord.agent_id)
            day = func.substr(func.cast(UsageRecord.ts, String), 1, 10)
            by_day = await grouped(day)
        return {"days": days, "cost_usd": round(float(cost), 6), "input_tokens": int(tin), "output_tokens": int(tout), "calls": int(calls),
                "by_model": sorted(by_model, key=lambda r: -r["cost_usd"]), "by_namespace": by_ns, "by_agent": by_agent,
                "by_day": sorted(by_day, key=lambda r: r["key"])}
