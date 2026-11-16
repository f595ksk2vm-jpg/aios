"""Layer 3 schema."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aios_kernel.db import Base, utcnow
from aios_kernel.models import DictMixin


def _ts() -> Mapped[dt.datetime]:
    return mapped_column(DateTime, default=utcnow)


class Business(Base, DictMixin):
    __tablename__ = "eco_businesses"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    namespace_id: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    idea: Mapped[str] = mapped_column(Text, default="")
    mission: Mapped[str] = mapped_column(Text, default="")
    business_model: Mapped[str] = mapped_column(Text, default="")
    strategy: Mapped[str] = mapped_column(Text, default="")
    stage: Mapped[str] = mapped_column(String(20), default="idea")  # idea|validation|building|operating
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|paused|archived
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    created_by: Mapped[str] = mapped_column(String(60), default="")
    created_at: Mapped[dt.datetime] = _ts()


class Project(Base, DictMixin):
    __tablename__ = "eco_projects"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    business_id: Mapped[str | None] = mapped_column(ForeignKey("eco_businesses.id"), nullable=True, index=True)
    namespace_id: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(30), default="software")
    description: Mapped[str] = mapped_column(Text, default="")
    requirements: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|paused|completed|archived
    created_by: Mapped[str] = mapped_column(String(60), default="")
    created_at: Mapped[dt.datetime] = _ts()


class Deliverable(Base, DictMixin):
    __tablename__ = "eco_deliverables"
    __table_args__ = (UniqueConstraint("namespace_id", "path"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    namespace_id: Mapped[str] = mapped_column(String(40), index=True)
    business_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    project_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    path: Mapped[str] = mapped_column(String(500))
    kind: Mapped[str] = mapped_column(String(30), default="file")
    summary: Mapped[str] = mapped_column(Text, default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    commit: Mapped[str] = mapped_column(String(40), default="")
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[dt.datetime] = _ts()
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Customer(Base, DictMixin):
    __tablename__ = "eco_customers"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    business_id: Mapped[str] = mapped_column(ForeignKey("eco_businesses.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), default="")
    company: Mapped[str] = mapped_column(String(255), default="")
    stage: Mapped[str] = mapped_column(String(20), default="lead")  # lead|qualified|customer|churned
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = _ts()
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class LedgerEntry(Base, DictMixin):
    __tablename__ = "eco_ledger"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    business_id: Mapped[str] = mapped_column(ForeignKey("eco_businesses.id"), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(10))  # revenue|expense
    category: Mapped[str] = mapped_column(String(60), default="general")
    amount: Mapped[float] = mapped_column(Float)
    memo: Mapped[str] = mapped_column(Text, default="")
    recorded_by: Mapped[str] = mapped_column(String(60), default="")


class Kpi(Base, DictMixin):
    __tablename__ = "eco_kpis"
    __table_args__ = (UniqueConstraint("business_id", "key"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    business_id: Mapped[str] = mapped_column(ForeignKey("eco_businesses.id"), index=True)
    key: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(120))
    unit: Mapped[str] = mapped_column(String(20), default="")
    target: Mapped[float | None] = mapped_column(Float, nullable=True)
    direction: Mapped[str] = mapped_column(String(4), default="up")  # up|down = which way is good
    created_at: Mapped[dt.datetime] = _ts()


class KpiPoint(Base, DictMixin):
    __tablename__ = "eco_kpi_points"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kpi_id: Mapped[str] = mapped_column(ForeignKey("eco_kpis.id"), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    value: Mapped[float] = mapped_column(Float)
    note: Mapped[str] = mapped_column(String(255), default="")
    recorded_by: Mapped[str] = mapped_column(String(60), default="")


class Workflow(Base, DictMixin):
    """A reusable, optionally recurring objective template."""
    __tablename__ = "eco_workflows"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    namespace_id: Mapped[str] = mapped_column(String(40), index=True)  # owning business or project namespace
    name: Mapped[str] = mapped_column(String(255))
    objective_title: Mapped[str] = mapped_column(String(255))
    objective_description: Mapped[str] = mapped_column(Text, default="")
    interval_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = run manually
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(60), default="")
    created_at: Mapped[dt.datetime] = _ts()
