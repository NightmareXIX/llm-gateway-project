"""App factory, lifespan, middleware.

The lifespan owns every long-lived resource. Nothing is created at import time and
nothing is a module-level singleton, so a test can stand up an app against a test
database without inheriting a connection pool from the production configuration.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api import auth as auth_routes
from app.api import keys as keys_routes
from app.api.v1 import chat as chat_routes
from app.api.v1 import conversations as conversations_routes
from app.auth.jwt import JwksCache
from app.config import Settings, get_settings, validate_startup_config
from app.core.errors import error_response, register_exception_handlers
from app.core.logging import (
    RequestContextMiddleware,
    configure_logging,
    get_logger,
)
from app.db.session import create_db_engine, create_session_factory
from app.providers.registry import build_registry
from app.schemas.errors import DEFAULT_ERROR_RESPONSES, ErrorResponse

logger = get_logger("app.main")

READYZ_TIMEOUT_S = 5.0
"""Upper bound on the readiness check's database round-trip."""

HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
"""Defaults for the shared outbound client.

Sized for the JWKS fetch, which is the only caller in Phase 1 and sits on the
critical path of every authenticated request. Step 6's provider adapters need a
much longer read timeout — a completion takes tens of seconds — and pass their
own per-request timeout rather than changing this."""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    logger.info("startup.begin", env=settings.ENV)

    # The engine is created eagerly but connects lazily — startup does not fail
    # because Postgres happens to be slow to boot. /readyz is what reports that.
    engine = create_db_engine(settings)
    app.state.db_engine = engine
    app.state.db_session_factory = create_session_factory(engine)

    # One client for every outbound call the process makes, so connections are
    # pooled and TLS handshakes are amortized. Phase 1 uses it for JWKS; Step 6's
    # provider adapters share it rather than opening their own.
    http_client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
    app.state.http_client = http_client

    # Keys are fetched lazily on the first token, not here: a Supabase blip at
    # boot should not stop the process from starting and serving /healthz.
    app.state.jwks_cache = JwksCache(
        jwks_url=settings.supabase_jwks_url,
        client=http_client,
    )

    # Unlike the two above, this one is *eager* and may kill startup. Every
    # failure it can have — an enabled provider with no adapter, or with an empty
    # key — is a configuration mistake, and the alternative to failing here is
    # discovering it as a 502 on the first real message.
    registry = build_registry(client=http_client, settings=settings)
    app.state.provider_registry = registry

    # Still to come:
    #   Phase 3 — redis.asyncio pool + Lua script loading (app/cache/client.py)

    logger.info(
        "startup.complete",
        require_verified_email=settings.REQUIRE_VERIFIED_EMAIL,
        jwks_url=settings.supabase_jwks_url,
        slots=registry.describe(),
    )
    try:
        yield
    finally:
        # Mirror-image teardown, in reverse order of acquisition.
        await http_client.aclose()
        await engine.dispose()
        logger.info("shutdown.complete")


def create_app() -> FastAPI:
    """Build the application. Fails loudly here if configuration is bad."""
    configure_logging()
    validate_startup_config()

    settings = get_settings()

    app = FastAPI(
        title="LLM Gateway",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        # Documents the error envelope on every route — including ones reached
        # through include_router, which merge the app's responses with their own.
        # Without this the schema advertises FastAPI's `{"detail": ...}`, a shape
        # nothing here returns.
        responses=DEFAULT_ERROR_RESPONSES,
    )

    register_exception_handlers(app)
    app.add_middleware(RequestContextMiddleware)

    app.include_router(auth_routes.router)
    app.include_router(keys_routes.router)
    app.include_router(chat_routes.router)
    app.include_router(conversations_routes.router)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        """Liveness only. Answers as long as the process is up, so an orchestrator
        never restarts a healthy app because a dependency is having a bad minute."""
        return {"status": "ok"}

    @app.get(
        "/readyz",
        tags=["health"],
        responses={503: {"model": ErrorResponse, "description": "A dependency is unreachable."}},
    )
    async def readyz(request: Request) -> JSONResponse:
        """Readiness: can this instance actually serve a request?

        Postgres only. Redis is running in compose but nothing reads it until
        Phase 3, and a readiness probe that fails on an unused dependency takes
        the app out of rotation for no reason.
        """
        try:
            # Bounded: an unreachable database refuses fast, but a hung one would
            # otherwise hold the probe open until the orchestrator's own timeout.
            async with asyncio.timeout(READYZ_TIMEOUT_S):
                async with app.state.db_engine.connect() as connection:
                    await connection.execute(text("select 1"))
        except Exception as exc:
            # The exception itself is logged, never returned — the client gets the
            # request id and the log line has the rest.
            logger.warning("readyz.database_unreachable", error=str(exc))
            return error_response(
                request,
                status_code=503,
                code="database_unavailable",
                message="Database is not reachable.",
            )

        return JSONResponse(status_code=200, content={"status": "ok", "database": "ok"})

    return app


app = create_app()
