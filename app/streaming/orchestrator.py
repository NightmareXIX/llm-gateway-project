"""D1's restart state machine — §1.1, on the wire.

Tokens are already on the user's screen and the provider dies halfway through a
sentence. Throw the half answer away, tell the client to forget everything it
has been shown, and start again somewhere else — up to three times, counting the
tokens that were spent producing text nobody will read.

**Almost all of the difficulty is in the rules about when not to restart**, and
each of them is a test:

- **Never after ``done``.** The terminal event makes the message final.
- **Never on a failure that would happen identically elsewhere.** ``BadRequest``
  and ``ContentFiltered`` are terminal by their own class flags;
  ``ContextTooLong`` re-fits once against the same provider and only before the
  first delta. None of that is decided here — it is
  :func:`~app.routing.router.route_stream`'s, made on the flags, and this module
  would be duplicating it if it looked at an error class at all.
- **Never when the user simply left.** A closed laptop is a person leaving, not
  a fault: abort upstream, stop, persist nothing further, and log it as the
  unremarkable event it is rather than as a ``CancelledError`` traceback.
- **Never when nothing was shown.** A candidate that failed before its first
  token has nothing to withdraw, so the failover is silent and the client never
  learns it happened.

**The 200 boundary (D13).** Not one byte is written until an attempt has
produced its first chunk. Everything that fails before that surfaces as an
ordinary JSON error envelope with a ``request_id`` — the shape Phase 1 clients
already handle — and only genuine mid-stream faults have to go in-band as a
``done`` that says ``failed`` inside a 200. Mechanically this is free: Starlette
sends the status line when the body generator yields its first value, so it is
enough not to yield one. The consequence is that ``meta`` goes out immediately
*before* the first delta rather than at request start, which costs nothing —
there was nothing to render until there was a token.

That same boundary is why the heartbeat only starts after ``meta``. A keepalive
comment is a byte like any other and would commit the response; the pre-first-
token silence is bounded by
:data:`~app.providers.base.DEFAULT_FIRST_TOKEN_TIMEOUT_S` instead, which is
exactly the risk D13 introduced that constant to answer.

**No database session is held.** The user's message was committed before any of
this began, the generation holds nothing, and the collector (Step 10) opens its
own short-lived session after ``done``. A free-tier pool cannot afford a
connection pinned for thirty seconds per concurrent chat, and that failure
appears only under load. Persistence arrives here as a callback for the same
reason it must not raise: by the time it runs the user already has their answer,
and there is no response left to turn into a 500.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Literal, Protocol
from uuid import UUID

from redis.asyncio import Redis

from app.cache import keys
from app.cache.exact import CachedResponse, chunk_for_replay
from app.core.clock import SYSTEM_CLOCK, Clock
from app.core.logging import get_logger
from app.memory.canonical import CanonicalMessage
from app.memory.render import AttachmentResolver
from app.providers.base import (
    DEFAULT_FIRST_TOKEN_TIMEOUT_S,
    DEFAULT_IDLE_TIMEOUT_S,
    DEFAULT_READ_TIMEOUT_S,
)
from app.providers.registry import ProviderRegistry
from app.providers.types import ExtractionTier, GenParams, ModelSpec, Usage
from app.quota.tracker import QuotaTracker
from app.routing import router as routing
from app.routing import selection
from app.routing.circuit_breaker import CircuitBreaker
from app.schemas.chat import ServedBy
from app.streaming import sse
from app.usage.metrics import LatencyTable

logger = get_logger("app.streaming.orchestrator")

MIN_PARTIAL_CONTENT_CHARS: Final = 40
"""How much discarded text is worth offering the client to keep.

Below roughly a sentence there is nothing to salvage, and a ``partial_content``
of three characters invites a frontend to render a "keep partial answer" button
next to the word "The". The longest buffer across all attempts is the candidate,
not the last one — the attempt that got furthest is the one worth keeping.
"""


# --------------------------------------------------------------------------- #
# The handoff to Step 10
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class StreamResult:
    """Everything the collector needs, assembled once the stream is over.

    A value rather than a live buffer, so persistence is testable without
    standing up a stream and so nothing downstream can influence what was sent.
    ``spec`` is ``None`` only when every candidate was skipped on an open
    breaker, which is also the one case with no served model to name.
    """

    conversation_id: UUID
    message_id: UUID
    requested_slot: str
    spec: ModelSpec | None

    text: str
    """The assembled answer on success; the longest discarded buffer on failure."""

    usage: Usage
    finish_reason: str
    status: Literal["ok", "failed"]
    error_code: str | None

    substituted: bool
    attempts: int
    trail: tuple[routing.AttemptRecord, ...]
    latency_ms: int
    ttft_ms: int | None
    wasted_tokens_out: int
    degraded: bool
    extraction_tier: ExtractionTier | None = None
    """The worst perception tier this turn's attachments needed (Phase 4).

    Defaulted, unlike every field above it, because a :class:`StreamResult`
    built by hand in a test predates this field and a turn with no attachment
    genuinely has no tier."""

    messages_dropped: int = 0
    """D34's streaming twin of ``RenderReport.messages_dropped``. Set only by
    ``_Turn.complete`` — a failed turn never reached a successful render, so
    it stays the default ``0``, exactly like ``degraded`` and
    ``extraction_tier`` above."""


class StreamPersistence(Protocol):
    """What Step 10's collector implements.

    A protocol rather than an import of the collector, so this module keeps
    knowing nothing about sessions, repositories or ``requests`` rows — and so a
    test can assert on what would have been written by capturing a
    :class:`StreamResult`.
    """

    async def persist(self, result: StreamResult) -> None: ...


class CacheReplayPersistence(Protocol):
    """What :meth:`~app.streaming.collector.Collector.persist_cache_hit`
    implements — D19's streaming write.

    A separate protocol from :class:`StreamPersistence` rather than a second
    ``StreamResult`` shape: a cache hit never routed, so there is no
    :class:`~app.providers.types.ModelSpec`, no attempt trail and no
    :attr:`StreamResult.spec` to be ``None`` in a way callers have to guard
    against. This is the whole of what a replay needs to persist, and nothing
    it does not have.
    """

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
    ) -> None: ...


async def stream_cached_completion(
    cached: CachedResponse,
    *,
    requested_slot: str,
    conversation_id: UUID,
    message_id: UUID,
    latency_ms: int,
    persistence: CacheReplayPersistence | None = None,
) -> AsyncIterator[bytes]:
    """Replay a D19 cache hit as a stream that looks like a real one.

    No candidate is attempted — the text already exists — so there is none of
    :func:`stream_completion`'s restart machinery, and no possibility of a
    pre-first-byte failure (D13): every frame below is guaranteed to be sent,
    which is what lets the caller skip the priming dance the live path needs
    before it can safely construct a ``StreamingResponse``.

    **No artificial delay.** Chunking the text via
    :func:`~app.cache.exact.chunk_for_replay` is what makes the *shape* of the
    stream match a real one — same events, same field names — but inserting a
    typing-speed sleep between chunks would be a lie about where the answer
    came from. ``X-Cache: HIT`` is the honest signal instead.
    """
    substituted = selection.is_substitution(requested_slot, cached.slot)

    yield sse.format_event(
        sse.MetaEvent(
            attempt=0,
            slot=cached.slot,
            provider=cached.provider,
            model=cached.model,
            requested_slot=requested_slot,
            conversation_id=str(conversation_id),
            message_id=str(message_id),
        )
    )
    for piece in chunk_for_replay(cached.text):
        yield sse.format_event(sse.DeltaEvent.of(piece))

    yield sse.format_event(
        sse.DoneEvent(
            served_by=ServedBy(slot=cached.slot, provider=cached.provider, model=cached.model),
            requested_slot=requested_slot,
            substituted=substituted,
            attempts=0,
            usage=sse.StreamUsage(
                prompt_tokens=cached.tokens_in,
                completion_tokens=cached.tokens_out,
                total_tokens=cached.tokens_in + cached.tokens_out,
                estimated=cached.usage_estimated,
                wasted_tokens_out=0,
            ),
            degraded=False,
            # A replay never ran the lane; the tier that produced the original
            # answer is recorded on that answer's own message row.
            extraction_tier=None,
            # Nothing was rendered this turn (trap 3) — the original turn's own
            # row carries whatever it dropped, not this one.
            messages_dropped=0,
            # A replay never re-resolves the pin either — no candidate chain
            # was walked, so there is nothing this turn overrode.
            warning=None,
            status="ok",
        )
    )

    if persistence is None:
        return
    try:
        await persistence.persist_cache_hit(
            conversation_id=conversation_id,
            message_id=message_id,
            requested_slot=requested_slot,
            provider=cached.provider,
            model=cached.model,
            slot=cached.slot,
            text=cached.text,
            substituted=substituted,
            latency_ms=latency_ms,
        )
    except Exception:
        # D14's rule applies here too: the client already has the answer, and
        # there is no response left to turn a raised exception into.
        logger.exception("stream.cache_persist_failed", message_id=str(message_id))


# --------------------------------------------------------------------------- #
# The state machine
# --------------------------------------------------------------------------- #
async def stream_completion(
    *,
    registry: ProviderRegistry,
    breaker: CircuitBreaker,
    history: list[CanonicalMessage],
    params: GenParams,
    requested: str,
    conversation_id: UUID,
    message_id: UUID,
    pinned: str | None = None,
    metrics: LatencyTable | None = None,
    rank_by_latency: bool = True,
    resolver: AttachmentResolver | None = None,
    quota: QuotaTracker | None = None,
    scope: keys.Scope = keys.SYSTEM_SCOPE,
    redis: Redis | None = None,
    persistence: StreamPersistence | None = None,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    timeout_s: float = DEFAULT_READ_TIMEOUT_S,
    idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
    first_token_timeout_s: float = DEFAULT_FIRST_TOKEN_TIMEOUT_S,
    heartbeat_interval_s: float = sse.HEARTBEAT_INTERVAL_S,
    clock: Clock = SYSTEM_CLOCK,
) -> AsyncIterator[bytes]:
    """Drive one streamed turn, yielding SSE frames ready for the wire.

    Raises :class:`~app.routing.router.RoutingFailed` when the whole chain failed
    *before* a single byte went out, so the endpoint can translate it into the
    ordinary error envelope (D13). After the first byte the same failure becomes
    a ``done`` with ``status: "failed"`` and this never raises, because there is
    no longer a response to turn into one.

    ``message_id`` is minted by the caller: ``meta`` promises it to the client
    with the first token, and the row is not written until after ``done``.
    """
    events = routing.route_stream(
        registry=registry,
        breaker=breaker,
        history=history,
        params=params,
        requested=requested,
        pinned=pinned,
        metrics=metrics,
        rank_by_latency=rank_by_latency,
        resolver=resolver,
        quota=quota,
        scope=scope,
        timeout_s=timeout_s,
        idle_timeout_s=idle_timeout_s,
        first_token_timeout_s=first_token_timeout_s,
        clock=clock,
    )

    state = _Turn(
        conversation_id=conversation_id, message_id=message_id, requested=requested, pinned=pinned
    )
    pending: asyncio.Task[routing.RouteStreamEvent] | None = None

    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(events.__anext__())

            # `wait` rather than `wait_for`: a timeout must leave the task
            # running. `wait_for` cancels what it was waiting on, which here is
            # the provider's next chunk — a heartbeat would silently abort the
            # generation it exists to keep alive.
            finished, _ = await asyncio.wait({pending}, timeout=heartbeat_interval_s)

            if await _client_gone(is_disconnected):
                logger.info(
                    "stream.client_disconnected",
                    message_id=str(message_id),
                    attempts=state.attempts,
                    delivered_chars=len(state.buffer),
                )
                return

            if not finished:
                if state.committed:
                    yield sse.format_heartbeat()
                continue

            task, pending = pending, None
            try:
                event = task.result()
            except StopAsyncIteration:  # pragma: no cover — StreamCompleted returns first
                break

            for frame in _apply(state, event):
                yield frame

            if isinstance(event, routing.AttemptStarted):
                await record_attempt(redis, message_id)

            if state.done:
                break

    except routing.RoutingFailed as failure:
        if not state.committed:
            # Nothing has been written, so the status line is still ours. This is
            # the whole payoff of D13: a fully-down provider pool produces a
            # debuggable 502 with a request_id rather than a 200 that says
            # "failed" somewhere inside itself.
            raise

        state.record_failure(failure)
        yield sse.format_event(state.done_event())

    except asyncio.CancelledError:
        # The client went away mid-flight and Starlette cancelled the body task.
        # Logged as the unremarkable thing it is — an event that happens whenever
        # someone closes a tab should not read as an incident — and re-raised,
        # because swallowing a cancellation breaks the task that requested it.
        logger.info(
            "stream.cancelled",
            message_id=str(message_id),
            attempts=state.attempts,
            delivered_chars=len(state.buffer),
        )
        raise

    finally:
        # Order matters. `cancel()` only *requests* cancellation, and calling
        # `aclose()` on a generator that is still unwinding raises "asynchronous
        # generator is already running" — so the in-flight `__anext__` is awaited
        # to completion first. Both awaits are best-effort: this block runs while
        # a disconnect, a cancellation or a provider failure is already in
        # progress, and none of them should be replaced by a cleanup error.
        if pending is not None:
            pending.cancel()
            with contextlib.suppress(BaseException):
                await pending
        with contextlib.suppress(BaseException):
            await events.aclose()

    await _persist(persistence, state)


def _apply(state: _Turn, event: routing.RouteStreamEvent) -> list[bytes]:
    """Fold one router event into the turn, and say what goes on the wire."""
    if isinstance(event, routing.AttemptStarted):
        state.begin(event)
        return []

    if isinstance(event, routing.AttemptAborted):
        state.abort(event)
        return []

    if isinstance(event, routing.StreamCompleted):
        state.complete(event)
        return [sse.format_event(state.done_event())]

    frames: list[bytes] = []
    if not state.committed:
        # First byte of the response, and the point of no return: after this the
        # error envelope is unavailable and every failure is in-band.
        frames.append(sse.format_event(state.meta_event()))
        state.committed = True
        state.pending_restart = None
    elif state.pending_restart is not None:
        # Deferred until now on purpose. A restart names the candidate that is
        # *serving*, and the attempt that started may itself have failed before
        # producing anything — announcing it at the attempt boundary would name a
        # model that never spoke.
        frames.append(sse.format_event(state.restart_event()))
        state.pending_restart = None

    state.buffer.append(event.text)
    frames.append(sse.format_event(sse.DeltaEvent.of(event.text)))
    return frames


class _Turn:
    """The mutable half of the state machine, kept out of the loop above.

    Separated so the rules are readable as rules — what is buffered, what is
    discarded, what has been committed — rather than as assignments interleaved
    with I/O. Everything here is pure; nothing awaits.
    """

    conversation_id: UUID
    message_id: UUID
    requested: str
    pinned: str | None
    """``conversations.pinned_model`` as it stood before this turn (D3/D32) —
    threaded straight through from :func:`stream_completion`'s own ``pinned``
    argument, which already decided the candidate chain via
    ``selection.candidates(pinned=...)``. Carried here only so
    :meth:`done_event` can build the same disclosure the non-streaming path
    builds, off the same fact routing already used."""

    buffer: list[str]
    """The current attempt's text. **Cleared on every restart**, never spliced:
    half an answer from one model welded to a full answer from another reads as a
    broken model rather than as a client bug, and is miserable to diagnose."""

    longest_partial: str
    """The furthest any discarded attempt got. What ``partial_content`` offers."""

    committed: bool
    done: bool
    attempt: int
    attempts: int
    spec: ModelSpec | None
    pending_restart: tuple[ModelSpec, str, int] | None
    """``(failed spec, reason, discarded_chars)``, waiting for the next attempt to
    prove itself by producing a delta."""

    status: Literal["ok", "failed"]
    error_code: str | None
    usage: Usage
    finish_reason: str
    trail: tuple[routing.AttemptRecord, ...]
    latency_ms: int
    ttft_ms: int | None
    wasted_tokens_out: int
    degraded: bool
    extraction_tier: ExtractionTier | None
    messages_dropped: int

    def __init__(
        self,
        *,
        conversation_id: UUID,
        message_id: UUID,
        requested: str,
        pinned: str | None = None,
    ) -> None:
        self.conversation_id = conversation_id
        self.message_id = message_id
        self.requested = requested
        self.pinned = pinned
        self.buffer = []
        self.longest_partial = ""
        self.committed = False
        self.done = False
        self.attempt = 0
        self.attempts = 0
        self.spec = None
        self.pending_restart = None
        self.status = "ok"
        self.error_code = None
        self.usage = Usage(tokens_in=0, tokens_out=0, estimated=True)
        self.finish_reason = "error"
        self.trail = ()
        self.latency_ms = 0
        self.ttft_ms = None
        self.wasted_tokens_out = 0
        self.degraded = False
        self.extraction_tier = None
        self.messages_dropped = 0

    # ---- transitions ------------------------------------------------------ #
    def begin(self, event: routing.AttemptStarted) -> None:
        self.attempt = event.attempt
        self.attempts = event.attempt
        self.spec = event.spec

    def abort(self, event: routing.AttemptAborted) -> None:
        text = "".join(self.buffer)
        if len(text) > len(self.longest_partial):
            self.longest_partial = text
        if text:
            self.pending_restart = (event.spec, event.error.code, event.discarded_chars)
        self.buffer.clear()

    def complete(self, event: routing.StreamCompleted) -> None:
        self.spec = event.spec
        self.status = "ok"
        self.usage = event.usage
        self.finish_reason = event.finish_reason
        self.trail = event.trail
        self.attempts = event.attempts
        self.latency_ms = event.latency_ms
        self.ttft_ms = event.ttft_ms
        self.wasted_tokens_out = event.wasted_tokens_out
        self.degraded = event.report.degraded
        self.extraction_tier = event.report.extraction_tier
        self.messages_dropped = event.report.messages_dropped
        self.done = True

    def record_failure(self, failure: routing.RoutingFailed) -> None:
        self.status = "failed"
        self.error_code = failure.error.code
        self.spec = failure.spec if failure.spec is not None else self.spec
        self.trail = failure.trail
        self.attempts = failure.attempts
        self.latency_ms = failure.latency_ms
        self.wasted_tokens_out = failure.wasted_tokens_out
        # Whatever the last attempt had managed before it died is still the
        # furthest anything got, and `abort` only sees a buffer it is clearing.
        text = "".join(self.buffer)
        if len(text) > len(self.longest_partial):
            self.longest_partial = text
        self.done = True

    # ---- events ----------------------------------------------------------- #
    def meta_event(self) -> sse.MetaEvent:
        spec = self._served()
        return sse.MetaEvent(
            attempt=self.attempt,
            slot=spec.slot,
            provider=spec.provider,
            model=spec.model,
            requested_slot=self.requested,
            conversation_id=str(self.conversation_id),
            message_id=str(self.message_id),
        )

    def restart_event(self) -> sse.RestartEvent:
        assert self.pending_restart is not None
        failed, reason, discarded = self.pending_restart
        spec = self._served()
        return sse.RestartEvent.of(
            reason=reason,
            failed_provider=failed.provider,
            failed_model=failed.model,
            next_slot=spec.slot,
            next_provider=spec.provider,
            next_model=spec.model,
            attempt=self.attempt,
            discarded_chars=discarded,
        )

    def done_event(self) -> sse.DoneEvent:
        spec = self._served()
        return sse.DoneEvent(
            served_by=ServedBy(slot=spec.slot, provider=spec.provider, model=spec.model),
            requested_slot=self.requested,
            substituted=selection.is_substitution(self.requested, spec.slot),
            attempts=self.attempts,
            usage=sse.StreamUsage(
                prompt_tokens=self.usage.tokens_in,
                completion_tokens=self.usage.tokens_out,
                total_tokens=self.usage.tokens_in + self.usage.tokens_out,
                estimated=self.usage.estimated,
                wasted_tokens_out=self.wasted_tokens_out,
            ),
            degraded=self.degraded,
            extraction_tier=self.extraction_tier,
            messages_dropped=self.messages_dropped,
            warning=selection.pin_warning(self.pinned, self.requested, spec.slot),
            status=self.status,
            partial_content=self.partial_content(),
        )

    def partial_content(self) -> str | None:
        """The salvage offer, or nothing. Only ever set on a failed stream."""
        if self.status != "failed":
            return None
        if len(self.longest_partial) < MIN_PARTIAL_CONTENT_CHARS:
            return None
        return self.longest_partial

    def result(self) -> StreamResult:
        spec = self.spec
        return StreamResult(
            conversation_id=self.conversation_id,
            message_id=self.message_id,
            requested_slot=self.requested,
            spec=spec,
            text="".join(self.buffer) if self.status == "ok" else self.longest_partial,
            usage=self.usage,
            finish_reason=self.finish_reason,
            status=self.status,
            error_code=self.error_code,
            substituted=(spec is not None and selection.is_substitution(self.requested, spec.slot)),
            attempts=self.attempts,
            trail=self.trail,
            latency_ms=self.latency_ms,
            ttft_ms=self.ttft_ms,
            wasted_tokens_out=self.wasted_tokens_out,
            degraded=self.degraded,
            extraction_tier=self.extraction_tier,
            messages_dropped=self.messages_dropped,
        )

    def _served(self) -> ModelSpec:
        """The candidate the client is being told about.

        Never ``None`` at any call site: ``meta`` and ``restart`` are only reached
        after an attempt started, and ``done`` is only reached after one started
        or after a failure that carries the last one. The single case with no spec
        at all — every candidate skipped on an open breaker — never commits, so it
        leaves through the error envelope instead.
        """
        assert self.spec is not None
        return self.spec


# --------------------------------------------------------------------------- #
# Side effects, both of which are allowed to fail
# --------------------------------------------------------------------------- #
async def record_attempt(redis: Redis | None, message_id: UUID) -> None:
    """Bump ``stream:{message_id}:attempts`` — written, never read (ADR-015).

    The in-process loop counter is authoritative: one request is served by one
    process, and a distributed counter cannot make a decision a local variable
    cannot make faster and more correctly. This key exists so that restarts are
    *visible* in Redis while they are happening, and as the seam a future
    multi-instance retry-resume would need. Failing to write it costs
    observability and nothing else, so it never raises.
    """
    if redis is None:
        return
    key = keys.stream_attempts(message_id)
    try:
        await redis.incr(key)
        await redis.expire(key, keys.STREAM_ATTEMPTS_TTL_S)
    except Exception as exc:
        logger.warning(
            "stream.attempt_counter_failed",
            message_id=str(message_id),
            error=str(exc),
            error_type=type(exc).__name__,
        )


async def _persist(persistence: StreamPersistence | None, state: _Turn) -> None:
    """Hand the finished turn to the collector, and swallow whatever it does.

    D14: the user already has their answer, and there is no response left to turn
    a raised exception into. The honest cost is that the message on screen will
    not survive a refresh — worse than persisting, better than a traceback
    attached to a request that visibly succeeded, and visible in the logs rather
    than silent.
    """
    if persistence is None or not state.done:
        return
    try:
        await persistence.persist(state.result())
    except Exception:
        logger.exception(
            "stream.persist_failed",
            message_id=str(state.message_id),
            conversation_id=str(state.conversation_id),
            attempts=state.attempts,
        )


async def _client_gone(is_disconnected: Callable[[], Awaitable[bool]] | None) -> bool:
    """Has the client left? A question, never a fault.

    Its own failure is not one either: a disconnect check that raises must not
    take down a stream that is otherwise working, so an error here reads as
    "still connected" and lets the normal timeouts end the request.
    """
    if is_disconnected is None:
        return False
    try:
        return await is_disconnected()
    except Exception:  # pragma: no cover — defensive
        return False


__all__ = [
    "MIN_PARTIAL_CONTENT_CHARS",
    "CacheReplayPersistence",
    "StreamPersistence",
    "StreamResult",
    "record_attempt",
    "stream_cached_completion",
    "stream_completion",
]
