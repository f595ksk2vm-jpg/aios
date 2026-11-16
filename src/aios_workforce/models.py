"""Layer 2 schema."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import JSON, DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aios_kernel.db import Base, utcnow
from aios_kernel.models import DictMixin


class Department(Base, DictMixin):
    __tablename__ = "wf_departments"
    __table_args__ = (UniqueConstraint("namespace_id", "name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    namespace_id: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text, default="")
    head_agent_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class CustomTemplate(Base, DictMixin):
    __tablename__ = "wf_templates"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    spec: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(60), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
