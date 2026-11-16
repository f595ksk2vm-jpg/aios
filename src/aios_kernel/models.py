"""Kernel (Layer 1) schema.

Only infrastructure concepts live here. A *namespace* is the kernel's isolation
primitive; upper layers decide what a namespace means (a business, a project).
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow

ROOT_NS = "ns_root"


def _ts() -> Mapped[dt.datetime]:
    return mapped_column(DateTime, default=utcnow)


class DictMixin:
    _hidden: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out = {}
        for col in self.__table__.columns:  # type: ignore[attr-defined]
            if col.name in self._hidden:
                continue
            v = getattr(self, col.name)
            if isinstance(v, dt.datetime):
                v = v.isoformat() + "Z"
            elif isinstance(v, (bytes, bytearray)):
                continue
            out[col.name] = v
        return out


# --- identity & access ---------------------------------------------------------

class User(Base, DictMixin):
    __tablename__ = "users"
    _hidden = ("password_hash",)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="viewer")  # owner|admin|operator|viewer
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = _ts()


class AuthToken(Base, DictMixin):
    __tablename__ = "auth_tokens"
    _hidden = ("token_hash",)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(20), default="session")  # session|api_key
    name: Mapped[str] = mapped_column(String(100), default="")
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = _ts()


class Secret(Base, DictMixin):
    __tablename__ = "secrets"
    _hidden = ("ciphertext",)
    __table_args__ = (UniqueConstraint("namespace_id", "name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    namespace_id: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(100))
    ciphertext: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[dt.datetime] = _ts()
    rotated_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


# --- namespaces (isolation) ------------------------------------------------------

class Namespace(Base, DictMixin):
    __tablename__ = "namespaces"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))  # opaque to the kernel: system|business|project|...
    name: Mapped[str] = mapped_column(String(255))
    parent_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|paused|archived
    # Placement policy: where this namespace's workloads may run and which
    # models may see its data. any|local|cloud.
    compute: Mapped[str] = mapped_column(String(10), default="any")
    models: Mapped[str] = mapped_column(String(10), default="any")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = _ts()


class Grant(Base, DictMixin):
    """Explicit, revocable permission for one namespace to use another's resources."""
    __tablename__ = "grants"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    from_ns: Mapped[str] = mapped_column(String(40), index=True)  # owner of the resource
    to_ns: Mapped[str] = mapped_column(String(40), index=True)    # who may use it
    resource_kind: Mapped[str] = mapped_column(String(20))        # knowledge|agent|workflow
    resource_id: Mapped[str | None] = mapped_column(String(40), nullable=True)  # None = all of that kind
    created_by: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[dt.datetime] = _ts()
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


# --- models ------------------------------------------------------------------------

class ModelProvider(Base, DictMixin):
    __tablename__ = "model_providers"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    kind: Mapped[str] = mapped_column(String(30))  # anthropic|openai_compat|scripted
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    secret_name: Mapped[str | None] = mapped_column(String(100), nullable=True)  # reference into the secret store
    location: Mapped[str] = mapped_column(String(10), default="cloud")  # cloud|local
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = _ts()


class ModelEntry(Base, DictMixin):
    __tablename__ = "model_catalog"
    __table_args__ = (UniqueConstraint("provider_id", "model_name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    provider_id: Mapped[str] = mapped_column(ForeignKey("model_providers.id"), index=True)
    model_name: Mapped[str] = mapped_column(String(120))
    display_name: Mapped[str] = mapped_column(String(120), default="")
    tier: Mapped[str] = mapped_column(String(12), default="balanced")  # fast|balanced|deep
    context_window: Mapped[int] = mapped_column(Integer, default=128000)
    price_in: Mapped[float] = mapped_column(Float, default=0.0)   # USD per million input tokens
    price_out: Mapped[float] = mapped_column(Float, default=0.0)  # USD per million output tokens
    latency_class: Mapped[int] = mapped_column(Integer, default=2)  # 1 fast .. 3 slow
    supports_tools: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class UsageRecord(Base, DictMixin):
    __tablename__ = "usage_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    namespace_id: Mapped[str] = mapped_column(String(40), index=True)
    agent_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    kind: Mapped[str] = mapped_column(String(12), default="llm")  # llm|compute
    provider: Mapped[str] = mapped_column(String(60), default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)


class Budget(Base, DictMixin):
    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("scope_kind", "scope_id", "period"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    scope_kind: Mapped[str] = mapped_column(String(12))  # global|namespace|agent
    scope_id: Mapped[str] = mapped_column(String(40), default="")
    period: Mapped[str] = mapped_column(String(8), default="month")  # day|month|total
    limit_usd: Mapped[float] = mapped_column(Float)
    created_at: Mapped[dt.datetime] = _ts()


# --- agents ------------------------------------------------------------------------

class Agent(Base, DictMixin):
    __tablename__ = "agents"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    namespace_id: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(80), index=True)       # machine key, e.g. backend_developer
    title: Mapped[str] = mapped_column(String(120), default="")     # human title
    department: Mapped[str] = mapped_column(String(80), default="")
    manager_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    # {"mode":"auto","tier":"balanced","prefer":"balanced"} or {"mode":"pinned","model":"provider/model"}
    model: Mapped[dict] = mapped_column(JSON, default=dict)
    tools: Mapped[list] = mapped_column(JSON, default=list)          # tool names offered to the model
    permissions: Mapped[list] = mapped_column(JSON, default=list)    # permission patterns
    autonomy: Mapped[str] = mapped_column(String(12), default="supervised")
    status: Mapped[str] = mapped_column(String(12), default="active")  # active|paused|deactivated
    exec_env: Mapped[str] = mapped_column(String(10), default="any")   # any|local|cloud
    created_by: Mapped[str] = mapped_column(String(60), default="")    # "user:<id>" | "agent:<id>" | "system"
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = _ts()
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AgentMessage(Base, DictMixin):
    __tablename__ = "agent_messages"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    namespace_id: Mapped[str] = mapped_column(String(40), index=True)
    from_agent: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)  # None = system/human
    to_agent: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)    # None = humans
    kind: Mapped[str] = mapped_column(String(20), default="message")  # message|delegation|question|answer|review|escalation|report
    subject: Mapped[str] = mapped_column(String(255), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    read_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


# --- work --------------------------------------------------------------------------

class Objective(Base, DictMixin):
    __tablename__ = "objectives"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    namespace_id: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    # planning|running|needs_attention|completed|failed|cancelled
    status: Mapped[str] = mapped_column(String(20), default="planning", index=True)
    owner_agent_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_by: Mapped[str] = mapped_column(String(60), default="")
    result: Mapped[str] = mapped_column(Text, default="")
    evaluation: Mapped[dict] = mapped_column(JSON, default=dict)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = _ts()
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class Task(Base, DictMixin):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    objective_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    namespace_id: Mapped[str] = mapped_column(String(40), index=True)
    parent_task_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(12), default="work")  # work|plan|review|finalize
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    # pending|ready|running|waiting|awaiting_approval|paused|done|failed|blocked|cancelled
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=5)  # 1 highest .. 9 lowest
    assigned_agent_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    required_role: Mapped[str] = mapped_column(String(80), default="")
    depends_on: Mapped[list] = mapped_column(JSON, default=list)  # task ids
    result: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = _ts()
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class Run(Base, DictMixin):
    """One execution of a task by an agent. Holds the full, resumable transcript."""
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(40), index=True)
    agent_id: Mapped[str] = mapped_column(String(40), index=True)
    namespace_id: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(12), default="running")  # running|suspended|completed|failed|cancelled
    messages: Mapped[list] = mapped_column(JSON, default=list)
    pending: Mapped[dict] = mapped_column(JSON, default=dict)  # why the run is suspended
    options: Mapped[dict] = mapped_column(JSON, default=dict)  # run-scoped tools, required tool, ...
    steps: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str] = mapped_column(String(180), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[dt.datetime] = _ts()
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class Job(Base, DictMixin):
    """Durable background job. The queue is a table so it works on any SQL backend."""
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_claim", "status", "run_after", "priority"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(12), default="queued")  # queued|running|done|failed|dead
    priority: Mapped[int] = mapped_column(Integer, default=5)
    affinity: Mapped[str] = mapped_column(String(10), default="any")  # any|local|cloud
    run_after: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    locked_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    heartbeat_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = _ts()
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class WorkerNode(Base, DictMixin):
    __tablename__ = "workers"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    node_name: Mapped[str] = mapped_column(String(120))
    location: Mapped[str] = mapped_column(String(10))
    concurrency: Mapped[int] = mapped_column(Integer, default=1)
    active_jobs: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[dt.datetime] = _ts()
    heartbeat_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class Approval(Base, DictMixin):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    namespace_id: Mapped[str] = mapped_column(String(40), index=True)
    agent_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    action: Mapped[str] = mapped_column(String(80))      # tool name or logical action
    category: Mapped[str] = mapped_column(String(30))
    summary: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)  # pending|approved|rejected
    decided_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = _ts()


# --- observability -------------------------------------------------------------------

class Event(Base, DictMixin):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    namespace_id: Mapped[str] = mapped_column(String(40), index=True, default=ROOT_NS)
    kind: Mapped[str] = mapped_column(String(60), index=True)
    level: Mapped[str] = mapped_column(String(8), default="info")  # info|warn|error
    actor: Mapped[str] = mapped_column(String(60), default="system")
    subject: Mapped[str] = mapped_column(String(80), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class AuditEntry(Base, DictMixin):
    """Append-only, hash-chained record of security-relevant actions."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(60))
    action: Mapped[str] = mapped_column(String(80), index=True)
    resource: Mapped[str] = mapped_column(String(120), default="")
    namespace_id: Mapped[str] = mapped_column(String(40), default=ROOT_NS)
    outcome: Mapped[str] = mapped_column(String(12), default="ok")  # ok|denied|error
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64), default="")
    hash: Mapped[str] = mapped_column(String(64), default="")


class SystemState(Base):
    __tablename__ = "system_state"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# --- memory --------------------------------------------------------------------------

class Document(Base, DictMixin):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    namespace_id: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(500), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = _ts()


class MemoryItem(Base, DictMixin):
    __tablename__ = "memory_items"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    namespace_id: Mapped[str] = mapped_column(String(40), index=True)
    scope: Mapped[str] = mapped_column(String(12), default="namespace")  # namespace|agent
    agent_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(20), default="note")  # note|fact|result|document|conversation
    document_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    content: Mapped[str] = mapped_column(Text)
    embedder: Mapped[str] = mapped_column(String(80), default="")
    embedding: Mapped[bytes] = mapped_column(LargeBinary)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
