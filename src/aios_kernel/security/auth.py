"""Human identity: users, password login, opaque bearer tokens, role-based access."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import os
import secrets as pysecrets
from dataclasses import dataclass

from sqlalchemy import func, select

from ..db import Database, new_id, utcnow
from ..errors import Conflict, NotFound, PermissionDenied, Unauthenticated
from ..models import AuthToken, User

# Role → permissions held by human operators of the control plane.
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "viewer": {"read"},
    "operator": {"read", "tasks:run", "approvals:decide", "memory:write", "business:manage"},
    "admin": {"read", "tasks:run", "approvals:decide", "memory:write", "business:manage", "workforce:manage",
              "models:manage", "secrets:manage", "system:control", "users:manage"},
}
ROLE_PERMISSIONS["owner"] = ROLE_PERMISSIONS["admin"] | {"system:owner"}
ROLES = list(ROLE_PERMISSIONS)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_b64, dk_b64 = stored.split("$")
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(dk_b64)
    except ValueError:
        return False
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(dk, expected)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Principal:
    """An authenticated human. Agents are never principals of the control API."""
    user: User

    @property
    def actor(self) -> str:
        return f"user:{self.user.id}"

    def can(self, permission: str) -> bool:
        return permission in ROLE_PERMISSIONS.get(self.user.role, set())

    def require(self, permission: str) -> None:
        if not self.can(permission):
            raise PermissionDenied(f"role '{self.user.role}' lacks permission '{permission}'")


class AuthService:
    def __init__(self, db: Database, session_ttl_hours: int = 12):
        self.db = db
        self.session_ttl = dt.timedelta(hours=session_ttl_hours)

    async def user_count(self) -> int:
        async with self.db.session() as s:
            return (await s.execute(select(func.count()).select_from(User))).scalar_one()

    async def create_user(self, email: str, password: str, *, name: str = "", role: str = "viewer") -> User:
        if role not in ROLE_PERMISSIONS:
            raise Conflict(f"unknown role {role!r}")
        if len(password) < 8:
            raise Conflict("password must be at least 8 characters")
        email = email.strip().lower()
        async with self.db.session() as s:
            if (await s.execute(select(User).where(User.email == email))).scalar_one_or_none():
                raise Conflict("a user with this email already exists")
            u = User(id=new_id("usr"), email=email, name=name or email.split("@")[0],
                     password_hash=hash_password(password), role=role)
            s.add(u)
        return u

    async def list_users(self) -> list[User]:
        async with self.db.session() as s:
            return list((await s.execute(select(User).order_by(User.created_at))).scalars())

    async def update_user(self, user_id: str, *, role: str | None = None, is_active: bool | None = None,
                          password: str | None = None) -> User:
        async with self.db.session() as s:
            u = await s.get(User, user_id)
            if not u:
                raise NotFound("user not found")
            if role is not None:
                if role not in ROLE_PERMISSIONS:
                    raise Conflict(f"unknown role {role!r}")
                u.role = role
            if is_active is not None:
                u.is_active = is_active
            if password:
                u.password_hash = hash_password(password)
        return u

    async def _issue(self, user_id: str, kind: str, name: str, ttl: dt.timedelta | None) -> str:
        token = ("aios_sk_" if kind == "api_key" else "aios_st_") + pysecrets.token_urlsafe(32)
        async with self.db.session() as s:
            s.add(AuthToken(id=new_id("tok"), user_id=user_id, token_hash=_hash_token(token), kind=kind, name=name,
                            expires_at=utcnow() + ttl if ttl else None))
        return token

    async def login(self, email: str, password: str) -> tuple[str, User]:
        async with self.db.session() as s:
            u = (await s.execute(select(User).where(User.email == email.strip().lower()))).scalar_one_or_none()
        if not u or not u.is_active or not verify_password(password, u.password_hash):
            raise Unauthenticated("invalid credentials")
        return await self._issue(u.id, "session", "login", self.session_ttl), u

    async def create_api_key(self, user_id: str, name: str) -> str:
        return await self._issue(user_id, "api_key", name, None)

    async def authenticate(self, token: str) -> Principal:
        async with self.db.session() as s:
            t = (await s.execute(select(AuthToken).where(AuthToken.token_hash == _hash_token(token)))).scalar_one_or_none()
            if not t or t.revoked_at or (t.expires_at and t.expires_at < utcnow()):
                raise Unauthenticated("invalid or expired token")
            u = await s.get(User, t.user_id)
        if not u or not u.is_active:
            raise Unauthenticated("user disabled")
        return Principal(u)

    async def revoke(self, token: str) -> None:
        async with self.db.session() as s:
            t = (await s.execute(select(AuthToken).where(AuthToken.token_hash == _hash_token(token)))).scalar_one_or_none()
            if t:
                t.revoked_at = utcnow()
