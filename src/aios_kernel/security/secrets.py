"""Encrypted secret store.

Values are encrypted at rest with a master key (Fernet: AES-128-CBC + HMAC).
Secrets are resolved by name at the moment a tool or provider needs them; they
never enter a prompt, a transcript, an event or an API response.
"""
from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from ..db import Database, new_id, utcnow
from ..errors import AiosError, NotFound
from ..models import ROOT_NS, Secret


def load_master_key(configured: str | None, data_dir: Path) -> bytes:
    if configured:
        return configured.encode()
    path = data_dir / "master.key"
    if path.exists():
        return path.read_bytes().strip()
    data_dir.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


class SecretStore:
    def __init__(self, db: Database, master_key: bytes):
        self.db = db
        try:
            self._fernet = Fernet(master_key)
        except ValueError as e:
            raise AiosError("AIOS_SECRET_KEY must be a urlsafe-base64 32-byte Fernet key") from e

    async def put(self, name: str, value: str, *, namespace_id: str = ROOT_NS, created_by: str = "") -> None:
        ct = self._fernet.encrypt(value.encode()).decode()
        async with self.db.session() as s:
            row = (await s.execute(select(Secret).where(Secret.namespace_id == namespace_id, Secret.name == name))).scalar_one_or_none()
            if row:
                row.ciphertext, row.rotated_at = ct, utcnow()
            else:
                s.add(Secret(id=new_id("sec"), namespace_id=namespace_id, name=name, ciphertext=ct, created_by=created_by))

    async def get(self, name: str, namespace_chain: list[str] | None = None) -> str | None:
        """Resolve ``name`` in the nearest namespace of ``namespace_chain`` (self first, root last)."""
        chain = namespace_chain or [ROOT_NS]
        async with self.db.session() as s:
            rows = {r.namespace_id: r for r in (await s.execute(
                select(Secret).where(Secret.name == name, Secret.namespace_id.in_(chain)))).scalars()}
        for ns in chain:
            if ns in rows:
                try:
                    return self._fernet.decrypt(rows[ns].ciphertext.encode()).decode()
                except InvalidToken as e:
                    raise AiosError(f"secret {name!r} cannot be decrypted with the current master key") from e
        return None

    async def delete(self, name: str, namespace_id: str = ROOT_NS) -> None:
        async with self.db.session() as s:
            row = (await s.execute(select(Secret).where(Secret.namespace_id == namespace_id, Secret.name == name))).scalar_one_or_none()
            if not row:
                raise NotFound("secret not found")
            await s.delete(row)

    async def list(self) -> list[Secret]:
        async with self.db.session() as s:
            return list((await s.execute(select(Secret).order_by(Secret.namespace_id, Secret.name))).scalars())
