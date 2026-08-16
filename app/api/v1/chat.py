"""``POST /v1/chat/completions`` — the endpoint the whole of Phase 1 exists for.

Everything here is assembly. The canonical schema, the repos, the registry, the
render pipeline and the Groq adapter were each built against a contract; this is
where they are wired together, and the interesting content is the *order* of the
steps rather than any one of them.

Three decisions worth reading before the code.

**The user's message is committed before the provider is called.** Two reasons,
and the second is the important one. A transaction held open across a call that
can legitimately take sixty seconds pins a Postgres connection for its duration,
which on a free tier is most of the pool. And when the provider fails, the user's
message should still be there: a thread that silently loses what someone typed is
worse than one that shows an error next to it.

**Every payload comes out of ``render()``.** Never ``adapter.build_payload``
directly — and since Phase 2 Step 5, never from here at all: the router owns the
render-and-attempt pair, because it may do it more than once per turn.

**``served_by`` is on every response from the first one.** Phase 1 could not
substitute anything, so it was constant. It shipped anyway, because D1 and D2 both
resolve to "substitute silently, then disclose", and a client that only learns to
read the disclosure in Phase 2 spent Phase 1 training its users to believe one
model answered everything. Step 5 is where it starts telling the truth.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from functools import partial
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.dependency import PrincipalDep
from app.auth.principal import Principal
from app.config import get_settings
from app.core.clock import SYSTEM_CLOCK
from app.core.errors import InvalidRequest, NotFound
from app.core.logging import get_logger, get_request_id
from app.db.models import Conversation
from app.db.repo import conversations as conversations_repo
from app.db.repo import messages as messages_repo
from app.deps import BreakerDep, LatencyDep, RedisDep, RegistryDep, SessionDep, SessionFactoryDep
from app.memory.canonical import CanonicalMessage, MessageMeta, text_block
from app.providers.errors import to_app_error
from app.providers.registry import ProviderRegistry, UnknownSlot
from app.providers.types import GenParams, ModelSpec
from app.routing import router as routing
from app.routing import selection
from app.routing.circuit_breaker import CircuitBreaker
from app.schemas.chat import (
    AssistantMessage,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    ServedBy,
    UsageOut,
)
from app.schemas.errors import AUTHENTICATED_ERROR_RESPONSES, NOT_FOUND_RESPONSE, ErrorResponse
from app.streaming import sse
from app.streaming.collector import Collector
from app.streaming.orchestrator import stream_completion
from app.usage import logger as usage_logger
from app.usage.metrics import LatencyTable

logger = get_logger("app.api.chat")

router = APIRouter(prefix="/v1/chat", tags=["chat"], responses=AUTHENTICATED_ERROR_RESPONSES)


@router.post(
    "/completions",
    response_model=ChatCompletionResponse,
    responses={
        **NOT_FOUND_RESPONSE,
        400: {"model": ErrorResponse, "description": "The request cannot be served as asked."},
        502: {"model": ErrorResponse, "description": "The model provider failed."},
    },
)
async def create_chat_completion(
    body: ChatCompletionRequest,
    request: Request,
    principal: PrincipalDep,
    session: SessionDep,
    session_factory: SessionFactoryDep,
    registry: RegistryDep,
    breaker: BreakerDep,
    latency: LatencyDep,
    redis: RedisDep,
) -> ChatCompletionResponse | StreamingResponse:
    """Answer one turn, persisting both halves of it.

    ``stream: true`` diverges after the inbound turn is committed — the two
    paths share everything up to there, which is D14's boundary as much as
    D13's: the request-scoped ``session`` above persists the user's message and
    is then let go, exactly as the non-streaming path already did, before either
    branch calls out to a provider that can legitimately take sixty seconds.
    """
    # Rejected here rather than inside the router, because only this layer knows
    # the name came off a request body and is therefore a 400. Before the
    # conversation is touched, so a typo'd slot does not open a thread it will
    # never answer.
    _validate_slot(registry, body.model)

    conversation = await _resolve_conversation(session, principal=principal, body=body)
    conversation_id = conversation.id

    # --- persist the inbound turn, then let go of the transaction ----------- #
    for message in body.messages:
        await messages_repo.append(
            session,
            conversation_id=conversation_id,
            user_id=principal.user_id,
            role=message.role,
            content=[text_block(message.content)],
        )
    await conversations_repo.touch(
        session, conversation_id=conversation_id, user_id=principal.user_id
    )
    await session.commit()

    history = await messages_repo.list_for_conversation(
        session, conversation_id=conversation_id, user_id=principal.user_id
    )
    params = GenParams(
        temperature=body.temperature,
        max_tokens=body.max_tokens,
        top_p=body.top_p,
        stop=list(body.stop),
        stream=body.stream,
    )

    if body.stream:
        return await _stream_chat_completion(
            request=request,
            session=session,
            body=body,
            principal=principal,
            conversation=conversation,
            history=history,
            params=params,
            registry=registry,
            breaker=breaker,
            latency=latency,
            redis=redis,
            session_factory=session_factory,
        )

    started = time.perf_counter()
    try:
        # One call, and everything that used to be here is behind it: the
        # candidate chain, the breaker, the render, the attempt, the failover and
        # the trail. The endpoint's job is now the two halves of persistence.
        outcome = await routing.route(
            registry=registry,
            breaker=breaker,
            history=history,
            params=params,
            requested=body.model,
            # D3: a conversation that has made a tool call is locked to one
            # model, and that outranks both `auto` and a named slot.
            pinned=conversation.pinned_model,
            metrics=latency,
            rank_by_latency=get_settings().ROUTING_LATENCY_RANKING,
        )
    except routing.RoutingFailed as failure:
        # Includes `ContextTooLong` raised by the fitting step before a request
        # was ever sent — it is a provider-shaped failure with a provider and a
        # model attached, and recording it is how "this model's window is too
        # small for real conversations" becomes visible in the usage table.
        await usage_logger.record_failure(
            session,
            principal=principal,
            error=failure.error,
            requested_slot=body.model,
            spec=failure.spec,
            latency_ms=_elapsed_ms(started),
            conversation_id=conversation_id,
            attempts=failure.trail,
        )
        await session.commit()
        # Substitutes a message written for our caller; the provider's own prose
        # stayed in the log line above, with the request_id. `to_app_error` sees
        # the normalized error, not the wrapper — it needs to know nothing about
        # routing.
        raise to_app_error(failure.error) from failure

    latency_ms = _elapsed_ms(started)
    spec = outcome.spec
    completion = outcome.completion
    substituted = _is_substitution(body.model, spec.slot)

    # Note what is *not* handled here: an empty generation. The adapter raises
    # `EmptyResponse` for a 200-with-nothing, so it leaves through the branch
    # above and no assistant row is written — which is invariant 4 ("an empty
    # generation is an error, not a stored message") satisfied by doing nothing
    # rather than by a special case.
    assistant = await messages_repo.append(
        session,
        conversation_id=conversation_id,
        user_id=principal.user_id,
        role="assistant",
        content=[text_block(completion.text)],
        meta=MessageMeta(
            # The *serving* candidate, not the requested one. On a failed-over
            # turn these three fields are the only record of which model actually
            # wrote the text sitting next to them.
            provider_used=spec.provider,
            model_used=spec.model,
            slot_used=spec.slot,
            requested_slot=body.model,
            substituted=substituted,
            attempts=outcome.attempts,
            tokens_in=completion.usage.tokens_in,
            tokens_out=completion.usage.tokens_out,
            degraded=outcome.report.degraded,
        ),
    )
    await usage_logger.record_success(
        session,
        principal=principal,
        spec=spec,
        requested_slot=body.model,
        usage=completion.usage,
        latency_ms=latency_ms,
        conversation_id=conversation_id,
        attempts=outcome.trail,
        substituted=substituted,
    )
    await session.commit()

    return _to_response(
        body=body,
        spec=spec,
        completion_text=completion.text,
        finish_reason=completion.finish_reason,
        tokens_in=completion.usage.tokens_in,
        tokens_out=completion.usage.tokens_out,
        estimated=completion.usage.estimated,
        degraded=outcome.report.degraded,
        attempts=outcome.attempts,
        conversation_id=conversation_id,
        assistant=assistant,
    )


# --------------------------------------------------------------------------- #
# The streaming twin (D13/D14)
# --------------------------------------------------------------------------- #
async def _stream_chat_completion(
    *,
    request: Request,
    session: AsyncSession,
    body: ChatCompletionRequest,
    principal: Principal,
    conversation: Conversation,
    history: list[CanonicalMessage],
    params: GenParams,
    registry: ProviderRegistry,
    breaker: CircuitBreaker,
    latency: LatencyTable,
    redis: Redis,
    session_factory: async_sessionmaker[AsyncSession],
) -> StreamingResponse:
    """Drive the router loop far enough to know whether anything will be sent,
    *before* committing to a 200 (D13).

    ``StreamingResponse.__call__`` sends the ASGI ``http.response.start`` — the
    status line — the instant Starlette starts iterating its body, not when the
    body first yields. So the only way to keep a fully-down provider pool
    debuggable as an ordinary JSON error is to prime the generator by hand,
    outside of Starlette's control, and only construct the ``StreamingResponse``
    once that first frame — or a :class:`~app.routing.router.RoutingFailed` — is
    already in hand.

    A pre-first-byte failure is recorded right here, on the request-scoped
    ``session`` that is still open at this point — the same session and the same
    :func:`~app.usage.logger.record_failure` the non-streaming path uses, because
    nothing about this failure is streaming-shaped yet: no candidate ever
    produced a token. Once a first frame *has* gone out, persistence stops being
    this function's job at all: the :class:`~app.streaming.collector.Collector`
    below is the only thing that writes anything from that point on (Step 10),
    working off a session it opens for itself well after this function has
    already returned.
    """
    message_id = uuid4()
    started = time.perf_counter()
    frames = stream_completion(
        registry=registry,
        breaker=breaker,
        history=history,
        params=params,
        requested=body.model,
        conversation_id=conversation.id,
        message_id=message_id,
        # D3: a conversation that has made a tool call is locked to one model,
        # and that outranks both `auto` and a named slot — same rule as the
        # non-streaming path, just read one step earlier.
        pinned=conversation.pinned_model,
        metrics=latency,
        rank_by_latency=get_settings().ROUTING_LATENCY_RANKING,
        redis=redis,
        persistence=Collector(session_factory, principal=principal),
        is_disconnected=partial(sse.client_disconnected, request),
    ).__aiter__()

    try:
        first = await frames.__anext__()
    except routing.RoutingFailed as failure:
        # Nothing was ever sent, so the error envelope — and the `requests` row
        # that explains it — are still ours to give, exactly as the
        # non-streaming path gives them.
        await usage_logger.record_failure(
            session,
            principal=principal,
            error=failure.error,
            requested_slot=body.model,
            spec=failure.spec,
            latency_ms=_elapsed_ms(started),
            conversation_id=conversation.id,
            attempts=failure.trail,
        )
        await session.commit()
        raise to_app_error(failure.error) from failure

    async def rest() -> AsyncIterator[bytes]:
        yield first
        async for chunk in frames:
            yield chunk

    return StreamingResponse(rest(), media_type=sse.SSE_MEDIA_TYPE, headers=sse.SSE_HEADERS)


# --------------------------------------------------------------------------- #
# Steps, pulled out so the handler above reads as the sequence it is
# --------------------------------------------------------------------------- #
async def _resolve_conversation(
    session: AsyncSession,
    *,
    principal: Principal,
    body: ChatCompletionRequest,
) -> Conversation:
    """Continue the caller's thread, or start one.

    Returns the row rather than its id, because the handler needs
    ``pinned_model`` off it (D3). Both branches already have the row in hand, so
    this costs nothing a second query would not.

    A ``conversation_id`` that is not theirs is a 404, never a 403 — the
    repository scopes ownership inside the SELECT, so a miss and a non-existent
    id are the same answer, and a 403 would confirm the id names something real.
    """
    if body.conversation_id is None:
        return await conversations_repo.create(
            session,
            user_id=principal.user_id,
            preferred_slot=body.model,
        )

    existing = await conversations_repo.get_owned(
        session, conversation_id=body.conversation_id, user_id=principal.user_id
    )
    if existing is None:
        raise NotFound("That conversation does not exist.", code="conversation_not_found")

    # Invariant 1's other half, which the schema validator cannot check: a
    # leading system message is only legal in a *new* conversation, because a
    # conversation may hold at most one and it must sit at seq 0.
    if any(message.role == "system" for message in body.messages):
        raise InvalidRequest(
            "A system message can only be set when a conversation is created.",
            code="system_message_not_first",
        )

    return existing


def _validate_slot(registry: ProviderRegistry, requested: str) -> None:
    """Reject a slot name the table does not carry, before anything else happens.

    ``UnknownSlot`` is translated here rather than in ``selection.py`` because
    this is the layer that knows where the name came from: off a request body, so
    it is the client's mistake and a 400. The same lookup failing on a stored
    ``preferred_slot`` or a stale ``pinned_model`` would be ours, and a 500.

    Checked up front rather than by catching ``UnknownSlot`` around the router:
    the registry raises the same class for a provider with no adapter and for one
    with no key, and both of those are our configuration being wrong, not the
    caller's request. Wrapping the router would label them 400s and send someone
    looking in the wrong place.
    """
    if requested == selection.AUTO:
        return
    try:
        registry.candidates(requested)
    except UnknownSlot as exc:
        raise InvalidRequest(
            f"Unknown model slot {requested!r}.",
            code="unknown_slot",
            details={"available": [selection.AUTO, *registry.slots()]},
        ) from exc


def _is_substitution(requested_slot: str, served_slot: str) -> bool:
    """Whether the client's explicit choice was overridden.

    Resolving ``auto`` is not substitution — nothing was overridden, the client
    asked the gateway to choose. Phase 1 could only ever return ``False`` here;
    it was computed rather than hardcoded so that Step 5 would inherit the right
    rule instead of a constant someone had to remember to change, and D10's spill
    (ADR-011) is what finally makes it reachable.

    Delegates since Step 9: the streaming orchestrator decides the same thing
    about the same fields, and two copies of it would let one path disclose a
    substitution the other hid.
    """
    return selection.is_substitution(requested_slot, served_slot)


def _elapsed_ms(started: float) -> int:
    """Wall-clock milliseconds since ``started``, from a monotonic source.

    ``perf_counter`` rather than the clock: this is a duration, and a duration
    measured against a wall clock goes negative when NTP steps it backwards.
    """
    return int((time.perf_counter() - started) * 1000)


def _to_response(
    *,
    body: ChatCompletionRequest,
    spec: ModelSpec,
    completion_text: str,
    finish_reason: str,
    tokens_in: int,
    tokens_out: int,
    estimated: bool,
    degraded: bool,
    attempts: int,
    conversation_id: UUID,
    assistant: CanonicalMessage,
) -> ChatCompletionResponse:
    """Assemble the wire body. Pure, so the shape is testable without a database."""
    return ChatCompletionResponse(
        # The request id, so what a user quotes from the UI is a log query.
        id=get_request_id() or str(assistant.id),
        created=int(SYSTEM_CLOCK.now().timestamp()),
        model=spec.model,
        choices=[
            Choice(
                index=0,
                message=AssistantMessage(content=completion_text),
                finish_reason=finish_reason,
            )
        ],
        usage=UsageOut(
            prompt_tokens=tokens_in,
            completion_tokens=tokens_out,
            total_tokens=tokens_in + tokens_out,
            estimated=estimated,
        ),
        served_by=ServedBy(slot=spec.slot, provider=spec.provider, model=spec.model),
        requested_slot=body.model,
        substituted=_is_substitution(body.model, spec.slot),
        attempts=attempts,
        degraded=degraded,
        conversation_id=conversation_id,
        message_id=assistant.id,
    )
