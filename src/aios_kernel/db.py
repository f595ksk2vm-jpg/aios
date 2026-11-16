"""Database access. SQLite (local) and PostgreSQL (cloud) through one code path."""
from __future__ import annotations

import contextvars
import datetime as dt
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Single declarative base shared by all three layers' tables."""


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def utcnow() -> dt.datetime:
    # Naive UTC everywhere: SQLite has no timezone support and mixing aware
    # and naive datetimes across backends is a classic source of bugs.
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


_in_session: contextvars.ContextVar[bool] = contextvars.ContextVar("aios_in_session", default=False)


class Database:
    def __init__(self, url: str):
        self.url = url
        self.is_sqlite = url.startswith("sqlite")
        kwargs: dict = {}
        if self.is_sqlite:
            kwargs["connect_args"] = {"timeout": 30}
        else:
            kwargs["pool_size"] = 10
            kwargs["max_overflow"] = 20
        self.engine = create_async_engine(url, **kwargs)
        if self.is_sqlite:
            self._configure_sqlite()
        self._maker = async_sessionmaker(self.engine, expire_on_commit=False)

    def _configure_sqlite(self) -> None:
        # WAL + BEGIN IMMEDIATE: concurrent asyncio tasks (and separate worker
        # processes) queue for the write lock instead of failing with
        # "database is locked" when a read transaction tries to upgrade.
        @event.listens_for(self.engine.sync_engine, "connect")
        def _on_connect(dbapi_conn, _record):
            dbapi_conn.isolation_level = None
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        @event.listens_for(self.engine.sync_engine, "begin")
        def _on_begin(conn):
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Short unit of work. Never hold one open across a model or tool call.

        Sessions do not nest: opening one inside another would wait on the
        first one's write lock (SQLite) or pin two pooled connections
        (PostgreSQL). That is a programming error, so it fails loudly.
        """
        if _in_session.get():
            raise RuntimeError("nested database session: finish the current unit of work before starting another")
        token = _in_session.set(True)
        try:
            async with self._maker() as s:
                try:
                    yield s
                    await s.commit()
                except BaseException:
                    await s.rollback()
                    raise
        finally:
            _in_session.reset(token)

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()
