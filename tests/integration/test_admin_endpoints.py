"""``/v1/admin/*`` end to end (Phase 7 Step 3, D44).

Every route is self-scoped, so the thing worth proving about each one is that
it never reads past ``principal.user_id`` — a second seeded user's rows or
counters must never leak into the first user's response. ``/quota`` has no
``requests`` row to scope: it delegates straight to ``list_models``, so its
own test proves that delegation rather than re-deriving the computation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import keys
from app.core.crypto import encrypt_provider_key
from app.db.models import Request
from app.db.repo import provider_keys as provider_keys_repo
from app.db.repo import requests as requests_repo
from app.providers.registry import build_registry
from tests import provider_fixtures
from tests.conftest import TokenFactory

pytestmark = pytest.mark.integration

USAGE = "/v1/admin/usage"
QUOTA = "/v1/admin/quota"
REQUESTS = "/v1/admin/requests"


def _headers(make_jwt: TokenFactory, **kwargs: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_jwt(**kwargs)}"}


@pytest.fixture
async def no_upstream(app: FastAPI) -> AsyncIterator[provider_fixtures.RecordingHandler]:
    """``/quota`` must answer from local state alone, the same claim
    ``test_models_endpoint.py`` makes about ``/v1/models`` — it delegates to
    the very same function, so a real upstream call here would mean that
    delegation broke."""
    handler = provider_fixtures.RecordingHandler(provider_fixtures.load("groq", "success"))
    client = handler.client()
    app.state.provider_registry = build_registry(client=client)
    try:
        yield handler
    finally:
        await client.aclose()


async def _at(session: AsyncSession, row: Request, when: Any) -> Request:
    """Back-date one seeded row so ordering assertions mean something.

    ``created_at`` is ``server_default=func.now()``, which inside a
    transaction is the *transaction's* start time (``test_repo_requests.py``
    makes the same point) — every row a test writes in one ``db_session``
    would otherwise share one timestamp and fall back to an ``id.desc()`` tie
    break with no relation to insertion order."""
    await session.execute(update(Request).where(Request.id == row.id).values(created_at=when))
    return row


async def _add_private_key(
    db_session: AsyncSession, *, user_id: UUID, provider: str, plaintext: str
) -> None:
    await provider_keys_repo.upsert(
        db_session,
        user_id=user_id,
        provider=provider,
        encrypted_key=encrypt_provider_key(plaintext),
        last_4=plaintext[-4:],
        nickname=None,
        validation_status="valid",
        last_validated_at=None,
    )


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
async def test_usage_requires_authentication(client: Any) -> None:
    response = await client.get(USAGE)
    assert response.status_code == 401


async def test_quota_requires_authentication(client: Any, no_upstream: Any) -> None:
    response = await client.get(QUOTA)
    assert response.status_code == 401
    assert no_upstream.requests == []


async def test_requests_requires_authentication(client: Any) -> None:
    response = await client.get(REQUESTS)
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# GET /v1/admin/usage
# --------------------------------------------------------------------------- #
async def test_window_query_param_validates(client: Any, make_jwt: TokenFactory) -> None:
    response = await client.get(USAGE, params={"window": "banana"}, headers=_headers(make_jwt))
    assert response.status_code == 422


async def test_usage_is_scoped_to_the_caller(
    client: Any,
    make_jwt: TokenFactory,
    db_session: AsyncSession,
    user_factory: Callable[..., Any],
) -> None:
    mine = await user_factory()
    someone_else = await user_factory()

    await requests_repo.create(db_session, user_id=mine.id, status=requests_repo.STATUS_OK)
    await requests_repo.create(db_session, user_id=mine.id, status=requests_repo.STATUS_ERROR)
    for _ in range(5):
        await requests_repo.create(
            db_session, user_id=someone_else.id, status=requests_repo.STATUS_OK
        )

    response = await client.get(
        USAGE,
        params={"window": "1h"},
        headers=_headers(make_jwt, sub=mine.id, email=mine.email),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["outcomes"]["total"] == 2
    assert body["outcomes"]["ok"] == 1
    assert body["outcomes"]["errors"] == 1
    assert sum(point["total"] for point in body["volume"]) == 2


async def test_an_unpriced_model_surfaces_separately_from_the_total(
    client: Any,
    make_jwt: TokenFactory,
    db_session: AsyncSession,
    user_factory: Callable[..., Any],
) -> None:
    """D46/trap 7: a model with no ``pricing.yaml`` entry contributes to
    ``unpriced_requests``, never to ``total_cost`` as a silent zero."""
    user = await user_factory()

    # Priced: groq/openai/gpt-oss-20b costs $0.10/$0.50 per Mtok (config/pricing.yaml).
    await requests_repo.create(
        db_session,
        user_id=user.id,
        status=requests_repo.STATUS_OK,
        provider="groq",
        model="openai/gpt-oss-20b",
        tokens_in=1_000_000,
        tokens_out=1_000_000,
    )
    # Unpriced: no entry for this (provider, model) anywhere in the table.
    await requests_repo.create(
        db_session,
        user_id=user.id,
        status=requests_repo.STATUS_OK,
        provider="mystery",
        model="ghost-1",
        tokens_in=500,
        tokens_out=500,
    )

    response = await client.get(
        USAGE,
        params={"window": "1h"},
        headers=_headers(make_jwt, sub=user.id, email=user.email),
    )

    assert response.status_code == 200
    body = response.json()

    assert body["unpriced_requests"] == 1
    assert Decimal(body["total_cost"]) == Decimal("0.10") + Decimal("0.50")
    assert body["currency"] == "usd"

    slices = {(s["provider"], s["model"]): s for s in body["providers"]}
    assert slices[("groq", "openai/gpt-oss-20b")]["simulated_cost"] is not None
    assert slices[("mystery", "ghost-1")]["simulated_cost"] is None

    # Both requests were spent under the shared pool (the `quota_scope`
    # default); the private side has zero tokens, which is a real "spent
    # nothing" fact, not a pricing gap, so it costs exactly zero rather than
    # `None` — the same distinction `simulated_cost` itself draws.
    assert body["pool_split"]["shared_cost"] is not None
    assert Decimal(body["pool_split"]["private_cost"]) == Decimal("0")


async def test_pool_split_cost_reflects_each_sides_own_tokens(
    client: Any,
    make_jwt: TokenFactory,
    db_session: AsyncSession,
    user_factory: Callable[..., Any],
) -> None:
    """The blended rate (``PoolSplitOut``'s docstring) is exact when every
    priced request in the window uses only one of input/output tokens at one
    price — the degenerate case that isolates the arithmetic from the
    approximation. groq/openai/gpt-oss-20b prices input at $0.10/Mtok
    (config/pricing.yaml); using only ``tokens_in`` keeps the blended rate
    equal to that one number regardless of how it is split across pools."""
    user = await user_factory()

    await requests_repo.create(
        db_session,
        user_id=user.id,
        status=requests_repo.STATUS_OK,
        provider="groq",
        model="openai/gpt-oss-20b",
        tokens_in=2_000_000,
        tokens_out=0,
        quota_scope="system",
    )
    await requests_repo.create(
        db_session,
        user_id=user.id,
        status=requests_repo.STATUS_OK,
        provider="groq",
        model="openai/gpt-oss-20b",
        tokens_in=1_000_000,
        tokens_out=0,
        quota_scope=str(uuid4()),
    )

    response = await client.get(
        USAGE,
        params={"window": "1h"},
        headers=_headers(make_jwt, sub=user.id, email=user.email),
    )

    assert response.status_code == 200
    body = response.json()

    assert Decimal(body["total_cost"]) == Decimal("0.30")
    pool = body["pool_split"]
    assert Decimal(pool["shared_cost"]) == Decimal("0.20")
    assert Decimal(pool["private_cost"]) == Decimal("0.10")


async def test_a_window_with_nothing_priced_reports_no_cost_at_all(
    client: Any,
    make_jwt: TokenFactory,
    db_session: AsyncSession,
    user_factory: Callable[..., Any],
) -> None:
    user = await user_factory()
    await requests_repo.create(
        db_session,
        user_id=user.id,
        status=requests_repo.STATUS_OK,
        provider="mystery",
        model="ghost-1",
        tokens_in=10,
        tokens_out=10,
    )

    response = await client.get(
        USAGE,
        params={"window": "1h"},
        headers=_headers(make_jwt, sub=user.id, email=user.email),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total_cost"] is None
    assert body["currency"] is None
    assert body["unpriced_requests"] == 1
    assert body["pool_split"]["shared_cost"] is None
    assert body["pool_split"]["private_cost"] is None


# --------------------------------------------------------------------------- #
# GET /v1/admin/requests
# --------------------------------------------------------------------------- #
async def test_requests_is_scoped_to_the_caller_and_ordered(
    client: Any,
    make_jwt: TokenFactory,
    db_session: AsyncSession,
    user_factory: Callable[..., Any],
) -> None:
    mine = await user_factory()
    someone_else = await user_factory()

    now = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)
    first = await _at(
        db_session,
        await requests_repo.create(db_session, user_id=mine.id, status=requests_repo.STATUS_OK),
        now - timedelta(minutes=5),
    )
    second = await _at(
        db_session,
        await requests_repo.create(db_session, user_id=mine.id, status=requests_repo.STATUS_ERROR),
        now,
    )
    await requests_repo.create(db_session, user_id=someone_else.id, status=requests_repo.STATUS_OK)

    response = await client.get(REQUESTS, headers=_headers(make_jwt, sub=mine.id, email=mine.email))

    assert response.status_code == 200
    body = response.json()
    ids = [row["id"] for row in body["data"]]
    assert set(ids) == {str(first.id), str(second.id)}
    assert ids[0] == str(second.id)  # most recent first


async def test_requests_limit_is_bounded(client: Any, make_jwt: TokenFactory) -> None:
    # One login for both calls — `make_jwt`'s default `email` is the same
    # literal every time, so two separate calls would mint two accounts
    # fighting over one email and 409 on the second (a trap CLAUDE.md already
    # names from Phase 6 Step 7's own test-authoring notes).
    headers = _headers(make_jwt)

    response = await client.get(REQUESTS, params={"limit": 0}, headers=headers)
    assert response.status_code == 422

    response = await client.get(REQUESTS, params={"limit": 10_000}, headers=headers)
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# GET /v1/admin/quota
# --------------------------------------------------------------------------- #
def _strip_resets_at(value: Any) -> Any:
    """Drop every ``resets_at`` key before comparing two live snapshots.

    Both routes compute ``resets_at`` off ``SYSTEM_CLOCK.now()`` at the
    instant each request is handled — two real, separate requests a few
    milliseconds apart, so the timestamp itself is expected to differ even
    when the two routes agree on everything that matters. Recurses through
    both the list of slot entries and each one's candidate tuple.
    """
    if isinstance(value, dict):
        return {k: _strip_resets_at(v) for k, v in value.items() if k != "resets_at"}
    if isinstance(value, list):
        return [_strip_resets_at(item) for item in value]
    return value


async def test_quota_matches_v1_models_for_the_same_caller(
    client: Any, make_jwt: TokenFactory, no_upstream: Any
) -> None:
    headers = _headers(make_jwt)

    quota_response = await client.get(QUOTA, headers=headers)
    models_response = await client.get("/v1/models", headers=headers)

    assert quota_response.status_code == 200
    assert models_response.status_code == 200
    assert no_upstream.requests == []
    assert _strip_resets_at(quota_response.json()) == _strip_resets_at(models_response.json())


async def test_a_private_key_holder_sees_their_own_scope_on_the_quota_route(
    client: Any,
    make_jwt: TokenFactory,
    no_upstream: Any,
    db_session: AsyncSession,
    redis_client: FakeRedis,
    user_factory: Callable[..., Any],
) -> None:
    """Mirrors ``test_models_endpoint.py``'s two-account fixture shape (D41):
    exhausting the *shared* pool's window must not affect a private-key
    holder's own view of the same candidate through this route."""
    holder = await user_factory()
    other = await user_factory()
    await _add_private_key(
        db_session, user_id=holder.id, provider="gemini", plaintext="user-owned-gemini-key"
    )
    shared_key = keys.quota(keys.SYSTEM_SCOPE, "gemini", "gemini-3.6-flash", "rpm")
    await redis_client.set(shared_key, 999_999, ex=60)

    holder_response = await client.get(
        QUOTA, headers=_headers(make_jwt, sub=holder.id, email=holder.email)
    )
    other_response = await client.get(
        QUOTA, headers=_headers(make_jwt, sub=other.id, email=other.email)
    )

    assert no_upstream.requests == []
    holder_body = holder_response.json()
    other_body = other_response.json()

    def _gemini_general(body: dict[str, Any]) -> dict[str, Any]:
        general = next(entry for entry in body["data"] if entry["id"] == "general")
        return next(c for c in general["candidates"] if c["provider"] == "gemini")

    assert _gemini_general(holder_body)["status"] == "available"
    assert _gemini_general(other_body)["status"] == "rate_limited"
