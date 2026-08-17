"""D20's sliding-window limiter, against ``fakeredis`` and a frozen clock.

The integration suite (``tests/integration/test_rate_limit.py``) proves the
endpoint answers 429 in the standard envelope; this proves the arithmetic
underneath it — which is where the interesting failures are. A limiter that
cliff-resets at the window boundary, or that never lets a hammering client back
in, passes an endpoint test and is still wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fakeredis.aioredis import FakeRedis

from app.auth.principal import Principal
from app.cache import keys
from app.config import GatewayLimits
from app.core.clock import FixedClock
from app.core.errors import TooManyRequests
from app.deps import RateLimiter

# A boundary-aligned instant: 00:00:00 UTC, so the current minute bucket starts
# exactly here and `elapsed_fraction` is 0.0 until the clock is advanced.
AT_BOUNDARY = datetime(2026, 8, 17, 0, 0, 0, tzinfo=UTC)

FREE = GatewayLimits(rpm=3, rpd=10)
TIERS = {"free": FREE, "plus": GatewayLimits(rpm=60, rpd=5000)}


def _principal(*, user_id: object | None = None, tier: str = "free") -> Principal:
    return Principal(
        user_id=user_id or uuid4(),  # type: ignore[arg-type]
        auth_method="session",
        api_key_id=None,
        tier=tier,
    )


def _limiter(redis: FakeRedis, clock: FixedClock) -> RateLimiter:
    return RateLimiter(redis, TIERS, clock=clock)


async def test_requests_under_the_limit_are_allowed(redis_client: FakeRedis) -> None:
    limiter = _limiter(redis_client, FixedClock(AT_BOUNDARY))
    principal = _principal()

    for _ in range(FREE.rpm):
        await limiter.enforce(principal)


async def test_the_request_over_the_limit_raises_with_a_retry_after(
    redis_client: FakeRedis,
) -> None:
    limiter = _limiter(redis_client, FixedClock(AT_BOUNDARY))
    principal = _principal()

    for _ in range(FREE.rpm):
        await limiter.enforce(principal)

    with pytest.raises(TooManyRequests) as caught:
        await limiter.enforce(principal)

    error = caught.value
    assert error.status_code == 429
    assert error.code == "rate_limited"
    retry_after = error.headers["Retry-After"]
    assert int(retry_after) >= 1
    assert error.details["retry_after_s"] == int(retry_after)


async def test_a_rejected_request_is_refunded(redis_client: FakeRedis) -> None:
    """Otherwise a hammering client inflates its own window past the point where
    waiting can help — a lockout rather than a limit."""
    clock = FixedClock(AT_BOUNDARY)
    limiter = _limiter(redis_client, clock)
    principal = _principal()

    for _ in range(FREE.rpm):
        await limiter.enforce(principal)
    for _ in range(5):
        with pytest.raises(TooManyRequests):
            await limiter.enforce(principal)

    counter = keys.rate_limit(str(principal.user_id), "rpm", int(AT_BOUNDARY.timestamp()))
    assert await redis_client.get(counter) == str(FREE.rpm)


async def test_the_window_slides_rather_than_cliff_resetting(redis_client: FakeRedis) -> None:
    """The point of the two-bucket interpolation. A fixed window would admit a
    full fresh allowance the instant the boundary passes; this one lets the
    previous bucket age out gradually, so the very start of the next minute is
    still blocked."""
    clock = FixedClock(AT_BOUNDARY)
    limiter = _limiter(redis_client, clock)
    principal = _principal()

    for _ in range(FREE.rpm):
        await limiter.enforce(principal)

    # One second into the *next* bucket: the previous one still counts for 59/60
    # of its total, so 3 * 0.983 + 1 = 3.95 > 3 and this is still a refusal.
    clock.set(AT_BOUNDARY)
    clock.advance(61)
    with pytest.raises(TooManyRequests):
        await limiter.enforce(principal)

    # Two thirds of the way through it, the previous bucket has decayed to 1.0,
    # which leaves room again.
    clock.advance(39)
    await limiter.enforce(principal)


async def test_retry_after_is_long_enough_to_actually_succeed(
    redis_client: FakeRedis,
) -> None:
    """The number in the header is a promise. Waiting exactly that long and
    trying again must not be refused a second time."""
    clock = FixedClock(AT_BOUNDARY)
    limiter = _limiter(redis_client, clock)
    principal = _principal()

    for _ in range(FREE.rpm):
        await limiter.enforce(principal)
    with pytest.raises(TooManyRequests) as caught:
        await limiter.enforce(principal)

    clock.advance(caught.value.details["retry_after_s"])
    await limiter.enforce(principal)


async def test_two_credentials_of_one_user_share_one_budget(
    redis_client: FakeRedis,
) -> None:
    """ADR-007's rule, keyed on ``user_id`` and never ``api_key_id``: a user with
    three integrations is one user with one budget."""
    limiter = _limiter(redis_client, FixedClock(AT_BOUNDARY))
    user_id = uuid4()
    session = _principal(user_id=user_id)
    integration = Principal(
        user_id=user_id,
        auth_method="api_key",
        api_key_id=uuid4(),
        tier="free",
    )

    await limiter.enforce(session)
    await limiter.enforce(integration)
    await limiter.enforce(session)

    with pytest.raises(TooManyRequests):
        await limiter.enforce(integration)


async def test_two_users_do_not_share_a_budget(redis_client: FakeRedis) -> None:
    limiter = _limiter(redis_client, FixedClock(AT_BOUNDARY))
    one, two = _principal(), _principal()

    for _ in range(FREE.rpm):
        await limiter.enforce(one)
    with pytest.raises(TooManyRequests):
        await limiter.enforce(one)

    await limiter.enforce(two)


async def test_the_daily_window_is_enforced_too(redis_client: FakeRedis) -> None:
    """``rpd`` is the wider of the two, so reaching it takes moving the clock
    past enough minute buckets that ``rpm`` never binds."""
    clock = FixedClock(AT_BOUNDARY)
    limiter = _limiter(redis_client, clock)
    principal = _principal()

    for _ in range(FREE.rpd):
        await limiter.enforce(principal)
        clock.advance(60)

    with pytest.raises(TooManyRequests) as caught:
        await limiter.enforce(principal)
    assert caught.value.details["retry_after_s"] > 60


async def test_the_tier_decides_the_limit(redis_client: FakeRedis) -> None:
    limiter = _limiter(redis_client, FixedClock(AT_BOUNDARY))
    plus = _principal(tier="plus")

    for _ in range(FREE.rpm + 5):
        await limiter.enforce(plus)


async def test_an_unknown_tier_is_let_through(redis_client: FakeRedis) -> None:
    """A tier the YAML does not describe is our config gap, not the caller's
    fault — and fail-open is this limiter's rule (Contract C)."""
    limiter = _limiter(redis_client, FixedClock(AT_BOUNDARY))
    exotic = _principal(tier="enterprise")

    for _ in range(FREE.rpm + 5):
        await limiter.enforce(exotic)


class _BrokenRedis:
    """Every command raises. What an unreachable Upstash looks like from here."""

    def pipeline(self, transaction: bool = True) -> object:
        raise ConnectionError("redis is down")


async def test_redis_down_fails_open() -> None:
    """The opposite of quota's rule (D15), deliberately. This limit protects our
    own capacity, not a credential that can be banned."""
    limiter = RateLimiter(_BrokenRedis(), TIERS, clock=FixedClock(AT_BOUNDARY))  # type: ignore[arg-type]
    principal = _principal()

    for _ in range(FREE.rpm + 5):
        await limiter.enforce(principal)


async def test_the_counter_expires_after_two_windows(redis_client: FakeRedis) -> None:
    """The previous bucket has to outlive its own window for the interpolation to
    have anything to read — that is what ``RATE_LIMIT_TTL_MULTIPLIER`` is for."""
    limiter = _limiter(redis_client, FixedClock(AT_BOUNDARY))
    principal = _principal()
    await limiter.enforce(principal)

    ttl = await redis_client.ttl(
        keys.rate_limit(str(principal.user_id), "rpm", int(AT_BOUNDARY.timestamp()))
    )
    assert ttl == 60 * keys.RATE_LIMIT_TTL_MULTIPLIER
