"""Shared FastAPI dependencies.

Resources are created once in the lifespan and read from ``request.app.state``
here. Nothing in this module constructs an engine, a client, or a pool — that
would give every test its own hidden connection pool.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from math import ceil
from typing import Annotated, Any, Final
from uuid import UUID

import httpx
from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.auth.principal import Principal
from app.cache import keys
from app.cache.client import LuaScriptRegistry
from app.cache.exact import ExactCache
from app.config import GatewayLimits, get_limits_config, get_settings
from app.core.clock import SYSTEM_CLOCK, Clock
from app.core.errors import TooManyRequests
from app.core.logging import get_logger
from app.keys_resolution.resolver import ProviderCredentials, UserCredentials
from app.memory.render import AttachmentResolver
from app.perception.lane import PerceptionResolver
from app.perception.storage import ObjectStore
from app.providers.registry import ProviderRegistry
from app.quota.tracker import QuotaTracker
from app.quota.windows import sliding_count
from app.routing.circuit_breaker import CircuitBreaker
from app.usage.metrics import LatencyTable

logger = get_logger("app.deps")


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


def get_store(request: Request) -> ObjectStore:
    """The process-wide object store, built once in the lifespan (D23).

    Built rather than looked up per request, mirroring ``get_registry``: which
    backend is live (``FILES_STORAGE_BACKEND``) is a boot-time decision, not
    a per-request one.
    """
    store: ObjectStore = request.app.state.object_store
    return store


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


def get_exact_cache(request: Request) -> ExactCache | None:
    """The exact-match cache over the process-wide Redis client, or ``None`` when
    D5/D19's cache is switched off (``CACHE_EXACT_ENABLED``).

    Constructed per request for the same reason the breaker and the quota
    tracker are: the cache holds no state of its own — ``cache:exact:{sha256}``
    is the state, shared by every worker and every instance. ``None`` rather
    than a no-op cache mirrors ``get_quota``: every read becomes an unconditional
    ``BYPASS`` and every write a no-op, without either call site having to know
    why.
    """
    settings = get_settings()
    if not settings.CACHE_EXACT_ENABLED:
        return None
    return ExactCache(get_redis(request))


def get_resolver(
    store: Annotated[ObjectStore, Depends(get_store)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    registry: Annotated[ProviderRegistry, Depends(get_registry)],
    breaker: Annotated[CircuitBreaker, Depends(get_breaker)],
    quota: Annotated[QuotaTracker | None, Depends(get_quota)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> AttachmentResolver | None:
    """Phase 4's perception lane, or ``None`` when it is switched off.

    **One instance per request, and that is load-bearing rather than tidy**
    (D22, trap 6): the resolver memoizes on ``(file_hash, native_wanted)``, and
    render runs once per candidate — up to three times per turn under failover.
    A process-wide resolver would memoize across *users*; a per-render one
    would re-extract the same document on every spill.

    ``None`` rather than a permissive resolver, mirroring ``get_quota``,
    ``get_exact_cache`` and ``get_rate_limiter``: with ``PERCEPTION_ENABLED``
    off, ``render`` falls back to ``NoAttachments`` and a turn carrying a
    ``file_ref`` fails loudly, instead of quietly answering a question about a
    document nobody read.

    Its six resources arrive as sub-dependencies rather than being read off
    ``request.app.state`` here, which is the one place in this module that
    matters: ``get_session_factory`` is overridden in tests, and a direct call
    would walk straight past the override to a piece of app state a test app
    never sets.

    ``scope`` is left at its default ``keys.SYSTEM_SCOPE``, exactly as it is at
    the answer lane's call sites, so Phase 6 replaces one constant in two lanes
    rather than one.
    """
    settings = get_settings()
    if not settings.PERCEPTION_ENABLED:
        return None
    return PerceptionResolver(
        store=store,
        session_factory=session_factory,
        registry=registry,
        breaker=breaker,
        quota=quota,
        redis=redis,
        settings=settings,
    )


def get_credentials(request: Request, principal: Principal) -> ProviderCredentials:
    """This request's credential-and-scope resolver (D36, D38).

    A **plain factory**, not a FastAPI dependency in its own right — it needs
    the authenticated principal, and ``app.auth.dependency`` imports this
    module for ``SessionDep``/``RateLimiterDep``, so a ``Depends(get_principal)``
    parameter here would be the same import cycle ``get_rate_limiter``'s
    docstring already documents. The caller composes it: ``api/v1/chat.py``'s
    ``CredentialsDep`` wraps this in a small ``Depends``-bound function of its
    own, exactly the way ``RateLimitDep`` composes ``enforce_rate_limit``.

    Always a fresh :class:`~app.keys_resolution.resolver.UserCredentials`,
    never cached across requests: D38's whole point is that removing a key
    takes effect on the very next message, and a resolver held longer than
    one request would go on serving a revoked key until it expired. The
    ``session_factory`` — not a request-scoped session — is what lets it
    survive into the streaming path the same way ``get_resolver`` already
    does.

    Nothing calls this yet (Phase 6 Step 4). Step 5 wires it through the
    answer lane, Step 6 through the perception lane.
    """
    registry = get_registry(request)
    session_factory = get_session_factory(request)
    return UserCredentials(principal.user_id, registry, session_factory)


def get_latency(request: Request) -> LatencyTable:
    """The in-process EWMA latency table `auto` ranks with (ADR-014).

    The mirror image of the breaker: one per *process*, created in the lifespan,
    because its entire value is that it accumulates across requests. It is
    deliberately not in Redis — Contract C is frozen, and a cross-instance latency
    key is a change that needs sign-off rather than a side effect.
    """
    table: LatencyTable = request.app.state.latency
    return table


# --------------------------------------------------------------------------- #
# D20 — the gateway's own per-user rate limit
# --------------------------------------------------------------------------- #
RPM: Final[keys.GatewayWindow] = "rpm"
RPD: Final[keys.GatewayWindow] = "rpd"
RPH: Final[keys.GatewayWindow] = "rph"
"""D43: the BYOK validation endpoint's own window. Not a tier throughput
limit — see :meth:`RateLimiter.enforce_one`."""

RATE_LIMIT_WINDOW_S: Final[Mapping[keys.GatewayWindow, int]] = {RPM: 60, RPD: 86_400, RPH: 3600}
"""How wide each of our own windows is. Unrelated to a provider's reset
semantics — ``quota/windows.py`` owns those, and a provider's day can end at
midnight Pacific. Ours is simply a rolling day, because there is no external
boundary for it to agree with."""


@dataclass(frozen=True, slots=True)
class _Counted:
    """One window, after this request has been counted into it."""

    name: keys.GatewayWindow
    limit: int
    width_s: int
    start: int
    """The epoch second the current bucket opened — the ``window_start`` segment
    of ``rl:{user_id}:{window}:{window_start}``, which is the one key in Contract
    C that carries a boundary at all (D20)."""
    previous: int
    current: int
    count: float
    """The interpolated sliding count, including this request."""


class RateLimiter:
    """D20's two-bucket sliding window over ``rl:{user_id}:{window}:{window_start}``.

    **Sliding, not fixed.** This is the one place Contract C's key schema gives
    us a ``window_start`` segment, so the standard interpolation is available:
    ``previous * (1 - elapsed) + current``, which is
    :func:`~app.quota.windows.sliding_count`. The provider-side counters cannot
    do this for want of a key shape (D16), and the contrast is the point — a
    fixed window admits twice the limit across a boundary, which is tolerable
    against a provider's published ceiling and needless here.

    **Keyed on ``user_id``, never ``api_key_id``** (D7, ADR-007). A user with
    three integrations is one user with one budget; ``api_key_id`` stays on the
    ``requests`` row for attribution and pays for nothing.

    **Fails open**, which is the opposite of the quota tracker's rule and for a
    reason that is not symmetric with it (Contract C). This limit protects our
    own capacity, not a credential that can be revoked; refusing traffic because
    a counter is unreachable trades a real outage for a hypothetical one. Quota
    fails closed because a banned provider key does not come back.

    **A rejected request is refunded.** The counter is incremented first — an
    ``INCR`` is atomic where a read-then-write is a race — and given back when
    the answer turns out to be no. Without the refund a client that keeps
    hammering keeps inflating its own window and can never climb back under the
    limit, which is a lockout rather than a rate limit.
    """

    def __init__(
        self,
        redis: Redis,
        limits: Mapping[str, GatewayLimits],
        *,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._redis = redis
        self._limits = limits
        self._clock = clock

    async def enforce(self, principal: Principal) -> None:
        """Count this request and raise :class:`TooManyRequests` if it is over.

        Returns ``None`` on every other path, including the ones where nothing
        could be decided: an unknown tier and an unreachable Redis both let the
        request through, loudly.
        """
        tier = self._limits.get(principal.tier)
        if tier is None:
            # A tier the YAML does not describe is a config gap, not a caller's
            # fault. Log it and charge them nothing.
            logger.warning("rate_limit.unknown_tier", tier=principal.tier)
            return

        user_id = str(principal.user_id)
        now = self._clock.now().timestamp()
        checks = ((RPM, tier.rpm), (RPD, tier.rpd))

        try:
            counted = await self._count(user_id, checks, now=now)
        except Exception as exc:
            logger.warning("rate_limit.fail_open", error=str(exc), error_type=type(exc).__name__)
            return

        await self._raise_if_over(user_id, counted, now=now, log_fields={"tier": principal.tier})

    async def enforce_one(
        self, user_id: str | UUID, window: keys.GatewayWindow, limit: int
    ) -> None:
        """D43: a fixed limit on one window, for one caller — not a tier's.

        ``enforce`` reads its limits from ``config/limits.yaml``'s per-tier
        ``gateway:`` block, which is throughput policy for our users in
        general. This is an anti-abuse floor on a single endpoint (the BYOK
        validation route), which is neither per-tier nor throughput, so it
        takes its limit as a bare argument instead of consulting
        ``self._limits`` — see ``api/keys.py``'s ``KEY_VALIDATION_LIMIT_PER_HOUR``.

        Shares ``_count``, ``_refund`` and ``_retry_after_s`` with
        :meth:`enforce` rather than duplicating them, factored out for exactly
        this second caller.
        """
        user_id_str = str(user_id)
        now = self._clock.now().timestamp()

        try:
            counted = await self._count(user_id_str, ((window, limit),), now=now)
        except Exception as exc:
            logger.warning("rate_limit.fail_open", error=str(exc), error_type=type(exc).__name__)
            return

        await self._raise_if_over(user_id_str, counted, now=now)

    async def _raise_if_over(
        self,
        user_id: str,
        counted: Sequence[_Counted],
        *,
        now: float,
        log_fields: Mapping[str, Any] | None = None,
    ) -> None:
        for window in counted:
            if window.count <= window.limit:
                continue

            await self._refund(user_id, counted)
            retry_after_s = _retry_after_s(window, now=now)
            logger.warning(
                "rate_limit.exceeded",
                window=window.name,
                limit=window.limit,
                count=round(window.count, 2),
                retry_after_s=retry_after_s,
                **(log_fields or {}),
            )
            raise TooManyRequests(
                f"You have used your {window.name.upper()} allowance of "
                f"{window.limit} requests. Try again in {retry_after_s}s.",
                retry_after_s=retry_after_s,
            )

    async def _count(
        self,
        user_id: str,
        checks: Sequence[tuple[keys.GatewayWindow, int]],
        *,
        now: float,
    ) -> tuple[_Counted, ...]:
        """Increment every window's current bucket and read every previous one.

        One round trip for all of it. The TTL is the window's width times
        :data:`~app.cache.keys.RATE_LIMIT_TTL_MULTIPLIER`, which is what keeps
        the previous bucket readable while the current one fills — a sliding
        window that cannot see the bucket it is interpolating from is a fixed
        window with extra steps.
        """
        starts: list[int] = []
        pipe = self._redis.pipeline(transaction=False)
        for name, _limit in checks:
            width = RATE_LIMIT_WINDOW_S[name]
            start = int(now // width) * width
            starts.append(start)
            current_key = keys.rate_limit(user_id, name, start)
            pipe.incr(current_key)
            pipe.expire(current_key, width * keys.RATE_LIMIT_TTL_MULTIPLIER)
            pipe.get(keys.rate_limit(user_id, name, max(0, start - width)))
        results: list[Any] = await pipe.execute()

        counted: list[_Counted] = []
        for index, (name, limit) in enumerate(checks):
            width = RATE_LIMIT_WINDOW_S[name]
            start = starts[index]
            current = int(results[index * 3])
            previous = int(results[index * 3 + 2] or 0)
            counted.append(
                _Counted(
                    name=name,
                    limit=limit,
                    width_s=width,
                    start=start,
                    previous=previous,
                    current=current,
                    count=sliding_count(previous, current, elapsed_fraction=(now - start) / width),
                )
            )
        return tuple(counted)

    async def _refund(self, user_id: str, counted: Sequence[_Counted]) -> None:
        """Give back every increment this request made. Best effort by design.

        A refund that fails costs the caller one request of their own budget for
        one window — annoying and self-correcting. Raising here would turn a 429
        into a 500, which is strictly worse information.
        """
        try:
            pipe = self._redis.pipeline(transaction=False)
            for window in counted:
                pipe.decr(keys.rate_limit(user_id, window.name, window.start))
            await pipe.execute()
        except Exception as exc:
            logger.warning(
                "rate_limit.refund_failed", error=str(exc), error_type=type(exc).__name__
            )


def _retry_after_s(window: _Counted, *, now: float) -> int:
    """When the interpolated count would next fit under the limit, in seconds.

    Not "when the window resets" — under a sliding window nothing resets, the
    previous bucket simply ages out continuously, so the honest answer is the
    instant the arithmetic first allows another request. Two cases:

    The previous bucket alone can decay enough — solve
    ``previous * (1 - f) + current = limit`` for the elapsed fraction ``f``, and
    that instant is inside the current bucket.

    Or the current bucket is already at or over the limit on its own, in which
    case no amount of decay helps until it *becomes* the previous bucket, and
    the same arithmetic runs again one window later.

    Both solve for the moment a *retry* would fit, not the moment the current
    count would — the retry adds one of its own, and a ``Retry-After`` whose
    expiry produces a second 429 is worse than no header at all.
    """
    if window.previous > 0 and window.current <= window.limit:
        # Still inside this bucket: `current` already includes the rejected
        # request, and the retry will re-add exactly it, so the target is the
        # instant the interpolated total first equals the limit.
        needed = (window.previous + window.current - window.limit) / window.previous
        if needed <= 1.0:
            return _at_least_one(window.start + needed * window.width_s - now)

    # This bucket is over the limit on its own, so nothing helps until it
    # *becomes* the previous bucket and starts decaying. It carries the
    # post-refund count by then, and the retry needs one slot of the limit for
    # itself — hence both subtractions.
    next_start = window.start + window.width_s
    carried = max(0, window.current - 1)
    allowance = window.limit - 1
    if carried > allowance:
        needed = 1.0 - allowance / carried
        return _at_least_one(next_start + needed * window.width_s - now)
    return _at_least_one(next_start - now)


def _at_least_one(seconds: float) -> int:
    """``Retry-After: 0`` invites an immediate retry that would be refused."""
    return max(1, ceil(seconds))


def get_rate_limiter(request: Request) -> RateLimiter | None:
    """D20's limiter over the process-wide Redis client, or ``None`` when
    ``RATE_LIMIT_ENABLED`` is off.

    ``None`` rather than a permissive limiter, mirroring ``get_quota`` and
    ``get_exact_cache``: the switch is off, so the object is not built, and no
    call site has to reason about a component that exists but decides nothing.

    Deliberately *not* a dependency that authenticates: ``app.auth.dependency``
    imports this module for ``SessionDep``, so a ``Depends(get_principal)``
    here would be an import cycle. The endpoint that needs it composes the two
    itself — see ``api/v1/chat.py``'s ``RateLimitDep``.
    """
    settings = get_settings()
    if not settings.RATE_LIMIT_ENABLED:
        return None
    return RateLimiter(get_redis(request), get_limits_config().gateway)


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SessionFactoryDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
RegistryDep = Annotated[ProviderRegistry, Depends(get_registry)]
StoreDep = Annotated[ObjectStore, Depends(get_store)]
ResolverDep = Annotated[AttachmentResolver | None, Depends(get_resolver)]
HttpClientDep = Annotated[httpx.AsyncClient, Depends(get_http_client)]
RedisDep = Annotated[Redis, Depends(get_redis)]
BreakerDep = Annotated[CircuitBreaker, Depends(get_breaker)]
QuotaDep = Annotated[QuotaTracker | None, Depends(get_quota)]
ExactCacheDep = Annotated[ExactCache | None, Depends(get_exact_cache)]
RateLimiterDep = Annotated[RateLimiter | None, Depends(get_rate_limiter)]
LatencyDep = Annotated[LatencyTable, Depends(get_latency)]
