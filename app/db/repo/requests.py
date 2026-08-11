"""Writes against ``requests`` — the usage and debugging surface.

One function, one INSERT. This table is append-only by design: a ``requests`` row
records what happened on one inbound call, and something that happened does not
later become something else. There is no ``update`` here and there should not be.

**Every column is nullable except ``status``**, which is the shape a failure needs.
A request that died before a slot was resolved has no provider and no model, and
writing ``"unknown"`` into those columns would poison exactly the query the table
exists to serve — "how often does Groq fail". ``NULL`` means "never got that far";
a string means we know.

The Phase 2/3 columns (``substituted``, ``attempts``, ``wasted_tokens_out``) are
deliberately not parameters yet. They carry their server defaults, and adding them
to this signature is what Phase 2 does when it has something to put in them —
which is cheaper than carrying arguments every current caller passes as a constant.

Takes a session, never commits. The caller owns the transaction boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
) -> Request:
    """Record one inbound request.

    ``user_id`` is who pays for it; ``api_key_id`` is which credential made it,
    and is ``None`` for a browser session. Quota keys on the first (§1.2) —
    a user with three API keys is one user with one budget — while the second is
    what answers "which of my integrations is burning the daily cap".
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
