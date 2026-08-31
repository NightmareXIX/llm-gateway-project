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

**The aggregates are asserted against seeded rows, not against a live app.**
Phase 7 Step 2's four dashboard reads all count in SQL, and every way of getting
them wrong is a semantic one — an empty bucket that is omitted rather than zero,
a cache hit counted as a provider call, a NULL provider bucketed as a provider,
a replay counted as a failure. Each of those is one test below, because none of
them would fail loudly anywhere else: the query still runs, the number is just
wrong in a flattering direction.

The endpoint's use of this repo is covered in
[test_chat_endpoint.py](tests/integration/test_chat_endpoint.py); the cascade
behaviour is covered in
[test_repo_messages.py](tests/integration/test_repo_messages.py). Neither is
repeated here.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Request
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


# --------------------------------------------------------------------------- #
# Aggregates — Phase 7 Step 2, D45
# --------------------------------------------------------------------------- #
# Every test below seeds rows through `repo.create` and then back-dates
# `created_at` with one UPDATE. The column is `server_default=func.now()`, which
# inside a transaction is the *transaction's* start time — so every row a test
# writes would otherwise share one timestamp and no time-bucketing assertion
# would mean anything. The back-dating is a test-only concern: `create` stays
# append-only and gains no `created_at` parameter, because production never wants
# to claim a request happened at a time it did not.

NOW = datetime(2026, 8, 31, 12, 30, 0, tzinfo=UTC)
"""A fixed instant, passed in rather than read off a clock — which is exactly why
the aggregates take `now`/`since` as arguments (the codebase's clock discipline)."""


async def _at(session: AsyncSession, row: Any, when: datetime) -> Any:
    """Back-date one seeded row. See the section comment above."""
    await session.execute(update(Request).where(Request.id == row.id).values(created_at=when))
    return row


# --------------------------------------------------------------------------- #
# window_span
# --------------------------------------------------------------------------- #
def test_the_span_is_floored_to_the_bucket_width() -> None:
    """The series has to land on stable edges: two calls a few seconds apart must
    describe the same buckets, or the chart jitters under every poll."""
    first, last = repo.window_span("24h", NOW)

    assert last == datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    assert first == datetime(2026, 8, 30, 13, 0, tzinfo=UTC)
    assert (last - first) == timedelta(hours=23)


@pytest.mark.parametrize(
    ("window", "count", "width"),
    [
        ("1h", 60, timedelta(minutes=1)),
        ("24h", 24, timedelta(hours=1)),
        ("7d", 28, timedelta(hours=6)),
    ],
)
def test_every_window_renders_between_thirty_and_sixty_points(
    window: Any, count: int, width: timedelta
) -> None:
    """D45's sizing rule. A window that produced 10 000 points would be a chart
    nobody can read and a payload nobody wants to send."""
    first, last = repo.window_span(window, NOW)

    assert (last - first) / width + 1 == count


# --------------------------------------------------------------------------- #
# volume_series
# --------------------------------------------------------------------------- #
async def test_a_bucket_with_no_traffic_is_present_and_zero(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """D45/trap 8, the reason `generate_series` is left-joined rather than
    `GROUP BY date_trunc`. A quiet hour that is simply absent from the series
    draws a smooth line straight through an outage."""
    user = await user_factory()
    # Two hours of traffic with a deliberate gap between them.
    await _at(
        db_session,
        await repo.create(db_session, user_id=user.id, status=repo.STATUS_OK),
        NOW - timedelta(hours=3, minutes=10),
    )
    await _at(
        db_session,
        await repo.create(db_session, user_id=user.id, status=repo.STATUS_OK),
        NOW - timedelta(hours=1, minutes=10),
    )

    series = await repo.volume_series(db_session, user_id=user.id, window="24h", now=NOW)

    assert len(series) == 24
    by_bucket = {point.bucket_start: point for point in series}
    assert by_bucket[datetime(2026, 8, 31, 9, tzinfo=UTC)].total == 1
    assert by_bucket[datetime(2026, 8, 31, 11, tzinfo=UTC)].total == 1
    # The gap in between: present, and zero rather than missing.
    empty = by_bucket[datetime(2026, 8, 31, 10, tzinfo=UTC)]
    assert empty.total == 0
    assert empty.errors == 0
    assert empty.cache_hits == 0


async def test_the_series_is_ordered_oldest_first_and_covers_the_whole_window(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()

    series = await repo.volume_series(db_session, user_id=user.id, window="1h", now=NOW)

    assert len(series) == 60
    starts = [point.bucket_start for point in series]
    assert starts == sorted(starts)
    assert starts[-1] == datetime(2026, 8, 31, 12, 30, tzinfo=UTC)
    assert starts[0] == datetime(2026, 8, 31, 11, 31, tzinfo=UTC)
    assert all(point.total == 0 for point in series)


async def test_a_failure_with_no_provider_still_counts_as_a_request(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """Volume counts every row, ``status`` included — a request that failed is
    still a request somebody made. The same row's absence from the *provider*
    distribution is asserted below."""
    user = await user_factory()
    await _at(
        db_session,
        await repo.create(db_session, user_id=user.id, status=repo.STATUS_ERROR),
        NOW - timedelta(minutes=10),
    )

    series = await repo.volume_series(db_session, user_id=user.id, window="24h", now=NOW)
    bucket = next(p for p in series if p.bucket_start == datetime(2026, 8, 31, 12, tzinfo=UTC))

    assert bucket.total == 1
    assert bucket.errors == 1


async def test_a_cache_hit_counts_in_volume_and_in_cache_hits(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    await _at(
        db_session,
        await repo.create(
            db_session,
            user_id=user.id,
            status=repo.STATUS_OK,
            provider="groq",
            model="llama-3.3-70b-versatile",
            cache_hit=True,
        ),
        NOW - timedelta(minutes=10),
    )

    series = await repo.volume_series(db_session, user_id=user.id, window="24h", now=NOW)
    bucket = next(p for p in series if p.bucket_start == datetime(2026, 8, 31, 12, tzinfo=UTC))

    assert (bucket.total, bucket.errors, bucket.cache_hits) == (1, 0, 1)


async def test_a_replay_is_neither_a_success_nor_an_error(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """Trap 18. ``replayed`` is a status Step 6 introduces into an unconstrained
    column, and the naive ``status <> 'ok'`` error predicate would silently turn
    every successful idempotent retry into a reported failure."""
    user = await user_factory()
    await _at(
        db_session,
        await repo.create(db_session, user_id=user.id, status=repo.STATUS_REPLAYED),
        NOW - timedelta(minutes=10),
    )

    series = await repo.volume_series(db_session, user_id=user.id, window="24h", now=NOW)
    bucket = next(p for p in series if p.bucket_start == datetime(2026, 8, 31, 12, tzinfo=UTC))

    assert bucket.total == 1
    assert bucket.errors == 0


async def test_a_row_older_than_the_window_is_outside_the_series(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    await _at(
        db_session,
        await repo.create(db_session, user_id=user.id, status=repo.STATUS_OK),
        NOW - timedelta(days=3),
    )

    series = await repo.volume_series(db_session, user_id=user.id, window="24h", now=NOW)

    assert sum(point.total for point in series) == 0


async def test_the_series_is_scoped_to_one_user(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """D44: every dashboard read is ownership-scoped in the SQL, exactly like
    conversations and files."""
    mine = await user_factory()
    theirs = await user_factory()
    for owner in (mine, theirs):
        await _at(
            db_session,
            await repo.create(db_session, user_id=owner.id, status=repo.STATUS_OK),
            NOW - timedelta(minutes=10),
        )

    series = await repo.volume_series(db_session, user_id=mine.id, window="24h", now=NOW)

    assert sum(point.total for point in series) == 1


# --------------------------------------------------------------------------- #
# provider_distribution
# --------------------------------------------------------------------------- #
async def test_the_distribution_groups_by_provider_and_model(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    since = NOW - timedelta(hours=24)
    for _ in range(2):
        await _at(
            db_session,
            await repo.create(
                db_session,
                user_id=user.id,
                status=repo.STATUS_OK,
                provider="groq",
                model="llama-3.3-70b-versatile",
                tokens_in=100,
                tokens_out=20,
            ),
            NOW - timedelta(minutes=10),
        )
    await _at(
        db_session,
        await repo.create(
            db_session,
            user_id=user.id,
            status=repo.STATUS_OK,
            provider="gemini",
            model="gemini-3.5-flash",
            tokens_in=7,
            tokens_out=3,
        ),
        NOW - timedelta(minutes=10),
    )

    slices = await repo.provider_distribution(db_session, user_id=user.id, since=since)

    # Busiest first, so the panel's top row is the provider doing the work.
    assert [(s.provider, s.requests) for s in slices] == [("groq", 2), ("gemini", 1)]
    assert slices[0].model == "llama-3.3-70b-versatile"
    assert (slices[0].tokens_in, slices[0].tokens_out) == (200, 40)
    assert (slices[1].tokens_in, slices[1].tokens_out) == (7, 3)


async def test_a_null_provider_is_not_a_provider_called_unknown(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """Trap 6. NULL means "never got that far"; bucketing it as a provider
    poisons the one query this table exists to answer."""
    user = await user_factory()
    since = NOW - timedelta(hours=24)
    await _at(
        db_session,
        await repo.create(db_session, user_id=user.id, status=repo.STATUS_ERROR),
        NOW - timedelta(minutes=10),
    )

    assert await repo.provider_distribution(db_session, user_id=user.id, since=since) == ()


async def test_a_cache_hit_is_not_a_provider_call(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """Trap 5. ``record_cache_hit`` writes the provider that *originally*
    answered, so counting the row here reports a call that never went out."""
    user = await user_factory()
    since = NOW - timedelta(hours=24)
    await _at(
        db_session,
        await repo.create(
            db_session,
            user_id=user.id,
            status=repo.STATUS_OK,
            provider="groq",
            model="llama-3.3-70b-versatile",
            cache_hit=True,
        ),
        NOW - timedelta(minutes=10),
    )
    await _at(
        db_session,
        await repo.create(
            db_session,
            user_id=user.id,
            status=repo.STATUS_OK,
            provider="groq",
            model="llama-3.3-70b-versatile",
            tokens_in=10,
            tokens_out=5,
        ),
        NOW - timedelta(minutes=10),
    )

    slices = await repo.provider_distribution(db_session, user_id=user.id, since=since)

    assert len(slices) == 1
    assert slices[0].requests == 1
    assert (slices[0].tokens_in, slices[0].tokens_out) == (10, 5)


async def test_a_provider_known_before_the_model_was_keeps_a_null_model(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """The column is nullable and so is the slice: the same "never got that far"
    rule, one level down."""
    user = await user_factory()
    since = NOW - timedelta(hours=24)
    await _at(
        db_session,
        await repo.create(db_session, user_id=user.id, status=repo.STATUS_ERROR, provider="groq"),
        NOW - timedelta(minutes=10),
    )

    slices = await repo.provider_distribution(db_session, user_id=user.id, since=since)

    assert len(slices) == 1
    assert slices[0].provider == "groq"
    assert slices[0].model is None
    assert (slices[0].tokens_in, slices[0].tokens_out) == (0, 0)


async def test_the_distribution_is_scoped_to_one_user_and_the_window(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    mine = await user_factory()
    theirs = await user_factory()
    since = NOW - timedelta(hours=24)
    await _at(
        db_session,
        await repo.create(
            db_session, user_id=theirs.id, status=repo.STATUS_OK, provider="groq", model="m"
        ),
        NOW - timedelta(minutes=10),
    )
    await _at(
        db_session,
        await repo.create(
            db_session, user_id=mine.id, status=repo.STATUS_OK, provider="groq", model="m"
        ),
        NOW - timedelta(days=2),
    )

    assert await repo.provider_distribution(db_session, user_id=mine.id, since=since) == ()


# --------------------------------------------------------------------------- #
# outcome_summary
# --------------------------------------------------------------------------- #
async def test_the_summary_partitions_every_row_exactly_once(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """``total == ok + errors + replays``, with ``cache_hits`` cutting across the
    partition rather than extending it."""
    user = await user_factory()
    since = NOW - timedelta(hours=24)
    when = NOW - timedelta(minutes=10)
    await _at(
        db_session,
        await repo.create(db_session, user_id=user.id, status=repo.STATUS_OK, tokens_in=10),
        when,
    )
    await _at(
        db_session,
        await repo.create(
            db_session, user_id=user.id, status=repo.STATUS_OK, cache_hit=True, tokens_in=0
        ),
        when,
    )
    await _at(
        db_session,
        await repo.create(
            db_session, user_id=user.id, status=repo.STATUS_ERROR, error_code="rate_limited"
        ),
        when,
    )
    await _at(
        db_session,
        await repo.create(db_session, user_id=user.id, status=repo.STATUS_REPLAYED),
        when,
    )

    summary = await repo.outcome_summary(db_session, user_id=user.id, since=since)

    assert summary.total == 4
    assert summary.ok == 2
    assert summary.errors == 1
    assert summary.replays == 1
    assert summary.cache_hits == 1
    assert summary.total == summary.ok + summary.errors + summary.replays


async def test_a_replay_is_counted_on_its_own_axis_and_nowhere_else(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """Trap 18 again, stated as its own assertion so a future ``status <> 'ok'``
    refactor fails here rather than on a dashboard."""
    user = await user_factory()
    since = NOW - timedelta(hours=24)
    await _at(
        db_session,
        await repo.create(db_session, user_id=user.id, status=repo.STATUS_REPLAYED),
        NOW - timedelta(minutes=10),
    )

    summary = await repo.outcome_summary(db_session, user_id=user.id, since=since)

    assert (summary.total, summary.replays) == (1, 1)
    assert (summary.ok, summary.errors, summary.cache_hits) == (0, 0, 0)
    assert (summary.substituted, summary.multi_attempt) == (0, 0)


async def test_the_summary_sums_tokens_including_the_wasted_ones(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """D1's discarded partial was really generated, so it is really spent."""
    user = await user_factory()
    since = NOW - timedelta(hours=24)
    await _at(
        db_session,
        await repo.create(
            db_session,
            user_id=user.id,
            status=repo.STATUS_OK,
            tokens_in=100,
            tokens_out=50,
            wasted_tokens_out=17,
        ),
        NOW - timedelta(minutes=10),
    )

    summary = await repo.outcome_summary(db_session, user_id=user.id, since=since)

    assert (summary.tokens_in, summary.tokens_out, summary.wasted_tokens_out) == (100, 50, 17)


async def test_an_empty_window_sums_to_zero_rather_than_none(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """``sum()`` over no rows is NULL in SQL. Without the ``coalesce`` the
    dashboard renders ``None`` tokens for every brand new account."""
    user = await user_factory()

    summary = await repo.outcome_summary(
        db_session, user_id=user.id, since=NOW - timedelta(hours=24)
    )

    assert summary == repo.OutcomeSummary(0, 0, 0, 0, 0, 0, 0, 0, 0, 0)


async def test_substituted_and_multi_attempt_are_different_questions(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """A candidate retried on the same provider took two attempts without ever
    changing which model answered; a substitution can happen on the first."""
    user = await user_factory()
    since = NOW - timedelta(hours=24)
    when = NOW - timedelta(minutes=10)
    await _at(
        db_session,
        await repo.create(
            db_session,
            user_id=user.id,
            status=repo.STATUS_OK,
            attempts=[
                {"n": 1, "provider": "groq", "outcome": "error"},
                {"n": 2, "provider": "groq", "outcome": "ok"},
            ],
        ),
        when,
    )
    await _at(
        db_session,
        await repo.create(
            db_session,
            user_id=user.id,
            status=repo.STATUS_OK,
            substituted=True,
            attempts=[{"n": 1, "provider": "gemini", "outcome": "ok"}],
        ),
        when,
    )

    summary = await repo.outcome_summary(db_session, user_id=user.id, since=since)

    assert summary.multi_attempt == 1
    assert summary.substituted == 1


async def test_multi_attempt_tolerates_a_pre_phase_six_attempt_trail(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """Trap 16. ``key_pool`` only exists on trails written since Phase 6 Step 5,
    and this count reads ``jsonb_array_length`` rather than looking inside an
    attempt — the same "tolerate a missing key rather than backfill one" rule
    ``MessageMeta.from_jsonb`` follows."""
    user = await user_factory()
    since = NOW - timedelta(hours=24)
    await _at(
        db_session,
        await repo.create(
            db_session,
            user_id=user.id,
            status=repo.STATUS_OK,
            attempts=[
                {"n": 1, "provider": "groq", "model": "a", "outcome": "error"},
                {"n": 2, "provider": "gemini", "model": "b", "outcome": "ok"},
            ],
        ),
        NOW - timedelta(minutes=10),
    )

    summary = await repo.outcome_summary(db_session, user_id=user.id, since=since)

    assert summary.multi_attempt == 1


async def test_an_empty_attempt_trail_is_not_a_multi_attempt(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """The column's server default is ``'[]'::jsonb``, so this is the shape every
    Phase 1 row has — ``jsonb_array_length`` must cope with it rather than the
    query erroring on the oldest rows in the table."""
    user = await user_factory()
    since = NOW - timedelta(hours=24)
    await _at(
        db_session,
        await repo.create(db_session, user_id=user.id, status=repo.STATUS_OK),
        NOW - timedelta(minutes=10),
    )

    summary = await repo.outcome_summary(db_session, user_id=user.id, since=since)

    assert summary.multi_attempt == 0


async def test_the_summary_is_scoped_to_one_user(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    mine = await user_factory()
    theirs = await user_factory()
    since = NOW - timedelta(hours=24)
    for owner in (theirs, theirs, mine):
        await _at(
            db_session,
            await repo.create(db_session, user_id=owner.id, status=repo.STATUS_OK),
            NOW - timedelta(minutes=10),
        )

    summary = await repo.outcome_summary(db_session, user_id=mine.id, since=since)

    assert summary.total == 1


# --------------------------------------------------------------------------- #
# pool_split
# --------------------------------------------------------------------------- #
async def test_the_split_puts_a_system_scoped_row_on_the_shared_side(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """D45's last row: a row written before Phase 6 Step 7 carries ``'system'``
    because the shared pool really did pay for it. Never compared against the
    caller's own id."""
    user = await user_factory()
    since = NOW - timedelta(hours=24)
    await _at(
        db_session,
        await repo.create(
            db_session, user_id=user.id, status=repo.STATUS_OK, tokens_in=10, tokens_out=4
        ),
        NOW - timedelta(minutes=10),
    )

    split = await repo.pool_split(db_session, user_id=user.id, since=since)

    assert (split.shared_requests, split.shared_tokens_in, split.shared_tokens_out) == (1, 10, 4)
    assert (split.private_requests, split.private_tokens_in, split.private_tokens_out) == (0, 0, 0)


async def test_a_byok_row_lands_on_the_private_side(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    since = NOW - timedelta(hours=24)
    when = NOW - timedelta(minutes=10)
    await _at(
        db_session,
        await repo.create(
            db_session,
            user_id=user.id,
            status=repo.STATUS_OK,
            tokens_in=100,
            tokens_out=40,
            quota_scope=str(user.id),
        ),
        when,
    )
    await _at(
        db_session,
        await repo.create(
            db_session, user_id=user.id, status=repo.STATUS_OK, tokens_in=1, tokens_out=2
        ),
        when,
    )

    split = await repo.pool_split(db_session, user_id=user.id, since=since)

    assert (split.private_requests, split.private_tokens_in, split.private_tokens_out) == (
        1,
        100,
        40,
    )
    assert (split.shared_requests, split.shared_tokens_in, split.shared_tokens_out) == (1, 1, 2)


async def test_an_empty_split_is_zeroes_rather_than_nones(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()

    split = await repo.pool_split(db_session, user_id=user.id, since=NOW - timedelta(hours=24))

    assert split == repo.PoolSplit(0, 0, 0, 0, 0, 0)


async def test_the_split_is_scoped_to_one_user(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """A second user's *private* rows are the most dangerous thing this function
    could leak, since ``quota_scope`` literally carries their id."""
    mine = await user_factory()
    theirs = await user_factory()
    since = NOW - timedelta(hours=24)
    await _at(
        db_session,
        await repo.create(
            db_session,
            user_id=theirs.id,
            status=repo.STATUS_OK,
            tokens_in=999,
            quota_scope=str(theirs.id),
        ),
        NOW - timedelta(minutes=10),
    )

    split = await repo.pool_split(db_session, user_id=mine.id, since=since)

    assert split == repo.PoolSplit(0, 0, 0, 0, 0, 0)
