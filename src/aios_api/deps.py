from __future__ import annotations

from fastapi import Depends, Request

from aios_kernel.errors import Unauthenticated
from aios_kernel.kernel import Kernel
from aios_kernel.security.auth import Principal


def get_kernel(request: Request) -> Kernel:
    return request.app.state.kernel


async def current(request: Request, k: Kernel = Depends(get_kernel)) -> Principal:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise Unauthenticated("missing bearer token")
    return await k.auth.authenticate(header[7:].strip())


def require(permission: str):
    """Dependency factory: authenticated user holding ``permission``."""
    async def dep(p: Principal = Depends(current)) -> Principal:
        p.require(permission)
        return p
    return dep


def dicts(rows) -> list[dict]:
    return [r.to_dict() for r in rows]
