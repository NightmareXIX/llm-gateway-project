"""``app/db/repo/requests.py`` against a real Postgres.

``requests`` is append-only and almost entirely nullable, and both of those are
decisions rather than accidents — so both are asserted here.

**NULL means "never got that far".** A request that died before a slot resolved
has no provider and no model. Writing ``"unknown"`` into those columns would
poison the one query this table exists to answer — "how often does Groq fail" —
by inventing a provider that never served anything. The minimal-insert test is
what stops a well-meaning default from being added later.

**The Phase 2/3 columns carry their server defaults.** ``substituted``,
``attempts`` and ``wasted_tokens_out`` are not parameters yet. They are in the
schema now because a migration against a live free-tier Postgres is worse than
three unused columns, and they are checked here so that when Phase 2 starts
writing them it is changing a value rather than discovering the column was NULL
all along.

The endpoint's use of this repo is covered in
[test_chat_endpoint.py](tests/integration/test_chat_endpoint.py); the cascade
behaviour is covered in
[test_repo_messages.py](tests/integration/test_repo_messages.py). Neither is
repeated here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repo import requests as repo

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------------- #
async def test_a_request_that_died_early_records_nulls_not_placeholders(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """The failure shape. ``provider = 'unknown'`` would show up in every
    per-provider aggregate as a provider."""
    user = await user_factory()

    row = await repo.create(db_session, user_id=user.id, status=repo.STATUS_ERROR)

    assert row.status == "error"
    assert row.provider is None
    assert row.model is None
    assert row.served_slot is None
    assert row.requested_slot is None
    assert row.tokens_in is None
    assert row.tokens_out is None
    assert row.latency_ms is None
    assert row.conversation_id is None
    assert row.api_key_id is None


async def test_the_row_knows_its_own_id_before_the_caller_gets_it(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """The ``flush``. Without it the caller logs an intention rather than a row,
    and ``request_row_id`` in the log line is ``None`` exactly when someone is
    trying to find the row."""
    user = await user_factory()

    row = await repo.create(db_session, user_id=user.id, status=repo.STATUS_OK)

    assert row.id is not None
    assert row.created_at is not None


async def test_the_phase_two_columns_carry_their_defaults(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """Parameters since Step 5, but still defaulted — a caller with nothing to say
    about failover writes the same row Phase 1 wrote."""
    user = await user_factory()

    row = await repo.create(db_session, user_id=user.id, status=repo.STATUS_OK)

    assert row.substituted is False
    assert row.attempts == []
    assert row.wasted_tokens_out == 0
    assert row.cache_hit is False


async def test_the_attempt_trail_survives_the_round_trip(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """JSONB, so the list has to come back as a list of dicts rather than a string.

    The trail is deliberately longer than the three-attempt cap: a candidate
    skipped on an open breaker is an event without a round trip (ADR-015), so this
    column and ``messages.meta.attempts`` legitimately disagree.
    """
    user = await user_factory()
    trail = [
        {"n": 1, "provider": "groq", "model": "big", "outcome": "skipped_breaker"},
        {"n": 2, "provider": "groq", "model": "small", "outcome": "error", "latency_ms": 412},
        {"n": 3, "provider": "groq", "model": "other", "outcome": "ok", "latency_ms": 890},
    ]

    written = await repo.create(
        db_session,
        user_id=user.id,
        status=repo.STATUS_OK,
        substituted=True,
        attempts=trail,
        wasted_tokens_out=128,
    )
    await db_session.refresh(written)

    assert written.substituted is True
    assert written.wasted_tokens_out == 128
    assert written.attempts == trail
    assert written.attempts[1]["latency_ms"] == 412


async def test_a_fully_populated_row_survives_the_round_trip(
    db_session: AsyncSession,
    user_factory: Callable[..., Any],
    conversation_factory: Callable[..., Any],
    api_key_factory: Callable[..., Any],
) -> None:
    user = await user_factory()
    conversation = await conversation_factory(user=user)
    _, api_key = await api_key_factory(user=user)

    written = await repo.create(
        db_session,
        user_id=user.id,
        api_key_id=api_key.id,
        conversation_id=conversation.id,
        requested_slot="auto",
        served_slot="general",
        provider="groq",
        model="llama-3.3-70b-versatile",
        tokens_in=812,
        tokens_out=340,
        latency_ms=1234,
        status=repo.STATUS_OK,
        cache_hit=True,
    )
    # Re-read from Postgres rather than trusting the in-session object: the point
    # is that every value made it through the column types, not that Python kept
    # hold of what it was handed.
    await db_session.refresh(written)

    rows = await repo.list_for_user(db_session, user_id=user.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.id == written.id
    assert row.api_key_id == api_key.id
    assert row.conversation_id == conversation.id
    assert row.requested_slot == "auto"
    assert row.served_slot == "general"
    assert row.provider == "groq"
    assert row.model == "llama-3.3-70b-versatile"
    assert row.tokens_in == 812
    assert row.tokens_out == 340
    assert row.latency_ms == 1234
    assert row.cache_hit is True


async def test_a_browser_session_is_recorded_without_a_key(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """§1.2: ``user_id`` is who pays for it, ``api_key_id`` is which credential
    made it — and a session has none."""
    user = await user_factory()

    row = await repo.create(db_session, user_id=user.id, status=repo.STATUS_OK, api_key_id=None)

    assert row.user_id == user.id
    assert row.api_key_id is None


async def test_an_error_code_is_recorded_alongside_the_status(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """A bare "it failed" and a failure with ``rate_limited`` on it are different
    rows to whoever is asking why the error rate moved."""
    user = await user_factory()

    row = await repo.create(
        db_session,
        user_id=user.id,
        status=repo.STATUS_ERROR,
        provider="groq",
        model="llama-3.3-70b-versatile",
        error_code="rate_limited",
        latency_ms=42,
    )

    assert row.status == "error"
    assert row.error_code == "rate_limited"
    # Known before it failed, so recorded — unlike the early-death case above.
    assert row.provider == "groq"


async def test_the_status_column_is_open_to_later_values(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """No CHECK constraint on purpose: Phases 2 and 3 add ``degraded`` and
    ``cached``, and a constraint here would mean a migration each time."""
    user = await user_factory()

    row = await repo.create(db_session, user_id=user.id, status="degraded")

    assert row.status == "degraded"


# --------------------------------------------------------------------------- #
# list_for_user
# --------------------------------------------------------------------------- #
async def test_the_listing_is_newest_first(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """Ordered to match ``ix_requests_user_id_created_at``, and tie-broken by id so
    rows written inside one transaction — which share ``now()`` — still come back
    in a stable order rather than whatever the planner felt like."""
    user = await user_factory()
    written = [
        await repo.create(db_session, user_id=user.id, status=repo.STATUS_OK, model=str(index))
        for index in range(5)
    ]

    rows = await repo.list_for_user(db_session, user_id=user.id)

    assert len(rows) == 5
    ids = [row.id for row in rows]
    assert ids == sorted((row.id for row in written), reverse=True)


async def test_the_listing_is_scoped_to_one_user(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    mine = await user_factory()
    theirs = await user_factory()
    await repo.create(db_session, user_id=mine.id, status=repo.STATUS_OK, model="mine")
    await repo.create(db_session, user_id=theirs.id, status=repo.STATUS_OK, model="theirs")

    rows = await repo.list_for_user(db_session, user_id=mine.id)

    assert [row.model for row in rows] == ["mine"]


async def test_the_listing_honours_its_limit(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    for _ in range(10):
        await repo.create(db_session, user_id=user.id, status=repo.STATUS_OK)

    assert len(await repo.list_for_user(db_session, user_id=user.id, limit=3)) == 3


async def test_a_user_with_no_history_lists_empty(db_session: AsyncSession) -> None:
    """Empty, not an error — a brand new account has made no calls yet."""
    assert await repo.list_for_user(db_session, user_id=uuid4()) == []
