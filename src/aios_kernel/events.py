"""Activity stream: every notable thing the system does becomes a persisted event.

Events are written to the database (so any process in a distributed deployment
can tail them) and fanned out to in-process subscribers (hooks used by upper
layers, and wake-ups for live streams).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from sqlalchemy import select

from .db import Database
from .models import ROOT_NS, Event

log = logging.getLogger("aios.events")
Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self, db: Database):
        self.db = db
        self._handlers: list[tuple[str, Handler]] = []
        self._wake = asyncio.Event()

    def subscribe(self, prefix: str, handler: Handler) -> None:
        """Run ``handler`` for every event whose kind starts with ``prefix`` (this process only)."""
        self._handlers.append((prefix, handler))

    async def emit(self, kind: str, message: str = "", *, namespace_id: str = ROOT_NS, actor: str = "system",
                   subject: str = "", level: str = "info", data: dict | None = None) -> Event:
        ev = Event(kind=kind, message=message[:4000], namespace_id=namespace_id or ROOT_NS, actor=actor,
                   subject=subject, level=level, data=data or {})
        async with self.db.session() as s:
            s.add(ev)
        self._wake.set()
        for prefix, handler in self._handlers:
            if kind.startswith(prefix):
                try:
                    await handler(ev)
                except Exception:  # a broken hook must never break the emitter
                    log.exception("event handler failed for %s", kind)
        return ev

    async def wait(self, timeout: float) -> None:
        """Block until a local event arrives or ``timeout`` elapses (remote events are found by polling)."""
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    async def list(self, *, after_id: int = 0, limit: int = 200, namespace_ids: list[str] | None = None,
                   kind_prefix: str | None = None, newest_first: bool = False) -> list[Event]:
        q = select(Event)
        if after_id:
            q = q.where(Event.id > after_id)
        if namespace_ids is not None:
            q = q.where(Event.namespace_id.in_(namespace_ids))
        if kind_prefix:
            q = q.where(Event.kind.startswith(kind_prefix))
        q = q.order_by(Event.id.desc() if newest_first else Event.id.asc()).limit(limit)
        async with self.db.session() as s:
            return list((await s.execute(q)).scalars())
