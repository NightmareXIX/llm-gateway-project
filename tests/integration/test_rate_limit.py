"""D20 — the gateway's own limit on the gateway's own users, end to end.

The arithmetic is tested in ``tests/unit/test_rate_limiter.py``; what is proved
here is the part only the real endpoint can prove: that going over answers 429 in
the *standard envelope* with a usable ``Retry-After``, that the two credentials a
user may hold share one budget rather than two, that reads are untouched, and
that a Redis outage lets traffic through instead of stopping the product.

The limits are overridden to something small. Driving the committed
``free: {rpm: 20}`` to its edge would mean twenty-one full turns through the
router for one assertion, and the number in the YAML is business configuration
rather than behaviour under test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import GatewayLimits, ProvidersConfig, get_providers_config
from app.core.clock import FixedClock
from app.deps import RateLimiter, get_rate_limiter
from app.providers.registry import build_registry
from tests import provider_fixtures
from tests.conftest import TokenFactory, assert_envelope

pytestmark = pytest.mark.integration

COMPLETIONS = "/v1/chat/completions"

TIGHT = {"free": GatewayLimits(rpm=2, rpd=100)}
"""Two per minute, so the third request in a frozen minute is the one over."""


def _groq_only() -> ProvidersConfig:
    """The same pin the other chat suites use: an exact call count must not
    depend on whether an unrelated provider is enabled."""
    config = get_providers_config()
    providers = {
        name: entry if name == "groq" else entry.model_copy(update={"enabled": False})
        for name, entry in config.providers.items()
    }
    return config.model_copy(update={"providers": providers})


@pytest.fixture
async def groq(app: FastAPI) -> AsyncIterator[provider_fixtures.RecordingHandler]:
    handler = provider_fixtures.RecordingHandler(provider_fixtures.load("groq", "success"))
    client = handler.client()
    app.state.provider_registry = build_registry(client=client, config=_groq_only())
    try:
        yield handler
    finally:
        await client.aclose()


@pytest.fixture
def tight_limits(app: FastAPI, redis_client: Any, frozen_clock: FixedClock) -> None:
    """Swap the configured tier limits for ``TIGHT``, on a clock that does not
    move — so every request in a test lands in the same minute bucket."""
    app.dependency_overrides[get_rate_limiter] = lambda: RateLimiter(
        redis_client, TIGHT, clock=frozen_clock
    )


def _headers(make_jwt: TokenFactory, **kwargs: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_jwt(**kwargs)}"}


def _body() -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": "what is a gateway?"}]}


async def test_the_tier_limit_is_enforced_in_the_standard_envelope(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    tight_limits: None,
) -> None:
    headers = _headers(make_jwt)

    for _ in range(TIGHT["free"].rpm):
        allowed = await client.post(COMPLETIONS, json=_body(), headers=headers)
        assert allowed.status_code == 200

    refused = await client.post(COMPLETIONS, json=_body(), headers=headers)

    assert refused.status_code == 429
    error = assert_envelope(refused.json())
    assert error["code"] == "rate_limited"
    assert int(refused.headers["Retry-After"]) >= 1
    # The refusal happened before any routing: the provider saw only the two
    # requests that were actually allowed.
    assert len(groq.requests) == TIGHT["free"].rpm


async def test_two_api_keys_of_one_user_share_one_budget(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    user_factory: Any,
    api_key_factory: Any,
    groq: provider_fixtures.RecordingHandler,
    tight_limits: None,
) -> None:
    """ADR-007's rule, and the reason quota keys on ``user_id``: a user with two
    integrations is one user with one budget. ``api_key_id`` is attribution, not
    a wallet."""
    user = await user_factory()
    first_key, _ = await api_key_factory(user=user)
    second_key, _ = await api_key_factory(user=user)

    one = await client.post(COMPLETIONS, json=_body(), headers={"X-API-Key": first_key})
    two = await client.post(COMPLETIONS, json=_body(), headers={"X-API-Key": second_key})
    three = await client.post(COMPLETIONS, json=_body(), headers={"X-API-Key": first_key})

    assert one.status_code == 200
    assert two.status_code == 200
    assert three.status_code == 429

    # The same user's session token is the same budget too — the credential is
    # not what is being counted.
    session = await client.post(COMPLETIONS, json=_body(), headers=_headers(make_jwt, sub=user.id))
    assert session.status_code == 429


async def test_two_users_do_not_share_a_budget(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    tight_limits: None,
) -> None:
    busy = _headers(make_jwt, email="busy@example.com")
    quiet = _headers(make_jwt, email="quiet@example.com")

    for _ in range(TIGHT["free"].rpm):
        assert (await client.post(COMPLETIONS, json=_body(), headers=busy)).status_code == 200
    assert (await client.post(COMPLETIONS, json=_body(), headers=busy)).status_code == 429

    assert (await client.post(COMPLETIONS, json=_body(), headers=quiet)).status_code == 200


async def test_the_window_slides_rather_than_cliff_resetting(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    frozen_clock: FixedClock,
    groq: provider_fixtures.RecordingHandler,
    tight_limits: None,
) -> None:
    """A fixed window would hand back a full allowance the instant the minute
    rolled over. This one lets the previous bucket age out, so a caller who
    spent everything is still refused one second into the next minute."""
    headers = _headers(make_jwt)
    for _ in range(TIGHT["free"].rpm):
        assert (await client.post(COMPLETIONS, json=_body(), headers=headers)).status_code == 200

    refused = await client.post(COMPLETIONS, json=_body(), headers=headers)
    assert refused.status_code == 429

    frozen_clock.advance(60)
    just_over_the_boundary = await client.post(COMPLETIONS, json=_body(), headers=headers)
    assert just_over_the_boundary.status_code == 429

    # Waiting out the header's own promise does work, which is what makes it a
    # header worth obeying.
    frozen_clock.advance(int(refused.headers["Retry-After"]))
    assert (await client.post(COMPLETIONS, json=_body(), headers=headers)).status_code == 200


async def test_reads_are_not_rate_limited(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    tight_limits: None,
) -> None:
    """Applied to the chat endpoint only (D20). Rate-limiting the conversation
    list makes the UI feel broken and protects nothing — generation is what
    spends a free tier."""
    headers = _headers(make_jwt)
    for _ in range(TIGHT["free"].rpm + 1):
        await client.post(COMPLETIONS, json=_body(), headers=headers)

    listed = await client.get("/v1/conversations", headers=headers)
    assert listed.status_code == 200


async def test_redis_down_fails_open(
    app: FastAPI,
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    frozen_clock: FixedClock,
    groq: provider_fixtures.RecordingHandler,
) -> None:
    """Contract C's other half. Quota fails *closed* because a banned provider
    key never comes back; our own limit fails **open**, because refusing traffic
    for want of a counter trades a real outage for a hypothetical one."""

    class _BrokenRedis:
        def pipeline(self, transaction: bool = True) -> object:
            raise ConnectionError("redis is down")

    app.dependency_overrides[get_rate_limiter] = lambda: RateLimiter(
        _BrokenRedis(),  # type: ignore[arg-type]
        TIGHT,
        clock=frozen_clock,
    )
    headers = _headers(make_jwt)

    for _ in range(TIGHT["free"].rpm + 2):
        assert (await client.post(COMPLETIONS, json=_body(), headers=headers)).status_code == 200


async def test_the_switch_turns_it_off_entirely(
    app: FastAPI,
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    db_session: AsyncSession,
) -> None:
    """``RATE_LIMIT_ENABLED=false`` means the limiter is never constructed, which
    the endpoint sees as ``None`` and skips — the same shape as
    ``QUOTA_ENFORCEMENT`` and ``CACHE_EXACT_ENABLED``."""
    app.dependency_overrides[get_rate_limiter] = lambda: None
    headers = _headers(make_jwt)

    for _ in range(TIGHT["free"].rpm + 2):
        assert (await client.post(COMPLETIONS, json=_body(), headers=headers)).status_code == 200
