"""``GET /v1/models`` end to end (Phase 3 Step 7).

Every status this endpoint reports comes from local state — the breaker's hash
and the quota tracker's counters — so these tests write that state directly
into Redis rather than driving requests through the router to produce it. The
one assertion every test in this module shares is D21's own definition of
done: the mock transport behind the registry records **zero** requests, no
matter what the endpoint reports.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI

from app.cache import keys
from app.core.clock import FixedClock
from app.providers.errors import RateLimited
from app.providers.registry import build_registry
from app.routing.circuit_breaker import CircuitBreaker
from tests import provider_fixtures
from tests.conftest import TokenFactory

pytestmark = pytest.mark.integration

MODELS = "/v1/models"


def _headers(make_jwt: TokenFactory, **kwargs: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_jwt(**kwargs)}"}


def _entry(body: dict[str, Any], slot: str) -> dict[str, Any]:
    return next(entry for entry in body["data"] if entry["id"] == slot)


def _candidate(entry: dict[str, Any], provider: str) -> dict[str, Any]:
    return next(c for c in entry["candidates"] if c["provider"] == provider)


async def _open_breaker(breaker: CircuitBreaker, provider: str, model: str) -> None:
    """Take one candidate's breaker to ``open``, the way the router would —
    one ``RateLimited`` is enough (§2, Contract A)."""
    decision = await breaker.allows(provider, model)
    await breaker.record_failure(
        decision, RateLimited("out of quota", provider=provider, model=model, status_code=429)
    )


@pytest.fixture
async def no_upstream(app: FastAPI) -> AsyncIterator[provider_fixtures.RecordingHandler]:
    """Swap the registry for one that fails loudly if a provider is ever called.

    Every test asserts ``no_upstream.requests == []`` — D21's "makes no upstream
    call, ever", proven rather than assumed.
    """
    handler = provider_fixtures.RecordingHandler(provider_fixtures.load("groq", "success"))
    client = handler.client()
    app.state.provider_registry = build_registry(client=client)
    try:
        yield handler
    finally:
        await client.aclose()


async def test_requires_authentication(client: Any, no_upstream: Any) -> None:
    response = await client.get(MODELS)

    assert response.status_code == 401
    assert no_upstream.requests == []


async def test_a_healthy_fleet_is_all_available(
    client: Any, make_jwt: TokenFactory, no_upstream: Any
) -> None:
    response = await client.get(MODELS, headers=_headers(make_jwt))

    assert response.status_code == 200
    assert no_upstream.requests == []

    body = response.json()
    assert body["object"] == "list"
    assert [entry["id"] for entry in body["data"]] == ["auto", "general", "fast"]

    for entry in body["data"]:
        assert entry["status"] == "available"
        assert entry["resets_at"] is None
        assert entry["candidates"]
        for candidate in entry["candidates"]:
            assert candidate["status"] == "available"
            assert candidate["breaker_state"] == "closed"
            assert candidate["resets_at"] is None

    auto = _entry(body, "auto")
    assert auto["owned_by"] is None
    assert {(c["provider"], c["model"]) for c in auto["candidates"]} == {
        ("groq", "openai/gpt-oss-120b"),
        ("gemini", "gemini-3.6-flash"),
        ("openrouter", "nvidia/nemotron-3-super-120b-a12b:free"),
        ("groq", "openai/gpt-oss-20b"),
        ("gemini", "gemini-3.5-flash-lite"),
        ("openrouter", "openai/gpt-oss-20b:free"),
    }

    general = _entry(body, "general")
    assert general["owned_by"] == "groq"
    assert len(general["candidates"]) == 3

    # Windows are populated wherever the model declares a limit — every
    # candidate here does (config/limits.yaml).
    groq_general = _candidate(general, "groq")
    assert {w["window"] for w in groq_general["windows"]} == {"rpm", "rpd", "tpm", "tpd"}
    assert all(w["remaining"] == w["limit"] for w in groq_general["windows"])


async def test_an_open_breaker_marks_only_its_own_candidate_unavailable(
    client: Any,
    make_jwt: TokenFactory,
    redis_client: FakeRedis,
    frozen_clock: FixedClock,
    no_upstream: Any,
) -> None:
    breaker = CircuitBreaker(redis_client, clock=frozen_clock)
    await _open_breaker(breaker, "groq", "openai/gpt-oss-20b")

    response = await client.get(MODELS, headers=_headers(make_jwt))

    assert response.status_code == 200
    assert no_upstream.requests == []
    body = response.json()

    fast = _entry(body, "fast")
    groq_candidate = _candidate(fast, "groq")
    assert groq_candidate["status"] == "unavailable"
    assert groq_candidate["breaker_state"] == "open"
    assert groq_candidate["resets_at"] is not None
    assert groq_candidate["windows"] == []

    # The other two candidates are untouched, so the slot as a whole is still
    # servable — the router would fail over to one of them (D1/D2).
    assert fast["status"] == "available"
    assert fast["resets_at"] is None
    other_candidates = [c for c in fast["candidates"] if c["provider"] != "groq"]
    assert all(c["status"] == "available" for c in other_candidates)

    # `general` shares no candidates with `fast` and is untouched.
    general = _entry(body, "general")
    assert general["status"] == "available"


async def test_an_exhausted_window_marks_its_candidate_rate_limited(
    client: Any, make_jwt: TokenFactory, redis_client: FakeRedis, no_upstream: Any
) -> None:
    key = keys.quota(keys.SYSTEM_SCOPE, "gemini", "gemini-3.5-flash-lite", "rpm")
    await redis_client.set(key, 999_999, ex=60)

    response = await client.get(MODELS, headers=_headers(make_jwt))

    assert response.status_code == 200
    assert no_upstream.requests == []
    body = response.json()

    fast = _entry(body, "fast")
    gemini_candidate = _candidate(fast, "gemini")
    assert gemini_candidate["status"] == "rate_limited"
    assert gemini_candidate["breaker_state"] == "closed"
    assert gemini_candidate["resets_at"] is not None
    rpm_window = next(w for w in gemini_candidate["windows"] if w["window"] == "rpm")
    assert rpm_window["remaining"] == 0

    # groq and openrouter are unaffected, so the slot is still servable.
    assert fast["status"] == "available"


async def test_every_candidate_blocked_makes_the_slot_unavailable(
    client: Any,
    make_jwt: TokenFactory,
    redis_client: FakeRedis,
    frozen_clock: FixedClock,
    no_upstream: Any,
) -> None:
    breaker = CircuitBreaker(redis_client, clock=frozen_clock)
    for provider, model in [
        ("groq", "openai/gpt-oss-20b"),
        ("gemini", "gemini-3.5-flash-lite"),
        ("openrouter", "openai/gpt-oss-20b:free"),
    ]:
        await _open_breaker(breaker, provider, model)

    response = await client.get(MODELS, headers=_headers(make_jwt))

    assert response.status_code == 200
    assert no_upstream.requests == []
    body = response.json()

    fast = _entry(body, "fast")
    assert fast["status"] == "unavailable"
    assert fast["resets_at"] is not None
    assert {c["status"] for c in fast["candidates"]} == {"unavailable"}

    # `auto`'s fleet still has `general`'s three healthy candidates.
    auto = _entry(body, "auto")
    assert auto["status"] == "available"
    assert auto["resets_at"] is None


async def test_a_dead_redis_reports_unknown_rather_than_available(
    client: Any, make_jwt: TokenFactory, app: FastAPI, no_upstream: Any
) -> None:
    """ADR-017's fail-closed is for a real request. A status *read* has no
    request to refuse — it has nothing to report, and says so rather than
    guessing either `available` or `rate_limited`."""

    class _DeadRedis:
        def pipeline(self, *_args: Any, **_kwargs: Any) -> Any:
            # `remaining()` calls this synchronously, unlike every other command
            # here — a coroutine handed back in its place would raise on first
            # use anyway, but only after leaving an unawaited coroutine behind.
            raise ConnectionError("redis is gone")

        def __getattr__(self, _name: str) -> Any:
            async def boom(*_args: Any, **_kwargs: Any) -> Any:
                raise ConnectionError("redis is gone")

            return boom

    app.state.redis = _DeadRedis()

    response = await client.get(MODELS, headers=_headers(make_jwt))

    assert response.status_code == 200
    assert no_upstream.requests == []
    body = response.json()

    for entry in body["data"]:
        for candidate in entry["candidates"]:
            # The breaker fails open on a dead Redis (ADR-010) so this is never
            # `unavailable`; the quota tracker has nothing to report either way.
            assert candidate["status"] == "unknown"
            assert candidate["windows"] == []
        assert entry["status"] == "unknown"
