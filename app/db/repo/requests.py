"""Writes against ``requests`` — the usage and debugging surface.

One function, one INSERT. This table is append-only by design: a ``requests`` row
records what happened on one inbound call, and something that happened does not
later become something else. There is no ``update`` here and there should not be.

**Every column is nullable except ``status``**, which is the shape a failure needs.
A request that died before a slot was resolved has no provider and no model, and
writing ``"unknown"`` into those columns would poison exactly the query the table
exists to serve — "how often does Groq fail". ``NULL`` means "never got that far";
a string means we know.

``substituted``, ``attempts`` and ``wasted_tokens_out`` were added to the schema in
Phase 1 and left off this signature until there was something to put in them.
Phase 2 Step 5 is that moment: the router's attempt trail lands in ``attempts``,
and D2's disclosure lands in ``substituted``. ``wasted_tokens_out`` is still a
parameter nobody passes a non-zero value to — the streaming collector (Step 10) is
its first real writer, and it is here now so that step is a call-site change rather
than a signature change.

Takes a session, never commits. The caller owns the transaction boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
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
    substituted: bool = False,
    attempts: list[dict[str, Any]] | None = None,
    wasted_tokens_out: int = 0,
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
