"""FastAPI application: API-first control plane plus the command-center UI as static files."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from aios_kernel import __version__
from aios_kernel.config import Settings
from aios_kernel.errors import AiosError

from .routers import ecosystem, infra, system, workforce
from .system import build

log = logging.getLogger("aios.api")
UI_DIST = Path(os.environ.get("AIOS_UI_DIST") or Path(__file__).resolve().parents[2] / "ui" / "dist")


def create_app(settings: Settings | None = None) -> FastAPI:
    kernel = build(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await kernel.start()
        kernel.extensions["ecosystem"].start_scheduler()
        if await kernel.auth.user_count() == 0:
            log.warning("No user accounts yet: open the UI to create the owner account, or set AIOS_ADMIN_EMAIL / AIOS_ADMIN_PASSWORD.")
        yield
        await kernel.extensions["ecosystem"].stop()
        await kernel.stop()

    app = FastAPI(title="AI-OS", version=__version__, lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.kernel = kernel

    @app.exception_handler(AiosError)
    async def aios_error(_: Request, exc: AiosError):
        return JSONResponse({"error": type(exc).__name__, "detail": str(exc)}, status_code=exc.status)

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "version": __version__}

    # One router per layer: each layer's surface can evolve (or be deployed) independently.
    for r in (system.router, infra.router, workforce.router, ecosystem.router):
        app.include_router(r)

    if UI_DIST.exists():
        app.mount("/assets", StaticFiles(directory=UI_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            if path.startswith("api/"):
                return JSONResponse({"error": "NotFound", "detail": "unknown API route"}, status_code=404)
            candidate = (UI_DIST / path).resolve()
            if path and candidate.is_file() and UI_DIST.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(UI_DIST / "index.html")
    return app
