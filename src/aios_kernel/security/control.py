"""Approvals and the emergency stop."""
from __future__ import annotations

import time

from sqlalchemy import select

from ..db import Database, new_id, utcnow
from ..errors import Conflict, NotFound
from ..models import Approval, SystemState

STOP_KEY = "emergency_stop"


class KillSwitch:
    """Global halt. Checked by workers before claiming jobs and by runs before every step and tool call."""

    def __init__(self, db: Database):
        self.db = db
        self._cache: tuple[float, dict] = (0.0, {})

    async def state(self, fresh: bool = False) -> dict:
        now = time.monotonic()
        if not fresh and now - self._cache[0] < 1.0:
            return self._cache[1]
        async with self.db.session() as s:
            row = await s.get(SystemState, STOP_KEY)
        value = dict(row.value) if row else {"active": False}
        self._cache = (now, value)
        return value

    async def active(self) -> bool:
        return bool((await self.state()).get("active"))

    async def set(self, active: bool, *, by: str, reason: str = "") -> dict:
        value = {"active": active, "by": by, "reason": reason, "at": utcnow().isoformat() + "Z"}
        async with self.db.session() as s:
            row = await s.get(SystemState, STOP_KEY)
            if row:
                row.value = value
            else:
                s.add(SystemState(key=STOP_KEY, value=value))
        self._cache = (time.monotonic(), value)
        return value


class ApprovalService:
    def __init__(self, db: Database):
        self.db = db

    async def request(self, *, namespace_id: str, action: str, category: str, summary: str, payload: dict,
                      agent_id: str | None = None, run_id: str | None = None, task_id: str | None = None) -> Approval:
        a = Approval(id=new_id("apr"), namespace_id=namespace_id, agent_id=agent_id, run_id=run_id, task_id=task_id,
                     action=action, category=category, summary=summary[:4000], payload=payload)
        async with self.db.session() as s:
            s.add(a)
        return a

    async def get(self, approval_id: str) -> Approval:
        async with self.db.session() as s:
            a = await s.get(Approval, approval_id)
        if not a:
            raise NotFound("approval not found")
        return a

    async def list(self, status: str | None = None, limit: int = 200) -> list[Approval]:
        q = select(Approval).order_by(Approval.created_at.desc()).limit(limit)
        if status:
            q = q.where(Approval.status == status)
        async with self.db.session() as s:
            return list((await s.execute(q)).scalars())

    async def decide(self, approval_id: str, *, approve: bool, decided_by: str, reason: str = "") -> Approval:
        async with self.db.session() as s:
            a = await s.get(Approval, approval_id)
            if not a:
                raise NotFound("approval not found")
            if a.status != "pending":
                raise Conflict(f"approval already {a.status}")
            a.status = "approved" if approve else "rejected"
            a.decided_by, a.decided_at, a.reason = decided_by, utcnow(), reason
        return a
