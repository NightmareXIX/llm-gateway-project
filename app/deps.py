"""Shared FastAPI dependencies.

Resources are created once in the lifespan and read from ``request.app.state``
here. Nothing in this module constructs an engine, a client, or a pool — that
would give every test its own hidden connection pool.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

import httpx
from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.cache.client import LuaScriptRegistry
from app.config import get_limits_config, get_settings
from app.providers.registry import ProviderRegistry
from app.quota.tracker import QuotaTracker
from app.routing.circuit_breaker import CircuitBreaker
from app.usage.metrics import LatencyTable


def get_engine(request: Request) -> AsyncEngine:
    """The process-wide engine. Used by ``/readyz``; endpoints want a session."""
    engine: AsyncEngine = request.app.state.db_engine
    return engine


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request, closed when the request ends.

    Deliberately does not commit. The endpoint or repository owns the transaction
    boundary; an auto-commit here would turn a half-finished handler into a
    half-written conversation.
    """
    factory: async_sessionmaker[AsyncSession] = request.app.state.db_session_factory
    async with factory() as session:
        yield session


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """The factory itself, for the one caller that must not hold a session.

    D14: a streamed turn's collector opens its own session *after* the
    generation is over, deliberately outside FastAPI's ``yield``-dependency
    lifecycle — a ``StreamingResponse`` body keeps running long after the
    handler that returned it has itself finished, so a request-scoped session
    would already be torn down (or pinned open for the whole generation,
    which a free-tier connection pool cannot afford). ``get_session`` is wrong
    for that one caller; this is what it uses instead.
    """
    factory: async_sessionmaker[AsyncSession] = request.app.state.db_session_factory
    return factory


def get_http_client(request: Request) -> httpx.AsyncClient:
    """The process-wide outbound client.

    Endpoints rarely want this — provider adapters already hold it, bound at
    registry-build time. It is exposed for the odd caller that needs to make a
    one-off outbound call without inventing a second connection pool.
    """
    client: httpx.AsyncClient = request.app.state.http_client
    return client


def get_registry(request: Request) -> ProviderRegistry:
    """The slot table, built once in the lifespan.

    Built rather than looked up per request, so a config problem cannot appear
    mid-traffic: by the time an endpoint can call this, the registry is known
    good.
    """
    registry: ProviderRegistry = request.app.state.provider_registry
    return registry


def get_redis(request: Request) -> Redis:
    """The process-wide Redis client, opened once in the lifespan.

    Handed out rather than depended on: nothing may assume a command will
    succeed. Redis is fail-open here (ADR-010) — the breaker that reads it treats
    an unreachable server as "every candidate allowed", which is a slower correct
    gateway rather than a broken one. Quota, in Phase 3, will fail closed on the
    same client for the opposite reason.
    """
    client: Redis = request.app.state.redis
    return client


def get_breaker(request: Request) -> CircuitBreaker:
    """A circuit breaker over the process-wide Redis client.

    Constructed per request rather than held in the lifespan, and that is not
    laziness: the breaker keeps no state of its own — the ``cb:{provider}:{model}``
    hash *is* the state, shared by every worker and every instance. A singleton
    would suggest there is something to share in-process, which is exactly the
    misunderstanding the Redis-backed design exists to prevent. Constructing one
    is three attribute assignments.
    """
    return CircuitBreaker(get_redis(request))


def get_quota(request: Request) -> QuotaTracker | None:
    """A quota tracker over the process-wide Redis client, or ``None`` when
    enforcement is off.

    Constructed per request for the same reason the breaker is: the tracker holds
    no state — the ``q:{scope}:{provider}:{model}:*`` counters are the state, and
    they are shared by every worker and every instance. Constructing one is four
    attribute assignments.

    ``None`` rather than a no-op tracker, and that is the whole point of
    ``QUOTA_ENFORCEMENT`` (D15). Quota fails *closed*, so a self-inflicted total
    outage — Redis down, every candidate refused — needs an escape hatch that
    does not merely make the tracker permissive but stops it being constructed at
    all. The router takes ``quota: QuotaTracker | None`` and skips reservation
    entirely on ``None``, which is Phase 2's behaviour exactly.
    """
    settings = get_settings()
    if not settings.QUOTA_ENFORCEMENT:
        return None

    scripts: LuaScriptRegistry = request.app.state.lua_scripts
    return QuotaTracker(
        get_redis(request),
        scripts,
        get_limits_config(),
        headroom=settings.QUOTA_HEADROOM_FRACTION,
    )


def get_latency(request: Request) -> LatencyTable:
    """The in-process EWMA latency table `auto` ranks with (ADR-014).

    The mirror image of the breaker: one per *process*, created in the lifespan,
    because its entire value is that it accumulates across requests. It is
    deliberately not in Redis — Contract C is frozen, and a cross-instance latency
    key is a change that needs sign-off rather than a side effect.
    """
    table: LatencyTable = request.app.state.latency
    return table


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SessionFactoryDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
RegistryDep = Annotated[ProviderRegistry, Depends(get_registry)]
HttpClientDep = Annotated[httpx.AsyncClient, Depends(get_http_client)]
RedisDep = Annotated[Redis, Depends(get_redis)]
BreakerDep = Annotated[CircuitBreaker, Depends(get_breaker)]
QuotaDep = Annotated[QuotaTracker | None, Depends(get_quota)]
LatencyDep = Annotated[LatencyTable, Depends(get_latency)]
