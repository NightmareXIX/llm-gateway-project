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
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal
from app.cache.exact import CachedResponse, ExactCache
from app.core.clock import SYSTEM_CLOCK
from app.db.repo import messages as messages_repo
from app.keys_resolution.resolver import quota_scope_for
from app.memory.canonical import MessageMeta, text_block
from app.streaming.orchestrator import StreamResult
from app.usage import logger as usage_logger
from app.usage.metrics import STREAM, MetricsRegistry

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
        cache: ExactCache | None = None,
        cache_key: str | None = None,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._principal = principal
        self._metrics = metrics
        self._cache = cache
        self._cache_key = cache_key
        """D19's write side, threaded in rather than recomputed: ``cache_key`` is
        only ever non-``None`` when the endpoint already established, before the
        stream started, that ``(temperature, history)`` was eligible
        (``is_cacheable``) — this class checks only the one dimension that
        cannot be known until the turn is over, ``result.degraded``."""

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
                extraction_tier=result.extraction_tier,
                messages_dropped=result.messages_dropped,
                key_pool=result.key_pool,
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
            quota_scope=quota_scope_for(result.key_pool, self._principal.user_id),
            metrics=self._metrics,
            key_pool=result.key_pool,
            # Every turn this class records is a streamed one, so the mode is
            # never in question here the way it is at the endpoint, which serves
            # both.
            mode=STREAM,
        )

        # D5/D19's streaming write, exactly where this docstring promised it
        # would land. `result.degraded` and `result.messages_dropped` (D35) are
        # the two halves of `is_cacheable` the endpoint could not have checked
        # before the turn existed; the rest is why `self._cache_key` is `None`
        # at all whenever it does not hold. Routed through the same field
        # `messages_dropped` already carries (Step 3) rather than a second,
        # parallel `truncated` flag on `StreamResult`.
        if (
            self._cache is not None
            and self._cache_key is not None
            and not result.degraded
            and result.messages_dropped == 0
        ):
            await self._cache.put(
                self._cache_key,
                CachedResponse(
                    text=result.text,
                    provider=spec.provider,
                    model=spec.model,
                    slot=spec.slot,
                    finish_reason=result.finish_reason,
                    tokens_in=result.usage.tokens_in,
                    tokens_out=result.usage.tokens_out,
                    usage_estimated=result.usage.estimated,
                    created_at=SYSTEM_CLOCK.now().isoformat(),
                ),
            )

    async def persist_cache_hit(
        self,
        *,
        conversation_id: UUID,
        message_id: UUID,
        requested_slot: str,
        provider: str,
        model: str,
        slot: str,
        text: str,
        substituted: bool,
        latency_ms: int,
    ) -> None:
        """The write side of a *replayed* turn — D19's streaming cache hit.

        Not a :class:`StreamResult` branch: a cache hit never routed, so there
        is no attempt trail and no served :class:`~app.providers.types.ModelSpec`
        to read one off — only the pieces
        :func:`~app.streaming.orchestrator.stream_cached_completion` already
        has in hand, the same pieces the non-streaming endpoint's own cache-hit
        branch persists.
        """
        async with self._session_factory() as session:
            await messages_repo.append(
                session,
                conversation_id=conversation_id,
                user_id=self._principal.user_id,
                role="assistant",
                content=[text_block(text)],
                message_id=message_id,
                meta=MessageMeta(
                    provider_used=provider,
                    model_used=model,
                    slot_used=slot,
                    requested_slot=requested_slot,
                    substituted=substituted,
                    attempts=0,
                    tokens_in=0,
                    tokens_out=0,
                    degraded=False,
                ),
            )
            await usage_logger.record_cache_hit(
                session,
                principal=self._principal,
                provider=provider,
                model=model,
                slot=slot,
                requested_slot=requested_slot,
                latency_ms=latency_ms,
                conversation_id=conversation_id,
                substituted=substituted,
                quota_scope="system",
                metrics=self._metrics,
            )
            await session.commit()

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
            quota_scope=quota_scope_for(result.key_pool, self._principal.user_id),
            metrics=self._metrics,
            key_pool=result.key_pool,
        )


__all__ = ["Collector"]
