"""Runtime configuration.

Everything that differs between cloud, local and hybrid deployments is a
setting here; no module hardcodes a provider, a database or a filesystem
layout. Values come from ``AIOS_*`` environment variables.
"""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(f"AIOS_{name}", default)


def _bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # --- deployment topology -------------------------------------------------
    # cloud | local | hybrid. Informational for the control plane; actual
    # placement is decided per worker (worker_location) and per namespace.
    deployment_mode: str = "local"
    # Where *this process* runs. Workers only claim jobs whose affinity is
    # their own location or "any".
    worker_location: str = "local"
    node_name: str = field(default_factory=socket.gethostname)

    # --- state ---------------------------------------------------------------
    database_url: str = ""
    data_dir: Path = Path("./data")

    # --- security ------------------------------------------------------------
    # Master key for the secret store (Fernet, urlsafe base64). If unset a key
    # file is created under data_dir on first start (fine for local, set it
    # explicitly in cloud deployments).
    secret_key: str | None = None
    session_ttl_hours: int = 12
    admin_email: str | None = None
    admin_password: str | None = None

    # --- execution -----------------------------------------------------------
    embedded_worker: bool = True
    worker_concurrency: int = 4
    sandbox_driver: str = "auto"  # auto | subprocess | docker
    sandbox_docker_image: str = "python:3.12-slim"
    sandbox_timeout_s: int = 60
    sandbox_memory_mb: int = 1024
    max_run_steps: int = 40

    # --- network policy for agent tools -----------------------------------------
    http_allow_private: bool = False
    http_allowed_hosts: tuple[str, ...] = ()  # empty = any public host
    search_url: str | None = None  # SearxNG-compatible JSON search endpoint

    # --- memory --------------------------------------------------------------
    embedder: str = "hashing"  # hashing | openai_compat
    embedder_base_url: str | None = None
    embedder_model: str | None = None

    # --- dev -----------------------------------------------------------------
    enable_scripted_provider: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(_env("DATA_DIR", "./data")).resolve()
        hosts = tuple(h.strip() for h in (_env("HTTP_ALLOWED_HOSTS", "") or "").split(",") if h.strip())
        s = cls(
            deployment_mode=_env("DEPLOYMENT_MODE", "local"),
            worker_location=_env("WORKER_LOCATION", "local"),
            node_name=_env("NODE_NAME", socket.gethostname()),
            database_url=_env("DATABASE_URL", "") or "",
            data_dir=data_dir,
            secret_key=_env("SECRET_KEY"),
            session_ttl_hours=int(_env("SESSION_TTL_HOURS", "12")),
            admin_email=_env("ADMIN_EMAIL"),
            admin_password=_env("ADMIN_PASSWORD"),
            embedded_worker=_bool("EMBEDDED_WORKER", True),
            worker_concurrency=int(_env("WORKER_CONCURRENCY", "4")),
            sandbox_driver=_env("SANDBOX_DRIVER", "auto"),
            sandbox_docker_image=_env("SANDBOX_DOCKER_IMAGE", "python:3.12-slim"),
            sandbox_timeout_s=int(_env("SANDBOX_TIMEOUT_S", "60")),
            sandbox_memory_mb=int(_env("SANDBOX_MEMORY_MB", "1024")),
            max_run_steps=int(_env("MAX_RUN_STEPS", "40")),
            http_allow_private=_bool("HTTP_ALLOW_PRIVATE", False),
            http_allowed_hosts=hosts,
            search_url=_env("SEARCH_URL"),
            embedder=_env("EMBEDDER", "hashing"),
            embedder_base_url=_env("EMBEDDER_BASE_URL"),
            embedder_model=_env("EMBEDDER_MODEL"),
            enable_scripted_provider=_bool("ENABLE_SCRIPTED_PROVIDER", False),
        )
        s.finalize()
        return s

    def finalize(self) -> None:
        self.data_dir = Path(self.data_dir).resolve()
        if self.deployment_mode not in {"cloud", "local", "hybrid"}:
            raise ValueError(f"AIOS_DEPLOYMENT_MODE must be cloud|local|hybrid, got {self.deployment_mode!r}")
        if self.worker_location not in {"cloud", "local"}:
            raise ValueError(f"AIOS_WORKER_LOCATION must be cloud|local, got {self.worker_location!r}")
        if not self.database_url:
            self.database_url = f"sqlite+aiosqlite:///{self.data_dir / 'aios.db'}"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    def public(self) -> dict:
        """Non-sensitive view for the control interface."""
        url = self.database_url
        backend = url.split(":", 1)[0].split("+", 1)[0]
        return {
            "deployment_mode": self.deployment_mode,
            "worker_location": self.worker_location,
            "node_name": self.node_name,
            "database_backend": backend,
            "data_dir": str(self.data_dir),
            "embedded_worker": self.embedded_worker,
            "worker_concurrency": self.worker_concurrency,
            "sandbox_driver": self.sandbox_driver,
            "embedder": self.embedder,
            "http_allow_private": self.http_allow_private,
            "http_allowed_hosts": list(self.http_allowed_hosts),
        }
