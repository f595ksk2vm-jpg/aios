"""Shared fixtures. Model replies come from the scripted provider; everything else is real."""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aios_kernel.config import Settings  # noqa: E402
from aios_kernel.kernel import Kernel  # noqa: E402
from aios_kernel.llm.scripted import ScriptedProvider  # noqa: E402
from aios_kernel.llm.types import ChatResult, ToolCall  # noqa: E402
from aios_kernel.tasks.queue import Worker  # noqa: E402


def last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m["role"] == "user":
            return m["content"]
    return ""


def tool_names(tools) -> set[str]:
    return {t.name for t in tools}


def called(messages: list[dict], name: str) -> bool:
    return any(c["name"] == name for m in messages if m["role"] == "assistant" for c in m.get("tool_calls", []))


def call(tool: str, **arguments) -> ChatResult:
    return ChatResult(text="", tool_calls=[ToolCall(id=f"call_{tool}_{id(arguments)}", name=tool, arguments=arguments)], stop_reason="tool_use")


def say(text: str) -> ChatResult:
    return ChatResult(text=text)


async def wait_for(predicate, timeout: float = 20.0, interval: float = 0.05):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = await predicate()
        if value:
            return value
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(interval)


async def fresh_database_url() -> str:
    """'' for the default (a SQLite file per test), or a brand-new PostgreSQL database when
    AIOS_TEST_DATABASE_URL points at a server, e.g. postgresql+asyncpg://aios@127.0.0.1:55432/postgres."""
    admin = os.environ.get("AIOS_TEST_DATABASE_URL")
    if not admin:
        return ""
    import asyncpg
    name = f"aios_test_{uuid.uuid4().hex[:12]}"
    conn = await asyncpg.connect(admin.replace("+asyncpg", ""))
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()
    return admin.rsplit("/", 1)[0] + "/" + name


async def make_kernel(tmp_path, layers: bool = False, **overrides) -> Kernel:
    settings = Settings(data_dir=tmp_path / "data", embedded_worker=False, enable_scripted_provider=True,
                        database_url=await fresh_database_url(), **overrides)
    k = Kernel(settings)
    if layers:
        import aios_ecosystem
        import aios_workforce
        aios_workforce.install(k)
        aios_ecosystem.install(k)
    await k.start()
    # Route everything to the scripted provider: no network in tests.
    for p in await k.models.providers():
        if p.kind != "scripted":
            await k.models.upsert_provider(name=p.name, kind=p.kind, location=p.location, base_url=p.base_url,
                                           secret_name=p.secret_name, enabled=False)
    k.orchestrator.retry_delay_s = 0.05
    return k


async def _run(k: Kernel):
    k.worker = Worker(k, location="local", concurrency=4, poll_s=0.05)
    k.worker.start()
    yield k
    ScriptedProvider.responder = staticmethod(lambda *a: ChatResult(text="(no responder)"))
    await k.stop()


@pytest_asyncio.fixture
async def kernel(tmp_path):
    """Layer 1 only."""
    async for k in _run(await make_kernel(tmp_path)):
        yield k


@pytest_asyncio.fixture
async def system(tmp_path):
    """All three layers."""
    async for k in _run(await make_kernel(tmp_path, layers=True)):
        yield k


def task_title(messages: list[dict]) -> str:
    return messages[0]["content"].splitlines()[0].removeprefix("# Task: ")


async def objective_done(k: Kernel, objective_id: str, timeout: float = 30.0):
    async def check():
        o = await k.orchestrator.get_objective(objective_id)
        return o if o.status in {"completed", "needs_attention", "failed", "cancelled"} else None
    o = await wait_for(check, timeout)
    tasks = await k.orchestrator.list_tasks(objective_id=objective_id)
    assert o.status == "completed", [(t.title, t.kind, t.status, t.error) for t in tasks]
    return o, tasks
