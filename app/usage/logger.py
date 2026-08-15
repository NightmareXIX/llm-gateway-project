"""The one place a ``requests`` row is built, and the one place a turn is logged.

Two functions, one per outcome. The endpoint hands over the objects it already
has — a :class:`Principal`, a :class:`ModelSpec`, a :class:`Usage` or a
:class:`ProviderError` — and never assembles a row itself.

**Why a facade over a one-function repo.** Phase 1 has exactly one caller, so this
looks like a wrapper. Phase 2 has three: the router records an attempt trail, the
streaming collector records a message whose tokens were partly discarded (D1's
``wasted_tokens_out``), and this endpoint records the non-streaming path. Three
call sites each building a ``Request`` by hand is how ``status`` ends up spelled
two ways and how the field Phase 7's dashboard needs turns out to be populated on
two of the three. The repo owns the INSERT; this owns what goes in it.

**The log line is not optional decoration.** ``requests`` answers "what happened
across the fleet today"; the log line answers "what happened to *this* call", with
``request_id`` and ``user_id`` already bound as contextvars. The two are read at
different times by different people, so both are emitted.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal
from app.core.logging import get_logger
from app.db.models import Request
from app.db.repo import requests as requests_repo
from app.providers.errors import ProviderError
from app.providers.types import ModelSpec, Usage
from app.routing.router import AttemptRecord

logger = get_logger("app.usage")


async def record_success(
    session: AsyncSession,
    *,
    principal: Principal,
    spec: ModelSpec,
    requested_slot: str,
    usage: Usage,
    latency_ms: int,
    conversation_id: UUID | None = None,
    cache_hit: bool = False,
    attempts: Sequence[AttemptRecord] = (),
    substituted: bool = False,
) -> Request:
    """Record a turn that produced an answer.

    ``attempts`` is the router's trail — every event, including the candidates it
    skipped and the ones that failed before this one worked. A successful turn
    with a three-entry trail is the most interesting row in the table, and it is
    the only place that story survives: one ``messages`` row per logical message
    means the discarded attempts leave no other trace.
    """
    row = await requests_repo.create(
        session,
        user_id=principal.user_id,
        api_key_id=principal.api_key_id,
        conversation_id=conversation_id,
        requested_slot=requested_slot,
        served_slot=spec.slot,
        provider=spec.provider,
        model=spec.model,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
        latency_ms=latency_ms,
        status=requests_repo.STATUS_OK,
        cache_hit=cache_hit,
        substituted=substituted,
        attempts=[record.to_json() for record in attempts],
    )

    logger.info(
        "chat.completed",
        request_row_id=str(row.id),
        conversation_id=str(conversation_id) if conversation_id else None,
        requested_slot=requested_slot,
        served_slot=spec.slot,
        provider=spec.provider,
        model=spec.model,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
        tokens_estimated=usage.estimated,
        latency_ms=latency_ms,
        cache_hit=cache_hit,
        substituted=substituted,
        # The count of events, which is what a log reader scanning for "did this
        # one fail over?" wants. The trail itself is in the row.
        attempts=len(attempts),
    )
    return row


async def record_failure(
    session: AsyncSession,
    *,
    principal: Principal,
    error: ProviderError,
    requested_slot: str,
    latency_ms: int,
    spec: ModelSpec | None = None,
    conversation_id: UUID | None = None,
    attempts: Sequence[AttemptRecord] = (),
    substituted: bool = False,
) -> Request:
    """Record a turn that ended in a normalized provider failure.

    ``provider`` and ``model`` come off the error rather than off ``spec``: a
    :class:`~app.providers.errors.ProviderError` always names the pair that
    produced it, and in Phase 2 that will not be the pair the request started
    against. ``spec`` is only consulted for the slot, which the error does not
    carry.

    Errors flagged ``alert`` — only :class:`~app.providers.errors.AuthFailed` —
    are logged at *error* level. A dead provider key is an ops problem that does
    not fix itself, and it should not sit at the same severity as the rate limits
    this gateway hits by design every day.
    """
    row = await requests_repo.create(
        session,
        user_id=principal.user_id,
        api_key_id=principal.api_key_id,
        conversation_id=conversation_id,
        requested_slot=requested_slot,
        served_slot=spec.slot if spec is not None else None,
        provider=error.provider,
        model=error.model,
        latency_ms=latency_ms,
        status=requests_repo.STATUS_ERROR,
        error_code=error.code,
        substituted=substituted,
        # The trail matters more here than on the success path: this is the row
        # that answers "why did this take eight seconds before failing?", and the
        # error alone names one candidate out of however many were tried.
        attempts=[record.to_json() for record in attempts],
    )

    # `log_fields()` carries the routing flags — retryable, failover-eligible,
    # breaker-eligible — which is what makes "why did this not fail over?"
    # answerable from the log rather than from the error table in §2.1.2.
    fields = {
        **error.log_fields(),
        "request_row_id": str(row.id),
        "conversation_id": str(conversation_id) if conversation_id else None,
        "requested_slot": requested_slot,
        "served_slot": spec.slot if spec is not None else None,
        "latency_ms": latency_ms,
        "attempts": len(attempts),
        # The provider's own prose, kept out of the response body and put here
        # instead: it is written for whoever holds the upstream account.
        "detail": str(error),
    }
    if error.alert:
        logger.error("chat.failed", **fields)
    else:
        logger.warning("chat.failed", **fields)

    return row
