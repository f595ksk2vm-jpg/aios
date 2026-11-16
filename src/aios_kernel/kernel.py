"""The kernel: one object that wires Layer 1 together.

Upper layers receive a :class:`Kernel` and extend it through its public
registries — ``tools``, ``events.subscribe``, ``orchestrator.provisioner`` and
``job_handlers`` — and never the other way round.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Awaitable, Callable

from .agents.registry import AgentRegistry, Messaging
from .agents.runtime import AgentRuntime
from .audit import AuditLog
from .budget import BudgetService
from .config import Settings
from .db import Database
from .events import EventBus
from .llm.service import ModelService
from .memory.embeddings import HashingEmbedder, RemoteEmbedder
from .memory.store import MemoryService
from .models import ROOT_NS
from .namespaces import NamespaceService
from .sandbox import make_sandbox
from .security.auth import AuthService
from .security.control import ApprovalService, KillSwitch
from .security.secrets import SecretStore, load_master_key
from .tasks.orchestrator import Orchestrator
from .tasks.queue import JobQueue, Worker
from .tools import builtin
from .tools.base import ToolRegistry
from .tools.connectors import ConnectorService

log = logging.getLogger("aios.kernel")

SYSTEM_AGENT_PROMPT = (
    "You are the AI-OS system orchestrator. When no organization-specific leader exists you plan objectives, "
    "coordinate the available agents and report results. You are precise, economical with other agents' time, "
    "and honest about what was and was not achieved.")


class Kernel:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()
        self.settings.finalize()
        s = self.settings
        s.data_dir.mkdir(parents=True, exist_ok=True)
        s.workspaces_dir.mkdir(parents=True, exist_ok=True)

        self.db = Database(s.database_url)
        self.events = EventBus(self.db)
        self.audit = AuditLog(self.db)
        self.auth = AuthService(self.db, s.session_ttl_hours)
        self.secrets = SecretStore(self.db, load_master_key(s.secret_key, s.data_dir))
        self.killswitch = KillSwitch(self.db)
        self.approvals = ApprovalService(self.db)
        self.namespaces = NamespaceService(self.db)
        self.budget = BudgetService(self.db, self.namespaces)
        self.models = ModelService(self.db, self.secrets, self.events, enable_scripted=s.enable_scripted_provider)
        embedder = (RemoteEmbedder(s.embedder_base_url, s.embedder_model) if s.embedder == "openai_compat" and s.embedder_base_url
                    and s.embedder_model else HashingEmbedder())
        self.memory = MemoryService(self.db, self.namespaces, embedder)
        self.tools = ToolRegistry(self)
        self.connectors = ConnectorService(self)
        self.agents = AgentRegistry(self.db, self.namespaces, self.tools)
        self.messaging = Messaging(self.db, self.events)
        self.queue = JobQueue(self.db)
        self.runtime = AgentRuntime(self)
        self.orchestrator = Orchestrator(self)
        self.sandbox = None  # chosen in start(): needs to probe the host
        self.job_handlers: dict[str, Callable[[dict], Awaitable[None]]] = {
            "task.run": self.orchestrator.handle_task_run,
            "run.resume": self.orchestrator.handle_run_resume,
        }
        self.extensions: dict[str, object] = {}  # upper layers register their service objects here
        self.worker: Worker | None = None
        self._started = False

        builtin.register(self.tools)
        self.orchestrator.register_tools(self.tools)

    def workspace_for(self, namespace_id: str) -> Path:
        """Each namespace gets its own directory; file and execution tools are confined to it."""
        path = self.settings.workspaces_dir / namespace_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def start(self, *, run_worker: bool | None = None) -> None:
        """Create tables, seed defaults and (optionally) start an in-process worker. Idempotent."""
        if self._started:
            return
        s = self.settings
        await self.db.create_all()
        await self.namespaces.ensure_root()
        await self.models.seed()
        await self.connectors.load()
        self.sandbox = await make_sandbox(s.sandbox_driver, image=s.sandbox_docker_image, memory_mb=s.sandbox_memory_mb)
        if not await self.agents.list(namespace_ids=[ROOT_NS]):
            await self.agents.create(namespace_id=ROOT_NS, name="Atlas", role="orchestrator", title="System Orchestrator",
                                     system_prompt=SYSTEM_AGENT_PROMPT, model={"mode": "auto", "tier": "deep", "prefer": "quality"},
                                     tools=["memory_search", "memory_store"], permissions=["memory.*"], autonomy="supervised")
        if s.admin_email and s.admin_password and await self.auth.user_count() == 0:
            await self.auth.create_user(s.admin_email, s.admin_password, name="Administrator", role="owner")
            log.info("created initial owner account %s", s.admin_email)
        self._started = True
        if s.embedded_worker if run_worker is None else run_worker:
            self.worker = Worker(self, location=s.worker_location, concurrency=s.worker_concurrency)
            self.worker.start()
        await self.events.emit("system.started", f"AI-OS kernel started ({s.deployment_mode} mode, node {s.node_name})",
                               data={"mode": s.deployment_mode, "location": s.worker_location})

    async def stop(self) -> None:
        if self.worker:
            await self.worker.stop()
            self.worker = None
        await self.db.dispose()
        self._started = False

    # --- emergency control -----------------------------------------------------------

    async def emergency_stop(self, *, by: str, reason: str = "") -> dict:
        state = await self.killswitch.set(True, by=by, reason=reason)
        await self.audit.record(by, "system.emergency_stop", detail={"reason": reason})
        await self.events.emit("system.emergency_stop", f"EMERGENCY STOP engaged: {reason or 'no reason given'}", actor=by, level="error")
        return state

    async def resume_operations(self, *, by: str) -> dict:
        state = await self.killswitch.set(False, by=by)
        await self.audit.record(by, "system.resume")
        resumed = await self.orchestrator.resume_halted()
        await self.events.emit("system.resumed", f"Operations resumed; {resumed} halted run(s) re-queued", actor=by, level="warn")
        self.queue.wake.set()
        return {**state, "resumed_runs": resumed}
