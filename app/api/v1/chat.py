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
directly. Attachment resolution, budgeting and D4 fitting live in the pipeline's
six steps, and a call site that skips it gets none of them — silently, and only
for whichever conversation happened to be too long.

**``served_by`` is on every response from the first one.** Phase 1 cannot
substitute anything, so it is constant here. It ships anyway, because D1 and D2
both resolve to "substitute silently, then disclose", and a client that only
learns to read the disclosure in Phase 2 spent Phase 1 training its users to
believe one model answered everything.
"""

from __future__ import annotations

import time
from uuid import UUID

from fastapi import APIRouter
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependency import PrincipalDep
from app.auth.principal import Principal
from app.core.clock import SYSTEM_CLOCK
from app.core.errors import InvalidRequest, NotFound
from app.core.logging import get_logger, get_request_id
from app.db.repo import conversations as conversations_repo
from app.db.repo import messages as messages_repo
from app.deps import RegistryDep, SessionDep
from app.memory.canonical import CanonicalMessage, MessageMeta, text_block
from app.memory.render import render
from app.providers.base import DEFAULT_READ_TIMEOUT_S
from app.providers.errors import ProviderError, to_app_error
from app.providers.registry import ProviderRegistry, UnknownSlot
from app.providers.types import GenParams, ModelSpec
from app.routing import selection
from app.schemas.chat import (
    AssistantMessage,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    ServedBy,
    UsageOut,
)
from app.schemas.errors import AUTHENTICATED_ERROR_RESPONSES, NOT_FOUND_RESPONSE, ErrorResponse
from app.usage import logger as usage_logger

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
    principal: PrincipalDep,
    session: SessionDep,
    registry: RegistryDep,
) -> ChatCompletionResponse:
    """Answer one turn, persisting both halves of it."""
    if body.stream:
        # Refused, not downgraded. A client that asked for deltas and received one
        # blob has no way to distinguish that from a very fast model, and would
        # ship a streaming UI that silently never streams.
        raise InvalidRequest(
            "Streaming is not available yet. Send stream: false.",
            code="streaming_not_supported",
        )

    conversation_id = await _resolve_conversation(session, principal=principal, body=body)

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

    spec = _resolve_spec(registry, body.model)
    adapter = registry.adapter_for_spec(spec)
    # Phase 6 replaces this line with `resolve_provider_key(user_id, provider)`,
    # which checks the user's own key first and falls back to exactly this value.
    key = registry.system_key(spec.provider)

    history = await messages_repo.list_for_conversation(
        session, conversation_id=conversation_id, user_id=principal.user_id
    )
    params = GenParams(
        temperature=body.temperature,
        max_tokens=body.max_tokens,
        top_p=body.top_p,
        stop=list(body.stop),
        stream=False,
    )

    started = time.perf_counter()
    try:
        payload, report = await render(history, spec, params, adapter)
        completion = await adapter.complete(payload, key, timeout=DEFAULT_READ_TIMEOUT_S)
    except ProviderError as exc:
        # Includes `ContextTooLong` raised by the fitting step before a request
        # was ever sent — it is a provider-shaped failure with a provider and a
        # model attached, and recording it is how "this model's window is too
        # small for real conversations" becomes visible in the usage table.
        await usage_logger.record_failure(
            session,
            principal=principal,
            error=exc,
            requested_slot=body.model,
            spec=spec,
            latency_ms=_elapsed_ms(started),
            conversation_id=conversation_id,
        )
        await session.commit()
        # Substitutes a message written for our caller; the provider's own prose
        # stayed in the log line above, with the request_id.
        raise to_app_error(exc) from exc

    latency_ms = _elapsed_ms(started)

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
            provider_used=spec.provider,
            model_used=spec.model,
            slot_used=spec.slot,
            requested_slot=body.model,
            substituted=_is_substitution(body.model, spec.slot),
            attempts=1,
            tokens_in=completion.usage.tokens_in,
            tokens_out=completion.usage.tokens_out,
            degraded=report.degraded,
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
        degraded=report.degraded,
        conversation_id=conversation_id,
        assistant=assistant,
    )


# --------------------------------------------------------------------------- #
# Steps, pulled out so the handler above reads as the sequence it is
# --------------------------------------------------------------------------- #
async def _resolve_conversation(
    session: AsyncSession,
    *,
    principal: Principal,
    body: ChatCompletionRequest,
) -> UUID:
    """Continue the caller's thread, or start one.

    A ``conversation_id`` that is not theirs is a 404, never a 403 — the
    repository scopes ownership inside the SELECT, so a miss and a non-existent
    id are the same answer, and a 403 would confirm the id names something real.
    """
    if body.conversation_id is None:
        conversation = await conversations_repo.create(
            session,
            user_id=principal.user_id,
            preferred_slot=body.model,
        )
        return conversation.id

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

    return existing.id


def _resolve_spec(registry: ProviderRegistry, requested: str) -> ModelSpec:
    """Slot name → the model that will answer.

    ``UnknownSlot`` is translated here rather than in ``selection.py`` because
    this is the layer that knows where the name came from: off a request body, so
    it is the client's mistake and a 400. The same lookup failing on a stored
    ``preferred_slot`` would be ours, and a 500.
    """
    try:
        return selection.resolve_slot(registry, requested)
    except UnknownSlot as exc:
        raise InvalidRequest(
            f"Unknown model slot {requested!r}.",
            code="unknown_slot",
            details={"available": [selection.AUTO, *registry.slots()]},
        ) from exc


def _is_substitution(requested_slot: str, served_slot: str) -> bool:
    """Whether the client's explicit choice was overridden.

    Resolving ``auto`` is not substitution — nothing was overridden, the client
    asked the gateway to choose. Phase 1 has no failover, so this is always
    ``False``; it is computed rather than hardcoded so Phase 2 inherits the
    right rule instead of a constant someone has to remember to change.
    """
    return requested_slot != selection.AUTO and requested_slot != served_slot


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
        attempts=1,
        degraded=degraded,
        conversation_id=conversation_id,
        message_id=assistant.id,
    )
