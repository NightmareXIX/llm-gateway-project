"""Step 10: after ``done``, write down what the stream produced.

When a stream ends, the answer exists in two places — the user's screen, and
the in-memory buffer :func:`~app.streaming.orchestrator.stream_completion`
is about to let go of. Nothing has been persisted. This module is the
:class:`~app.streaming.orchestrator.StreamPersistence` the orchestrator hands
its finished :class:`~app.streaming.orchestrator.StreamResult` to: one
``messages`` row containing the final assembled answer and the model that
actually produced it, plus a ``requests`` row carrying the full attempt trail.

**D14 governs the session.** The orchestrator holds none for the duration of
the generation, and this is where one is finally opened — short-lived, from
``app.state.db_session_factory``, after the stream is already over. A
persistence failure must not turn a request that visibly succeeded into a
traceback: this module does not catch its own exceptions, because
:func:`~app.streaming.orchestrator._persist` already does, at the one layer
that knows there is no response left to turn one into.

**Invariant 4 decides the branch.** A failed stream's ``StreamResult.text`` is
the longest discarded partial (:attr:`StreamResult.status` is ``"failed"``),
offered to the client as ``partial_content`` — never as a stored message, or
an empty or half-answered generation would become a real row in a user's
history. Only the ``requests`` row is written for a failure, mirroring exactly
what the non-streaming endpoint already does in its own failure branch.

**Who is asking is bound at construction, not carried on the result.**
:class:`StreamResult` is the routing layer's idea of a finished turn, and it
has no notion of a :class:`Principal` — quota and ownership are this
module's concerns, not the router's. So a :class:`Collector` is built once
per request, with the caller's principal closed over, and reused for the one
result it will ever be handed.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal
from app.db.repo import messages as messages_repo
from app.memory.canonical import MessageMeta, text_block
from app.streaming.orchestrator import StreamResult
from app.usage import logger as usage_logger

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
"""What ``app.state.db_session_factory`` (an ``async_sessionmaker``) satisfies
structurally: calling it with no arguments hands back something usable as
``async with factory() as session``. Typed this loosely rather than as
``async_sessionmaker[AsyncSession]`` itself so a test can hand over a plain
``@asynccontextmanager`` function wrapping a single transactional session,
without needing a second engine just to satisfy mypy.
"""


class Collector:
    """The orchestrator's :class:`~app.streaming.orchestrator.StreamPersistence`.

    Takes a session *factory* rather than a session (D14) — that is also what
    makes this class testable without standing up a request: hand it a
    factory over a throwaway engine and a result built by hand.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        principal: Principal,
    ) -> None:
        self._session_factory = session_factory
        self._principal = principal

    async def persist(self, result: StreamResult) -> None:
        """One transaction, whichever branch it takes.

        Committing both rows together — rather than the message in one
        transaction and the request in another — is what keeps
        ``messages.meta.attempts`` and ``requests.attempts`` from ever
        disagreeing about whether the write happened at all.
        """
        async with self._session_factory() as session:
            if result.status == "ok":
                await self._persist_success(session, result)
            else:
                await self._persist_failure(session, result)
            await session.commit()

    async def _persist_success(self, session: AsyncSession, result: StreamResult) -> None:
        # Never None here: a "ok" status is only reached through `_Turn.complete`,
        # which always carries the serving candidate off `StreamCompleted`.
        assert result.spec is not None
        spec = result.spec

        await messages_repo.append(
            session,
            conversation_id=result.conversation_id,
            user_id=self._principal.user_id,
            role="assistant",
            content=[text_block(result.text)],
            # Minted up front by the orchestrator (Step 9): `meta` promised this
            # id to the client with the first token, long before this row exists.
            message_id=result.message_id,
            meta=MessageMeta(
                provider_used=spec.provider,
                model_used=spec.model,
                slot_used=spec.slot,
                requested_slot=result.requested_slot,
                substituted=result.substituted,
                attempts=result.attempts,
                tokens_in=result.usage.tokens_in,
                tokens_out=result.usage.tokens_out,
                wasted_tokens_out=result.wasted_tokens_out,
                degraded=result.degraded,
            ),
        )
        await usage_logger.record_success(
            session,
            principal=self._principal,
            spec=spec,
            requested_slot=result.requested_slot,
            usage=result.usage,
            latency_ms=result.latency_ms,
            conversation_id=result.conversation_id,
            attempts=result.trail,
            substituted=result.substituted,
            wasted_tokens_out=result.wasted_tokens_out,
        )

    async def _persist_failure(self, session: AsyncSession, result: StreamResult) -> None:
        """No ``messages`` row. Invariant 4 — an empty or half-written
        generation is an error, not a stored message — and by the time a
        stream reaches ``failed`` there is no complete answer to store, only
        the partial the client was already offered a chance to keep.

        Only reachable for an *in-band* failure: everything that fails before
        the first byte raises out of the orchestrator instead of ever calling
        ``persist`` (D13), and the endpoint's own request-scoped session
        writes that row the same way it always has.
        """
        await usage_logger.record_stream_failure(
            session,
            principal=self._principal,
            requested_slot=result.requested_slot,
            latency_ms=result.latency_ms,
            error_code=result.error_code,
            spec=result.spec,
            conversation_id=result.conversation_id,
            attempts=result.trail,
            substituted=result.substituted,
            wasted_tokens_out=result.wasted_tokens_out,
        )


__all__ = ["Collector"]
