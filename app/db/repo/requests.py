"""Reads and writes against ``requests`` — the usage and debugging surface.

One write function, one INSERT. This table is append-only by design: a
``requests`` row records what happened on one inbound call, and something that
happened does not later become something else. There is no ``update`` here and
there should not be.

**Every column is nullable except ``status`` and ``quota_scope``**, which is the
shape a failure needs. A request that died before a slot was resolved has no
provider and no model, and writing ``"unknown"`` into those columns would poison
exactly the query the table exists to serve — "how often does Groq fail".
``NULL`` means "never got that far"; a string means we know. ``quota_scope`` is
the one exception because it names what pays, not what happened — always known,
even for a request that got nowhere.

``substituted``, ``attempts`` and ``wasted_tokens_out`` were added to the schema in
Phase 1 and left off this signature until there was something to put in them.
Phase 2 Step 5 is that moment: the router's attempt trail lands in ``attempts``,
and D2's disclosure lands in ``substituted``. ``wasted_tokens_out`` is still a
parameter nobody passes a non-zero value to — the streaming collector (Step 10) is
its first real writer, and it is here now so that step is a call-site change rather
than a signature change.

``ttft_ms`` and ``quota_scope`` are Phase 3 Step 1's pair, added to this signature
for the same reason: Step 5's router integration and Step 9's streaming collector
are call-site changes rather than signature changes when the time comes.
``quota_scope`` defaults to ``"system"`` because every call site is, until Phase 6
replaces the constant with a resolved value.

Phase 7 Step 2 adds the other half: four aggregate reads for the usage dashboard,
below the two row-level functions and documented as their own section. They read
this table; they do not change what is written to it, and no migration or index
came with them.

Takes a session, never commits. The caller owns the transaction boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import DateTime, and_, func, literal, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import Request

STATUS_OK = "ok"
STATUS_ERROR = "error"
"""The two values Phase 1 writes. The column has no CHECK constraint on purpose —
Phases 2 and 3 add ``degraded`` and ``cached``, and a constraint here would mean a
migration each time."""


async def create(
    session: AsyncSession,
    *,
    user_id: UUID,
    status: str,
    api_key_id: UUID | None = None,
    conversation_id: UUID | None = None,
    requested_slot: str | None = None,
    served_slot: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    latency_ms: int | None = None,
    error_code: str | None = None,
    cache_hit: bool = False,
    substituted: bool = False,
    attempts: list[dict[str, Any]] | None = None,
    wasted_tokens_out: int = 0,
    ttft_ms: int | None = None,
    quota_scope: str = "system",
) -> Request:
    """Record one inbound request.

    ``user_id`` is who pays for it; ``api_key_id`` is which credential made it,
    and is ``None`` for a browser session. Quota keys on the first (§1.2) —
    a user with three API keys is one user with one budget — while the second is
    what answers "which of my integrations is burning the daily cap".

    ``attempts`` is the router's trail, already serialized. It can be longer than
    the three-attempt cap, because a candidate skipped on an open breaker is an
    event without a round trip (ADR-015) — so this column and
    ``messages.meta.attempts`` legitimately disagree, and neither is wrong.
    """
    row = Request(
        user_id=user_id,
        api_key_id=api_key_id,
        conversation_id=conversation_id,
        requested_slot=requested_slot,
        served_slot=served_slot,
        provider=provider,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
        status=status,
        error_code=error_code,
        cache_hit=cache_hit,
        substituted=substituted,
        attempts=attempts if attempts is not None else [],
        wasted_tokens_out=wasted_tokens_out,
        ttft_ms=ttft_ms,
        quota_scope=quota_scope,
    )
    session.add(row)
    # Assigns id and created_at now, so the caller can log the row it just wrote
    # rather than logging an intention.
    await session.flush()
    return row


async def list_for_user(
    session: AsyncSession,
    *,
    user_id: UUID,
    limit: int = 50,
) -> Sequence[Request]:
    """Most recent first, scoped to one user.

    Ordered to match ``ix_requests_user_id_created_at``. Phase 7's usage dashboard
    aggregates rather than lists, so this exists for tests and for the "show me my
    last few calls" view — not as the dashboard's query.
    """
    result = await session.execute(
        select(Request)
        .where(Request.user_id == user_id)
        .order_by(Request.created_at.desc(), Request.id.desc())
        .limit(limit)
    )
    return result.scalars().all()


# --------------------------------------------------------------------------- #
# Aggregate reads — Phase 7 Step 2, D45
# --------------------------------------------------------------------------- #
# Everything below counts rows in Postgres and returns frozen dataclasses.
# Nothing here loads rows into Python to count them: the dashboard's whole point
# is that ``ix_requests_user_id_created_at`` does the work, and a ``GROUP BY``
# that happens in a ``for`` loop stops being free the first time somebody's
# account gets busy.
#
# Costing is deliberately absent. These functions return token counts;
# ``app/usage/pricing.py`` turns them into money one layer up (Step 3). A repo
# module that reads a YAML price table is a repo module with a config
# dependency, and the next person who needs the same counts in a different unit
# has to edit SQL to get them.
#
# Every function is ownership-scoped in the SQL itself (``WHERE user_id = :uid``),
# takes its ``now``/``since`` from the caller rather than calling the clock, and
# returns UTC.


STATUS_REPLAYED = "replayed"
"""D6's idempotent replay, written by Phase 7 Step 6 and read here already.

Trap 18: this is a new value in a deliberately unconstrained column, and these
aggregates are the readers most likely to be wrong about it. A replay is neither
a success nor a failure — it is the *same* answer handed back a second time — so
it is counted on its own axis and excluded from both ``ok`` and ``errors``.
Naming the constant one step before anything writes it is what keeps the error
rate from jumping the day idempotency ships.
"""

_NON_ERROR_STATUSES = (STATUS_OK, STATUS_REPLAYED)
"""What the error rate is measured against. Anything else — ``error``, and any
value a later phase adds — is a failure. Written as the complement so a new
status counts as a failure by default and has to be classified deliberately to
stop being one."""

SHARED_SCOPE = "system"
"""D45's last row. :func:`pool_split` keys on this literal and never on the
caller's own id: a row written before Phase 6 Step 7 carries ``'system'``
because that is what actually paid for it, not because the column was empty."""


@dataclass(frozen=True, slots=True)
class VolumePoint:
    """One bucket of the request-volume series.

    Present even when nothing happened in it (D45, trap 8) — an hour with no
    traffic is a zero bar, and a series that simply omits it draws a chart in
    which a quiet night looks exactly like a busy one.
    """

    bucket_start: datetime
    """UTC, and the *inclusive* left edge: the bucket covers
    ``[bucket_start, bucket_start + width)``. The client renders local time."""

    total: int
    errors: int
    cache_hits: int


@dataclass(frozen=True, slots=True)
class ProviderSlice:
    """One ``(provider, model)`` pair's share of the real upstream calls.

    ``model`` is nullable for the same reason the column is: a request can know
    which provider it was talking to and still die before a model resolved.
    """

    provider: str
    model: str | None
    requests: int
    tokens_in: int
    tokens_out: int


@dataclass(frozen=True, slots=True)
class OutcomeSummary:
    """Every headline outcome over one window.

    ``total`` partitions exactly into ``ok + errors + replays``. ``cache_hits``,
    ``substituted`` and ``multi_attempt`` cut *across* that partition rather than
    extending it — a cache hit is also an ``ok``.
    """

    total: int
    ok: int
    errors: int
    cache_hits: int
    replays: int

    substituted: int
    """D2's disclosure: something other than the requested model answered."""

    multi_attempt: int
    """Rows whose attempt trail holds more than one entry. A different question
    from ``substituted`` and a more forgiving one — a candidate retried on the
    same provider, or skipped on an open breaker, lands here without ever
    changing which model answered."""

    tokens_in: int
    tokens_out: int
    wasted_tokens_out: int


@dataclass(frozen=True, slots=True)
class PoolSplit:
    """Shared free tier versus the caller's own provider account (Phase 6).

    ``requests.quota_scope`` stopped being a constant in Phase 6 Step 7, which is
    what makes this a query rather than a wish.
    """

    shared_requests: int
    shared_tokens_in: int
    shared_tokens_out: int
    private_requests: int
    private_tokens_in: int
    private_tokens_out: int


Window = Literal["1h", "24h", "7d"]

_WINDOWS: dict[Window, tuple[timedelta, int]] = {
    "1h": (timedelta(minutes=1), 60),
    "24h": (timedelta(hours=1), 24),
    "7d": (timedelta(hours=6), 28),
}
"""Bucket width and bucket count per window (D45), so every series renders as
roughly 30 to 60 points whichever window is asked for."""

_INTERVALS: dict[Window, str] = {
    "1h": "interval '1 minute'",
    "24h": "interval '1 hour'",
    "7d": "interval '6 hours'",
}
"""The same widths as SQL literals, for ``generate_series``' step argument and
the join's right edge.

Interpolated into the statement rather than bound, because Postgres cannot infer
a parameter's type inside ``generate_series(timestamptz, timestamptz, ?)``. Safe
by construction: the keys are a closed :data:`Window` and the values are written
here, so no caller-supplied string can reach this dict.
"""


def window_span(window: Window, now: datetime) -> tuple[datetime, datetime]:
    """First and last bucket start for ``window``, floored to the bucket width.

    Exposed rather than private because the API layer needs the same ``since``
    the series starts at when it asks for the other three aggregates — a summary
    computed over a different span than the chart above it is a dashboard that
    contradicts itself.
    """
    width, count = _WINDOWS[window]
    step = int(width.total_seconds())
    last_epoch = int(now.astimezone(UTC).timestamp()) // step * step
    last = datetime.fromtimestamp(last_epoch, tz=UTC)
    return last - width * (count - 1), last


async def volume_series(
    session: AsyncSession,
    *,
    user_id: UUID,
    window: Window,
    now: datetime,
) -> tuple[VolumePoint, ...]:
    """Request volume over time — one point per bucket, no bucket missing.

    The buckets come from ``generate_series`` and the rows are **left-joined**
    onto them (D45). A ``GROUP BY date_trunc`` over the rows alone would be
    shorter and would silently drop every quiet bucket, drawing a smooth line
    straight through an outage.

    Counts every row, failures included: a request that failed is still a
    request somebody made.
    """
    first, last = window_span(window, now)
    width: ColumnElement[Any] = literal_column(_INTERVALS[window])
    buckets = (
        func.generate_series(
            literal(first, DateTime(timezone=True)),
            literal(last, DateTime(timezone=True)),
            width,
        )
        .table_valued("bucket_start")
        # `render_derived` is what emits the `AS buckets(bucket_start)` column
        # list. A bare alias names the relation without naming its column, and
        # Postgres then cannot resolve `buckets.bucket_start` in the join.
        .render_derived(name="buckets")
    )

    result = await session.execute(
        select(
            buckets.c.bucket_start,
            func.count(Request.id).label("total"),
            func.count(Request.id)
            .filter(Request.status.notin_(_NON_ERROR_STATUSES))
            .label("errors"),
            func.count(Request.id).filter(Request.cache_hit).label("cache_hits"),
        )
        .select_from(buckets)
        .outerjoin(
            Request,
            and_(
                Request.user_id == user_id,
                Request.created_at >= buckets.c.bucket_start,
                Request.created_at < buckets.c.bucket_start + width,
            ),
        )
        .group_by(buckets.c.bucket_start)
        .order_by(buckets.c.bucket_start)
    )
    return tuple(
        VolumePoint(
            bucket_start=row.bucket_start.astimezone(UTC),
            total=row.total,
            errors=row.errors,
            cache_hits=row.cache_hits,
        )
        for row in result
    )


async def provider_distribution(
    session: AsyncSession,
    *,
    user_id: UUID,
    since: datetime,
) -> tuple[ProviderSlice, ...]:
    """Which providers actually served this user, and what they cost in tokens.

    Two exclusions, both of which change the answer (traps 5 and 6):

    * ``provider IS NULL`` is dropped. NULL means "never got that far", not a
      provider called "unknown"; bucketing it as one poisons the exact question
      this table exists to answer.
    * ``cache_hit = true`` is dropped. A cache hit's row names the candidate that
      *originally* answered (``usage/logger.py::record_cache_hit``), so counting
      it here reports a provider call that never happened.
    """
    result = await session.execute(
        select(
            Request.provider,
            Request.model,
            func.count(Request.id).label("requests"),
            func.coalesce(func.sum(Request.tokens_in), 0).label("tokens_in"),
            func.coalesce(func.sum(Request.tokens_out), 0).label("tokens_out"),
        )
        .where(
            Request.user_id == user_id,
            Request.created_at >= since,
            Request.provider.is_not(None),
            Request.cache_hit.is_(False),
        )
        .group_by(Request.provider, Request.model)
        .order_by(func.count(Request.id).desc(), Request.provider, Request.model)
    )
    return tuple(
        ProviderSlice(
            provider=row.provider,
            model=row.model,
            requests=row.requests,
            tokens_in=row.tokens_in,
            tokens_out=row.tokens_out,
        )
        for row in result
    )


async def outcome_summary(
    session: AsyncSession,
    *,
    user_id: UUID,
    since: datetime,
) -> OutcomeSummary:
    """Every headline number for one window, in one round trip.

    ``FILTER`` clauses rather than six queries: the predicates differ, the scan
    does not, and a dashboard that opens six connections to render one card is a
    dashboard that falls over on the free tier it is describing.

    ``multi_attempt`` reads ``jsonb_array_length(attempts)`` and never looks
    *inside* an attempt object — which is what makes it safe against rows written
    before Phase 6 Step 5 put ``key_pool`` in the trail (trap 16).
    """
    result = await session.execute(
        select(
            func.count(Request.id).label("total"),
            func.count(Request.id).filter(Request.status == STATUS_OK).label("ok"),
            func.count(Request.id)
            .filter(Request.status.notin_(_NON_ERROR_STATUSES))
            .label("errors"),
            func.count(Request.id).filter(Request.cache_hit).label("cache_hits"),
            func.count(Request.id).filter(Request.status == STATUS_REPLAYED).label("replays"),
            func.count(Request.id).filter(Request.substituted).label("substituted"),
            func.count(Request.id)
            .filter(func.jsonb_array_length(Request.attempts) > 1)
            .label("multi_attempt"),
            func.coalesce(func.sum(Request.tokens_in), 0).label("tokens_in"),
            func.coalesce(func.sum(Request.tokens_out), 0).label("tokens_out"),
            func.coalesce(func.sum(Request.wasted_tokens_out), 0).label("wasted_tokens_out"),
        ).where(Request.user_id == user_id, Request.created_at >= since)
    )
    row = result.one()
    return OutcomeSummary(
        total=row.total,
        ok=row.ok,
        errors=row.errors,
        cache_hits=row.cache_hits,
        replays=row.replays,
        substituted=row.substituted,
        multi_attempt=row.multi_attempt,
        tokens_in=row.tokens_in,
        tokens_out=row.tokens_out,
        wasted_tokens_out=row.wasted_tokens_out,
    )


async def pool_split(
    session: AsyncSession,
    *,
    user_id: UUID,
    since: datetime,
) -> PoolSplit:
    """Shared pool versus the caller's own provider key, by requests and tokens.

    Keyed on ``quota_scope = 'system'`` versus everything else, never on a
    comparison against the caller's own id (D45): a row written before Phase 6
    Step 7 says ``'system'``, and that is the truth about it rather than a
    missing value to work around.
    """
    shared = Request.quota_scope == SHARED_SCOPE
    private = Request.quota_scope != SHARED_SCOPE
    result = await session.execute(
        select(
            func.count(Request.id).filter(shared).label("shared_requests"),
            func.coalesce(func.sum(Request.tokens_in).filter(shared), 0).label("shared_tokens_in"),
            func.coalesce(func.sum(Request.tokens_out).filter(shared), 0).label(
                "shared_tokens_out"
            ),
            func.count(Request.id).filter(private).label("private_requests"),
            func.coalesce(func.sum(Request.tokens_in).filter(private), 0).label(
                "private_tokens_in"
            ),
            func.coalesce(func.sum(Request.tokens_out).filter(private), 0).label(
                "private_tokens_out"
            ),
        ).where(Request.user_id == user_id, Request.created_at >= since)
    )
    row = result.one()
    return PoolSplit(
        shared_requests=row.shared_requests,
        shared_tokens_in=row.shared_tokens_in,
        shared_tokens_out=row.shared_tokens_out,
        private_requests=row.private_requests,
        private_tokens_in=row.private_tokens_in,
        private_tokens_out=row.private_tokens_out,
    )
