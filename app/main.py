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
from app.api.v1 import files as files_routes
from app.api.v1 import models as models_routes
from app.auth.jwt import JwksCache
from app.cache.client import LuaScriptRegistry, create_redis_client, probe, redacted_target
from app.config import Settings, get_settings, validate_startup_config
from app.core.errors import error_response, register_exception_handlers
from app.core.logging import (
    RequestContextMiddleware,
    configure_logging,
    get_logger,
)
from app.db.session import create_db_engine, create_session_factory
from app.perception.storage import build_store
from app.providers.registry import build_registry
from app.schemas.errors import DEFAULT_ERROR_RESPONSES, ErrorResponse
from app.usage.metrics import LatencyTable

logger = get_logger("app.main")

READYZ_TIMEOUT_S = 5.0
"""Upper bound on the readiness check's database round-trip."""

READYZ_REDIS_TIMEOUT_S = 1.0
"""Upper bound on the readiness check's Redis round-trip — separate, and shorter.

Two reasons it is not folded into the block above. It cannot *fail* the probe
(ADR-010), so spending the database's budget on it would let a fail-open
dependency cause a 503 by starvation alone. And a `PING` is the cheapest command
Redis has: one second is already generous, and anything slower is a server that
is not answering rather than one that is busy."""

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

    # Phase 4 Step 2 (D23): the object store behind `POST /v1/files`. Built
    # eagerly like the registry above it — `SupabaseStore` makes no network call
    # at construction, so there is nothing to fail loudly on here, but which
    # backend is live is a boot-time decision and should not be reconsidered
    # per request.
    app.state.object_store = build_store(client=http_client, settings=settings)

    # Lazy like the engine: from_url opens no socket, so an Upstash blip cannot
    # stop the process booting. Nothing reads it until Step 3's circuit breaker —
    # it arrives a phase early (the plan says Phase 3) because breaker state has
    # to be shared across instances, and a per-process breaker would let each
    # one rediscover the same dead provider separately.
    redis = create_redis_client(settings)
    app.state.redis = redis

    scripts = LuaScriptRegistry(redis)
    scripts.load_dir()
    await scripts.warm()  # never fatal; Redis is fail-open (ADR-010)
    app.state.lua_scripts = scripts

    # Per-process on purpose (ADR-014). It starts empty, so a freshly-booted
    # instance routes in config order until each candidate has five successful
    # samples — which is what makes the first request after a deploy predictable
    # rather than arbitrary.
    app.state.latency = LatencyTable()

    logger.info(
        "startup.complete",
        require_verified_email=settings.REQUIRE_VERIFIED_EMAIL,
        jwks_url=settings.supabase_jwks_url,
        # Redacted deliberately: REDIS_URL carries a password in every
        # deployment that is not a laptop, and this line goes to log storage.
        redis=redacted_target(settings.REDIS_URL),
        slots=registry.describe(),
        # D11's kill switch. Printed at boot because "the router reordered my
        # candidates" and "the router did not" are the same log line otherwise.
        latency_ranking=settings.ROUTING_LATENCY_RANKING,
        # Phase 3 Step 1's four switches — printed for the same reason: each one
        # changes request behaviour invisibly unless its state is in the first
        # line of the startup log.
        quota_enforcement=settings.QUOTA_ENFORCEMENT,
        quota_headroom_fraction=settings.QUOTA_HEADROOM_FRACTION,
        cache_exact_enabled=settings.CACHE_EXACT_ENABLED,
        rate_limit_enabled=settings.RATE_LIMIT_ENABLED,
        # Phase 4 Step 1's file/perception switches — same reasoning as the four
        # above: each one changes upload or extraction behaviour invisibly
        # unless its state is in the first line of the startup log.
        files_storage_backend=settings.FILES_STORAGE_BACKEND,
        file_max_bytes=settings.FILE_MAX_BYTES,
        perception_enabled=settings.PERCEPTION_ENABLED,
        perception_local_only=settings.PERCEPTION_LOCAL_ONLY,
        perception_local_ocr_enabled=settings.PERCEPTION_LOCAL_OCR_ENABLED,
    )
    try:
        yield
    finally:
        # Mirror-image teardown, in reverse order of acquisition.
        await redis.aclose()
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
    app.include_router(files_routes.router)
    app.include_router(models_routes.router)

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

        **Postgres always decides. Redis decides too, but only while quota
        enforcement means the gateway actually depends on it** (D15,
        [ADR-018](../docs/decisions/ADR-018-quota-fails-closed.md)) — the general
        rule ADR-010 states still holds, it just has a second dependency read
        against it now: *a readiness probe fails only on dependencies whose
        absence makes the instance unable to serve.* With
        ``QUOTA_ENFORCEMENT=True``, `quota.tracker.QuotaTracker.reserve` fails
        every candidate closed the instant Redis is unreachable (D15), so an
        instance in that state will refuse every chat request while reporting
        itself healthy unless this probe says otherwise. With enforcement off, the
        tracker is never constructed and the old ADR-010 verdict applies exactly
        as written — Redis is reported, not decided by.

        The breaker's fail-*open* reasoning (ADR-010) is untouched: a missing
        breaker costs one predictable wasted round trip, which is not what this
        branch is about.
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

        # Outside the block above, on its own shorter bound: this check cannot
        # turn the probe red on its own, so it must not be able to spend the
        # budget of the one that always can.
        redis_ok = await probe(app.state.redis, timeout_s=READYZ_REDIS_TIMEOUT_S)

        if not redis_ok and get_settings().QUOTA_ENFORCEMENT:
            # D15: quota fails closed, so an instance that cannot reach Redis
            # cannot serve a chat request either — leaving it in rotation would
            # convert a Redis outage into a fleet of instances that all answer
            # 502 while reporting themselves ready.
            logger.warning("readyz.redis_unreachable_quota_enforced")
            return error_response(
                request,
                status_code=503,
                code="redis_unavailable",
                message="Redis is not reachable and quota enforcement requires it.",
            )

        return JSONResponse(
            status_code=200,
            content={
                "status": "ok",
                "database": "ok",
                "redis": "ok" if redis_ok else "unavailable",
            },
        )

    return app


app = create_app()
