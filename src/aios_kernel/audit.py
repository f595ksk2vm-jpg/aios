"""Tamper-evident audit trail. Each entry commits to the previous one by hash."""
from __future__ import annotations

import asyncio
import hashlib
import json

from sqlalchemy import select

from .db import Database, utcnow
from .models import ROOT_NS, AuditEntry


def _digest(prev: str, e: AuditEntry) -> str:
    body = json.dumps([prev, e.ts.isoformat(), e.actor, e.action, e.resource, e.namespace_id, e.outcome, e.detail],
                      sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()


class AuditLog:
    def __init__(self, db: Database):
        self.db = db
        self._lock = asyncio.Lock()

    async def record(self, actor: str, action: str, *, resource: str = "", namespace_id: str = ROOT_NS,
                     outcome: str = "ok", detail: dict | None = None) -> None:
        async with self._lock, self.db.session() as s:
            # Reading the tail and appending happen in one write transaction,
            # so concurrent writers (even other processes) cannot fork the chain.
            last = (await s.execute(select(AuditEntry).order_by(AuditEntry.id.desc()).limit(1))).scalar_one_or_none()
            e = AuditEntry(ts=utcnow(), actor=actor, action=action, resource=resource, namespace_id=namespace_id or ROOT_NS,
                           outcome=outcome, detail=detail or {}, prev_hash=last.hash if last else "")
            e.hash = _digest(e.prev_hash, e)
            s.add(e)

    async def list(self, limit: int = 200, action_prefix: str | None = None) -> list[AuditEntry]:
        q = select(AuditEntry).order_by(AuditEntry.id.desc()).limit(limit)
        if action_prefix:
            q = q.where(AuditEntry.action.startswith(action_prefix))
        async with self.db.session() as s:
            return list((await s.execute(q)).scalars())

    async def verify(self) -> dict:
        """Recompute the chain; returns the first broken entry id, if any."""
        prev, count = "", 0
        async with self.db.session() as s:
            rows = (await s.execute(select(AuditEntry).order_by(AuditEntry.id.asc()))).scalars()
            for e in rows:
                count += 1
                if e.prev_hash != prev or e.hash != _digest(prev, e):
                    return {"ok": False, "entries": count, "broken_at": e.id}
                prev = e.hash
        return {"ok": True, "entries": count, "broken_at": None}
