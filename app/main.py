"""Agent Budget Controller — application factory."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress

import structlog
from fastapi import FastAPI
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import (
    admin_agents,
    admin_config,
    admin_controls,
    admin_models,
    proxy,
    status as status_api,
    stream,
)
from app.config import settings
from app.core import upstream
from app.core.pricing import pricing
from app.db.session import dispose_engine, init_engine, session_scope
from app.logging_setup import configure_logging
from app.redisx.client import gateway
from app.workers import reaper

log = structlog.get_logger(__name__)

STATIC_DIR = "app/static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.log_level, settings.log_json)
    log.info(
        "startup",
        app=settings.app_name,
        dev_mode=settings.dev_mode,
        fail_mode=settings.enforcement_fail_mode,
    )
    init_engine()
    await gateway.connect()
    await upstream.connect()

    # Pricing is read on every request, so it is mirrored in memory at startup
    # rather than joined per call.
    async with session_scope() as session:
        await pricing.load(session)

    reaper_task = asyncio.create_task(reaper.run_forever())

    try:
        yield
    finally:
        reaper_task.cancel()
        with suppress(asyncio.CancelledError):
            await reaper_task
        await upstream.close()
        await gateway.close()
        await dispose_engine()
        log.info("shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description=(
            "Enforces LLM token spend limits per team, agent and session "
            "*before* a request is dispatched upstream."
        ),
        lifespan=lifespan,
    )

    @app.exception_handler(StarletteHTTPException)
    async def unwrap_error_envelope(request, exc: StarletteHTTPException):
        """Give every error one shape: ``{"error": {type, message, ...}}``.

        Endpoints that raise HTTPException would otherwise have their envelope
        nested a level deeper (``{"detail": {"error": …}}``) than the ones that
        return a JSONResponse directly, so a client would need to know which
        layer refused it before it could read why. Errors FastAPI raises on its
        own — a routing 404, a 405 — keep the default shape.
        """
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(
                status_code=exc.status_code,
                content=exc.detail,
                headers=getattr(exc, "headers", None),
            )
        return await http_exception_handler(request, exc)

    @app.get("/health", tags=["ops"])
    async def health() -> JSONResponse:
        redis_ok = await gateway.healthy()

        db_ok = True
        try:
            from sqlalchemy import text

            from app.db.session import session_scope

            async with session_scope() as s:
                await s.execute(text("SELECT 1"))
        except Exception as exc:  # pragma: no cover - surfaced in the payload
            db_ok = False
            log.warning("health.db_failed", error=str(exc))

        healthy = redis_ok and db_ok
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={
                "status": "ok" if healthy else "degraded",
                "dev_mode": settings.dev_mode,
                "redis": "ok" if redis_ok else "unavailable",
                "database": "ok" if db_ok else "unavailable",
                "enforcement_fail_mode": settings.enforcement_fail_mode,
            },
        )

    app.include_router(proxy.router)
    app.include_router(status_api.router)
    app.include_router(stream.router)
    app.include_router(admin_agents.router)
    app.include_router(admin_config.router)
    app.include_router(admin_models.router)
    app.include_router(admin_controls.router)

    class RevalidatingStatics(StaticFiles):
        """Serve the dashboard's assets with must-revalidate.

        The default lets a browser hold an ES module indefinitely, so a
        deployed fix appears not to have shipped until someone thinks to
        hard-reload. These files are a few kilobytes and the ETag makes
        revalidation a 304, so the bandwidth saved by caching them is not worth
        the confusion of stale code.
        """

        def is_not_modified(self, response_headers, request_headers) -> bool:
            response_headers["Cache-Control"] = "no-cache, must-revalidate"
            return super().is_not_modified(response_headers, request_headers)

        async def get_response(self, path: str, scope):
            response = await super().get_response(path, scope)
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
            return response

    app.mount("/static", RevalidatingStatics(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    @app.get("/dashboard", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(f"{STATIC_DIR}/index.html")

    return app


app = create_app()
