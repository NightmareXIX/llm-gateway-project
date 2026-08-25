"""The one place a ``requests`` row is built, and the one place a turn is logged.

Three functions, two outcomes. The caller hands over the objects it already
has — a :class:`Principal`, a :class:`ModelSpec`, a :class:`Usage` or a
:class:`ProviderError` — and never assembles a row itself.

**Why a facade over a one-function repo.** Phase 1 had exactly one caller, so this
looked like a wrapper. Phase 2 has three: the non-streaming endpoint records both
outcomes with a full :class:`ProviderError` in hand, and the streaming collector
(Step 10) records a message whose tokens were partly discarded (D1's
``wasted_tokens_out``) — but only ever after the 200 was already committed (D13),
by which point the orchestrator has already reduced a failure down to a wire-safe
``error_code`` with no live error object behind it. That is what
:func:`record_stream_failure` is for, rather than a third ``error`` shape bolted
onto :func:`record_failure`. Three call sites each building a ``Request`` by hand
is how ``status`` ends up spelled two ways and how the field Phase 7's dashboard
needs turns out to be populated on two of the three. The repo owns the INSERT;
this owns what goes in it.

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
    wasted_tokens_out: int = 0,
    quota_scope: str = "system",
) -> Request:
    """Record a turn that produced an answer.

    ``attempts`` is the router's trail — every event, including the candidates it
    skipped and the ones that failed before this one worked. A successful turn
    with a three-entry trail is the most interesting row in the table, and it is
    the only place that story survives: one ``messages`` row per logical message
    means the discarded attempts leave no other trace.

    ``wasted_tokens_out`` defaults to 0, which is exactly what every non-streaming
    caller has to report: a failed ``complete`` produced no text, so there is
    nothing to have wasted. The streaming collector is the first caller to pass a
    non-zero value — tokens a *discarded* attempt generated on the way to this
    successful one, really spent even though the client never saw them.

    ``quota_scope`` (Phase 6 Step 7) is who actually paid: ``"system"`` for the
    shared pool, a ``user_id`` string for a private key — the caller derives it
    from the winning attempt's ``key_pool`` (D42) via
    :func:`~app.keys_resolution.resolver.quota_scope_for`, since that pool
    label plus the caller's own principal is exactly what
    :class:`~app.keys_resolution.resolver.ResolvedKey.scope` would have been.
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
        wasted_tokens_out=wasted_tokens_out,
        quota_scope=quota_scope,
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
        wasted_tokens_out=wasted_tokens_out,
        quota_scope=quota_scope,
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
    quota_scope: str = "system",
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

    ``quota_scope`` (Phase 6 Step 7): the *last attempted* candidate's pool, the
    same way ``spec``/``served_slot`` already fall back to the last one tried
    rather than the one requested. ``"system"`` when every candidate was
    skipped on an open breaker and nothing ever resolved a credential.
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
        quota_scope=quota_scope,
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
        "quota_scope": quota_scope,
        # The provider's own prose, kept out of the response body and put here
        # instead: it is written for whoever holds the upstream account.
        "detail": str(error),
    }
    if error.alert:
        logger.error("chat.failed", **fields)
    else:
        logger.warning("chat.failed", **fields)

    return row


async def record_cache_hit(
    session: AsyncSession,
    *,
    principal: Principal,
    provider: str,
    model: str,
    slot: str,
    requested_slot: str,
    latency_ms: int,
    conversation_id: UUID | None = None,
    substituted: bool = False,
    quota_scope: str = "system",
) -> Request:
    """Record a turn served entirely from D19's exact-match cache.

    A fourth facade function rather than a special case bolted onto
    :func:`record_success`: a cache hit never routed, so there is no
    :class:`~app.providers.types.ModelSpec` in hand and no attempt trail to
    write — only the three strings :class:`~app.cache.exact.CachedResponse`
    carries. ``tokens_in``/``tokens_out`` are zero because a hit costs nothing;
    ``provider``/``model``/``slot`` name the candidate that originally produced
    the cached text, which is what ``served_by`` on the response continues to
    disclose.

    ``quota_scope`` always ``"system"`` (Phase 6 Step 7): a hit spends no
    credential and no quota, the same reasoning that gives it
    ``messages_dropped=0``/``extraction_tier=None`` elsewhere (phase5 trap 3)
    — passed explicitly rather than left to the default so a reader does not
    have to wonder whether this call site simply forgot the parameter.
    """
    row = await requests_repo.create(
        session,
        user_id=principal.user_id,
        api_key_id=principal.api_key_id,
        conversation_id=conversation_id,
        requested_slot=requested_slot,
        served_slot=slot,
        provider=provider,
        model=model,
        tokens_in=0,
        tokens_out=0,
        latency_ms=latency_ms,
        status=requests_repo.STATUS_OK,
        cache_hit=True,
        substituted=substituted,
        attempts=[],
        quota_scope=quota_scope,
    )

    logger.info(
        "chat.cache_hit",
        request_row_id=str(row.id),
        conversation_id=str(conversation_id) if conversation_id else None,
        requested_slot=requested_slot,
        served_slot=slot,
        provider=provider,
        model=model,
        latency_ms=latency_ms,
        substituted=substituted,
    )
    return row


async def record_stream_failure(
    session: AsyncSession,
    *,
    principal: Principal,
    requested_slot: str,
    latency_ms: int,
    error_code: str | None,
    spec: ModelSpec | None = None,
    conversation_id: UUID | None = None,
    attempts: Sequence[AttemptRecord] = (),
    substituted: bool = False,
    wasted_tokens_out: int = 0,
    quota_scope: str = "system",
) -> Request:
    """Record a streamed turn that failed *in-band*, after the 200 committed.

    The streaming twin of :func:`record_failure`, and deliberately not the same
    function. By the time the collector sees a failed :class:`StreamResult`, D13's
    boundary has already been crossed — the orchestrator reduced the router's
    :class:`~app.providers.errors.ProviderError` down to a wire-safe
    ``error_code`` for the ``done`` event, and the object itself is gone. So this
    takes the pieces that survive instead: ``spec`` names the last candidate
    attempted (or ``None`` when every one was skipped on an open breaker), the
    same way :func:`record_failure` falls back to it for ``served_slot``.

    A pre-first-byte exhaustion never reaches here at all — it still raises
    ``RoutingFailed`` out of the orchestrator, and the endpoint's own
    request-scoped session calls :func:`record_failure` for it exactly as the
    non-streaming path always has.
    """
    row = await requests_repo.create(
        session,
        user_id=principal.user_id,
        api_key_id=principal.api_key_id,
        conversation_id=conversation_id,
        requested_slot=requested_slot,
        served_slot=spec.slot if spec is not None else None,
        provider=spec.provider if spec is not None else None,
        model=spec.model if spec is not None else None,
        latency_ms=latency_ms,
        status=requests_repo.STATUS_ERROR,
        error_code=error_code,
        substituted=substituted,
        attempts=[record.to_json() for record in attempts],
        wasted_tokens_out=wasted_tokens_out,
        quota_scope=quota_scope,
    )

    logger.warning(
        "chat.stream_failed",
        request_row_id=str(row.id),
        conversation_id=str(conversation_id) if conversation_id else None,
        requested_slot=requested_slot,
        served_slot=spec.slot if spec is not None else None,
        provider=spec.provider if spec is not None else None,
        model=spec.model if spec is not None else None,
        error_code=error_code,
        latency_ms=latency_ms,
        attempts=len(attempts),
        wasted_tokens_out=wasted_tokens_out,
        quota_scope=quota_scope,
    )
    return row
