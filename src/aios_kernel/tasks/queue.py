"""Durable job queue and workers.

The queue is a database table, claimed with an optimistic compare-and-set, so
the same code runs on SQLite (one machine) and PostgreSQL (many workers, many
machines) with no broker to operate. Each job carries a placement *affinity*;
a worker only claims jobs for its own location, which is how hybrid
deployments keep local workloads on local hardware.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
from typing import Awaitable, Callable

from sqlalchemy import func, select, update

from ..db import Database, new_id, utcnow
from ..models import Job, WorkerNode

log = logging.getLogger("aios.queue")
STALE_AFTER_S = 120


class JobQueue:
    def __init__(self, db: Database):
        self.db = db
        self.wake = asyncio.Event()

    async def enqueue(self, kind: str, payload: dict, *, priority: int = 5, affinity: str = "any", delay_s: float = 0,
                      max_attempts: int = 3) -> Job:
        job = Job(id=new_id("job"), kind=kind, payload=payload, priority=priority, affinity=affinity,
                  run_after=utcnow() + dt.timedelta(seconds=delay_s), max_attempts=max_attempts)
        async with self.db.session() as s:
            s.add(job)
        self.wake.set()
        return job

    async def claim(self, worker_id: str, location: str, limit: int) -> list[Job]:
        if limit <= 0:
            return []
        now = utcnow()
        claimed: list[Job] = []
        async with self.db.session() as s:
            rows = list((await s.execute(
                select(Job).where(Job.status == "queued", Job.run_after <= now, Job.affinity.in_(["any", location]))
                .order_by(Job.priority, Job.created_at).limit(limit * 2))).scalars())
            for job in rows:
                if len(claimed) >= limit:
                    break
                # Compare-and-set: only one worker's UPDATE matches status='queued'.
                res = await s.execute(update(Job).where(Job.id == job.id, Job.status == "queued")
                                      .values(status="running", locked_by=worker_id, heartbeat_at=now, attempts=Job.attempts + 1))
                if res.rowcount == 1:
                    claimed.append(job)
        return claimed

    async def finish(self, job_id: str, *, error: str | None = None) -> None:
        async with self.db.session() as s:
            job = await s.get(Job, job_id)
            if not job:
                return
            if error is None:
                job.status, job.finished_at = "done", utcnow()
            elif job.attempts < job.max_attempts:
                job.status, job.error, job.locked_by = "queued", error[:2000], None
                job.run_after = utcnow() + dt.timedelta(seconds=min(60, 2 ** job.attempts))
            else:
                job.status, job.error, job.finished_at = "dead", error[:2000], utcnow()

    async def heartbeat(self, worker_id: str) -> None:
        async with self.db.session() as s:
            await s.execute(update(Job).where(Job.status == "running", Job.locked_by == worker_id).values(heartbeat_at=utcnow()))

    async def requeue_stale(self) -> int:
        """Return jobs whose worker stopped heart-beating to the queue (crash recovery)."""
        cutoff = utcnow() - dt.timedelta(seconds=STALE_AFTER_S)
        async with self.db.session() as s:
            res = await s.execute(update(Job).where(Job.status == "running", Job.heartbeat_at < cutoff)
                                  .values(status="queued", locked_by=None))
        return res.rowcount or 0

    async def stats(self) -> dict:
        async with self.db.session() as s:
            rows = (await s.execute(select(Job.status, func.count()).group_by(Job.status))).all()
        return {status: n for status, n in rows}


class Worker:
    def __init__(self, kernel, *, location: str, concurrency: int, name: str | None = None, poll_s: float = 1.0):
        self.k = kernel
        self.location = location
        self.concurrency = concurrency
        self.id = name or f"{kernel.settings.node_name}:{os.getpid()}:{location}"
        self.poll_s = poll_s
        self._active: set[asyncio.Task] = set()
        self._stopping = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name=f"worker-{self.id}")

    async def stop(self) -> None:
        self._stopping = True
        self.k.queue.wake.set()
        if self._task:
            await asyncio.wait([self._task], timeout=10)
        for t in list(self._active):
            t.cancel()
        if self._active:
            await asyncio.wait(self._active, timeout=5)
        async with self.k.db.session() as s:
            row = await s.get(WorkerNode, self.id)
            if row:
                await s.delete(row)

    async def _register(self) -> None:
        async with self.k.db.session() as s:
            row = await s.get(WorkerNode, self.id)
            if row:
                row.heartbeat_at, row.active_jobs, row.concurrency = utcnow(), len(self._active), self.concurrency
            else:
                s.add(WorkerNode(id=self.id, node_name=self.k.settings.node_name, location=self.location,
                                 concurrency=self.concurrency, active_jobs=len(self._active),
                                 meta={"pid": os.getpid(), "sandbox": (await self.k.sandbox.info())}))

    async def run(self) -> None:
        k = self.k
        last_beat = 0.0
        loop = asyncio.get_running_loop()
        while not self._stopping:
            try:
                if loop.time() - last_beat > 10:
                    await self._register()
                    await k.queue.heartbeat(self.id)
                    await k.queue.requeue_stale()
                    last_beat = loop.time()
                if not await k.killswitch.active():
                    for job in await k.queue.claim(self.id, self.location, self.concurrency - len(self._active)):
                        t = asyncio.create_task(self._execute(job), name=f"job-{job.id}")
                        self._active.add(t)
                        t.add_done_callback(self._active.discard)
            except Exception:
                log.exception("worker loop error")
            k.queue.wake.clear()
            try:
                await asyncio.wait_for(k.queue.wake.wait(), self.poll_s)
            except asyncio.TimeoutError:
                pass

    async def _execute(self, job: Job) -> None:
        handler: Callable[[dict], Awaitable[None]] | None = self.k.job_handlers.get(job.kind)
        try:
            if not handler:
                raise RuntimeError(f"no handler for job kind {job.kind!r}")
            await handler({**job.payload, "_attempt": job.attempts + 1})
            await self.k.queue.finish(job.id)
        except asyncio.CancelledError:
            await asyncio.shield(self.k.queue.finish(job.id, error="worker shut down"))
            raise
        except Exception as e:
            log.exception("job %s (%s) failed", job.id, job.kind)
            await self.k.queue.finish(job.id, error=f"{type(e).__name__}: {e}")
        finally:
            self.k.queue.wake.set()
