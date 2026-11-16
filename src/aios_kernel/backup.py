"""Backup and recovery.

A backup is one ``.tar.gz``: a consistent database snapshot, every workspace
(including git history) and a manifest. The master key is *not* included
unless asked for — a backup that contains both the encrypted secrets and their
key is as sensitive as the secrets themselves.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path

from .db import utcnow
from .errors import AiosError


def _sqlite_path(url: str) -> Path:
    return Path(url.split("///", 1)[1])


def _snapshot_db(settings, dest: Path) -> str:
    if settings.is_sqlite:
        src = sqlite3.connect(_sqlite_path(settings.database_url))
        dst = sqlite3.connect(dest / "aios.db")
        try:
            src.backup(dst)  # online, consistent snapshot even while the system is running
        finally:
            src.close()
            dst.close()
        return "sqlite"
    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        raise AiosError("pg_dump is required on this host to back up a PostgreSQL database")
    import subprocess
    url = settings.database_url.replace("+asyncpg", "")
    res = subprocess.run([pg_dump, "--format=custom", "--file", str(dest / "aios.pgdump"), url], capture_output=True, text=True)
    if res.returncode != 0:
        raise AiosError(f"pg_dump failed: {res.stderr[:500]}")
    return "postgresql"


def create_backup_sync(settings, *, include_key: bool = False) -> dict:
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    target = settings.backups_dir / f"aios-backup-{stamp}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        backend = _snapshot_db(settings, tmp_path)
        manifest = {"created_at": stamp, "database": backend, "includes_master_key": include_key,
                    "deployment_mode": settings.deployment_mode, "format": 1}
        (tmp_path / "manifest.json").write_text(json.dumps(manifest, indent=1))
        with tarfile.open(target, "w:gz") as tar:
            for item in tmp_path.iterdir():
                tar.add(item, arcname=item.name)
            if settings.workspaces_dir.exists():
                tar.add(settings.workspaces_dir, arcname="workspaces")
            key = settings.data_dir / "master.key"
            if include_key and key.exists():
                tar.add(key, arcname="master.key")
    return {**manifest, "file": target.name, "path": str(target), "size_bytes": target.stat().st_size}


async def create_backup(settings, *, include_key: bool = False) -> dict:
    return await asyncio.to_thread(create_backup_sync, settings, include_key=include_key)


def list_backups(settings) -> list[dict]:
    if not settings.backups_dir.exists():
        return []
    return [{"file": p.name, "size_bytes": p.stat().st_size, "created_at": p.name[len("aios-backup-"):-len(".tar.gz")]}
            for p in sorted(settings.backups_dir.glob("aios-backup-*.tar.gz"), reverse=True)]


def restore_backup_sync(settings, archive: Path, *, force: bool = False) -> dict:
    """Restore into ``settings.data_dir``. Run with the system stopped."""
    if not archive.exists():
        raise AiosError(f"{archive} does not exist")
    with tarfile.open(archive) as tar:
        manifest = json.loads(tar.extractfile("manifest.json").read())
        if manifest["database"] == "sqlite":
            db_path = _sqlite_path(settings.database_url) if settings.is_sqlite else None
            if db_path is None:
                raise AiosError("this backup holds a SQLite database but the configured database is not SQLite")
            if db_path.exists() and not force:
                raise AiosError(f"{db_path} already exists; pass --force to overwrite")
        if settings.workspaces_dir.exists() and any(settings.workspaces_dir.iterdir()) and not force:
            raise AiosError("workspaces directory is not empty; pass --force to overwrite")
        with tempfile.TemporaryDirectory() as tmp:
            tar.extractall(tmp, filter="data")  # refuses absolute paths, links escaping the target, device files
            tmp_path = Path(tmp)
            settings.data_dir.mkdir(parents=True, exist_ok=True)
            if manifest["database"] == "sqlite":
                for suffix in ("", "-wal", "-shm"):
                    Path(str(db_path) + suffix).unlink(missing_ok=True)
                shutil.copy2(tmp_path / "aios.db", db_path)
            else:
                shutil.copy2(tmp_path / "aios.pgdump", settings.data_dir / "aios.pgdump")
            if (tmp_path / "workspaces").exists():
                if settings.workspaces_dir.exists():
                    shutil.rmtree(settings.workspaces_dir)
                shutil.copytree(tmp_path / "workspaces", settings.workspaces_dir, symlinks=True)
            if (tmp_path / "master.key").exists():
                shutil.copy2(tmp_path / "master.key", settings.data_dir / "master.key")
    note = ("PostgreSQL dump written to data_dir/aios.pgdump; load it with pg_restore." if manifest["database"] != "sqlite" else
            "Database and workspaces restored.")
    if not manifest.get("includes_master_key"):
        note += " The master key was not in this backup: provide the original AIOS_SECRET_KEY / master.key to read stored secrets."
    return {**manifest, "note": note}
