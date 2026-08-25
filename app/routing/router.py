"""The failover loop: walk the candidate chain, and know when to stop.

One entry point serves both the streaming and non-streaming paths, because the
alternative is two copies of the attempt bookkeeping that drift — and the trail
this module produces is the only record that discarded attempts ever happened.
One ``messages`` row per logical message means a candidate that 429'd leaves no
other trace anywhere in the system.

**The whole game is the second decision.** The first is easy: ask the breaker,
render, send. The second is what to do with a failure, and it is decided entirely
by the *kind* of failure — a rate limit means "try someone else", a malformed
payload means "stop, this fails identically everywhere, and trying it three more
times just wastes three more seconds and burns quota doing it". That distinction
lives in Contract A's class-level flags (``retryable_same_provider`` /
``failover_eligible`` / ``breaker_eligible``), never in an ``isinstance`` ladder
and never in ``adapter.name``: if this module needed to know which provider it was
talking to, something that belongs behind the interface has leaked.

**Two counters, and they are not the same number.** ``RouterOutcome.attempts``
counts requests that left the process, and it is what caps at three (D12).
``RouterOutcome.trail`` records events, including candidates skipped because their
breaker was open — those cost no round trip and consume no attempt, which is why
the candidate list is never truncated to the cap. Three open breakers at the head
of the chain would otherwise exhaust it without a single request being made while
healthy providers sat at position four.

**The attempt budget is one budget (ADR-015).** Three upstream calls, whether they
went to three candidates or twice to one. A same-provider retry therefore *yields*:
it is skipped when only one attempt remains and an untried candidate exists, so a
single flaky provider can never spend the whole budget on itself and starve the
failover this module exists to perform.

No session, no commit, no HTTP concerns. The endpoint (Step 5) persists what comes
back; the orchestrator (Step 9) drives the streaming twin. Both get the same
bookkeeping because both come through here.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Final, Literal
from uuid import uuid4

from app.cache import keys
from app.core.clock import SYSTEM_CLOCK, Clock
from app.core.logging import get_logger
from app.keys_resolution.resolver import ProviderCredentials, ResolvedKey, SystemCredentials
from app.memory.canonical import CanonicalMessage
from app.memory.render import AttachmentResolver, RenderReport, render
from app.providers.base import (
    DEFAULT_FIRST_TOKEN_TIMEOUT_S,
    DEFAULT_IDLE_TIMEOUT_S,
    DEFAULT_READ_TIMEOUT_S,
    take_hint,
)
from app.providers.errors import (
    ContextTooLong,
    EmptyResponse,
    ProviderError,
    RateLimited,
    Unavailable,
)
from app.providers.registry import ProviderRegistry
from app.providers.types import Completion, GenParams, ModelSpec, Usage
from app.quota import allocations
from app.quota.tracker import QuotaTracker, Reservation, WindowGrant
from app.routing import selection
from app.routing.circuit_breaker import BreakerState, CircuitBreaker
from app.usage.metrics import COMPLETE, STREAM, LatencyTable

logger = get_logger("app.routing.router")

MAX_ATTEMPTS: Final = 3
"""D1's cap, applied to both paths so they behave identically.

In-process and authoritative: one request is served by one process, and a
distributed counter cannot make a decision a local variable cannot make faster and
more correctly. Step 9 writes ``stream:{message_id}:attempts`` for observability
and deliberately does not read it (ADR-015).
"""

MAX_SAME_PROVIDER_RETRIES: Final = 1
"""Retries against the *same* candidate, for ``retryable_same_provider`` errors.

Hand-rolled rather than ``tenacity`` (ADR-013): it is a dozen lines, and a
decorator fights the async-generator streaming path Step 9 adds. One rather than
two, because the retries share the attempt budget with failover — see ADR-015.
``RateLimited`` is excluded by its own flag, and that is not a tuning choice:
hammering a 429 is how a free-tier key gets banned.
"""

RETRY_BASE_DELAY_S: Final = 0.25
RETRY_MAX_DELAY_S: Final = 2.0

DISCARDED_CHARS_PER_TOKEN: Final = 4
"""Characters-over-four, for the tokens a *discarded* streamed attempt generated.

Not ``adapter.estimate_tokens``, which measures a request payload rather than a
response, and not the provider's own count, because providers almost never
report usage on a stream they never finished. So this is the only number
available — and it has to exist, because those tokens were really generated and
really charged against the free tier's daily budget. Dropping them makes Phase
3's tracker wrong in exactly the scenario it exists for, and the miscount is
invisible until a key rate-limits earlier than predicted. Marked
``estimated=True`` wherever it surfaces; ADR-014's reconciliation cannot recover
it after the fact.
"""

type AttemptOutcome = Literal["ok", "error", "skipped_breaker", "skipped_quota"]


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """One event in the trail written to ``requests.attempts``.

    This array is the answer to "why did this answer look weird?", and it is the
    only place that answer will ever exist.

    ``n`` numbers *events*, not attempts: a skipped candidate takes an ``n`` and
    leaves ``RouterOutcome.attempts`` alone, so a trail can legitimately be longer
    than the cap of three.
    """

    n: int
    slot: str
    provider: str
    model: str
    outcome: AttemptOutcome
    error_code: str | None = None
    latency_ms: int = 0
    wasted_tokens_out: int = 0
    """Tokens a discarded attempt really generated. Always 0 on the non-streaming
    path — a failed ``complete`` produced nothing — and populated by Step 9, where
    a restart throws away text the free tier already charged for."""

    breaker: BreakerState = "closed"
    retry_after_s: float | None = None
    """On a skip of either kind: how long until this candidate is worth trying
    again — the breaker's cooldown, or D17's blocked window's reset."""

    blocked_window: str | None = None
    """Only on ``skipped_quota``: which window (``rpm``/``rpd``/``tpm``/``tpd``)
    said no. ``None`` on every other outcome, breaker skips included — a breaker
    skip has no window, it has a cooldown."""

    key_pool: str | None = None
    """Which pool the credential resolved for this candidate came from
    (D42) — ``None`` only on ``skipped_breaker``, where no candidate is
    resolved because the breaker question is asked first. A trail that failed
    over from a private key to the shared pool is exactly the row this field
    exists for."""

    def to_json(self) -> dict[str, Any]:
        """The JSONB shape. Stable — Phase 7's dashboard reads it."""
        record: dict[str, Any] = {
            "n": self.n,
            "slot": self.slot,
            "provider": self.provider,
            "model": self.model,
            "outcome": self.outcome,
            "error_code": self.error_code,
            "latency_ms": self.latency_ms,
            "wasted_tokens_out": self.wasted_tokens_out,
            "breaker": self.breaker,
        }
        if self.retry_after_s is not None:
            record["retry_after_s"] = round(self.retry_after_s, 3)
        if self.blocked_window is not None:
            record["blocked_window"] = self.blocked_window
        if self.key_pool is not None:
            record["key_pool"] = self.key_pool
        return record


@dataclass(frozen=True, slots=True)
class RouterOutcome:
    """What the loop came back with, and everything it did on the way."""

    spec: ModelSpec
    """The candidate that actually served. Not necessarily the one asked for —
    that difference is what ``substituted`` discloses."""

    completion: Completion
    report: RenderReport

    trail: tuple[AttemptRecord, ...]
    """Every event, skips included. Serialized into ``requests.attempts``."""

    attempts: int
    """Requests that left the process. At most :data:`MAX_ATTEMPTS`. This is what
    ``MessageMeta.attempts`` and the response body carry."""

    latency_ms: int
    """Wall time across the whole loop, including retries and abandoned
    candidates — what the user waited, not what the winning attempt took."""

    key_pool: Literal["shared", "private"]
    """The *winning* attempt's pool (D42) — which credential actually answered."""


# --------------------------------------------------------------------------- #
# What the streaming loop yields
# --------------------------------------------------------------------------- #
# Deltas alone would not do. `router.py`'s job on the streaming path is the same
# bookkeeping as on the other one — which candidate, which attempt, what was
# thrown away — and an orchestrator that had to infer attempt boundaries from a
# gap in the deltas would end up keeping a second copy of it. These four events
# are that bookkeeping made explicit, and they are deliberately *not* SSE: the
# wire format is `streaming/sse.py`'s, and this module knows nothing about HTTP.
@dataclass(frozen=True, slots=True)
class AttemptStarted:
    """A request is about to leave the process. Nothing has been received yet.

    Not "something will be streamed" — this attempt may fail before its first
    chunk, which is the ordinary case D13 keeps off the wire entirely. An
    orchestrator must therefore treat this as bookkeeping, never as a cue to
    emit anything to a client.
    """

    attempt: int
    spec: ModelSpec


@dataclass(frozen=True, slots=True)
class AttemptDelta:
    """One piece of generated text, from the attempt most recently started."""

    text: str


@dataclass(frozen=True, slots=True)
class AttemptAborted:
    """An attempt failed. Emitted whether or not it had produced any text.

    ``discarded_chars`` is the difference that matters: zero means nothing ever
    reached the client and the failover is invisible (D13), non-zero means a
    partial answer is on screen and has to be explicitly withdrawn (D1).
    """

    attempt: int
    spec: ModelSpec
    error: ProviderError
    discarded_chars: int
    wasted_tokens_out: int


@dataclass(frozen=True, slots=True)
class StreamCompleted:
    """The terminal event of a successful stream, and the loop's whole report.

    An async generator cannot return a value, so everything :class:`RouterOutcome`
    carries on the non-streaming path arrives here instead — including the trail,
    which is the only surviving record of the attempts that were discarded.
    """

    spec: ModelSpec
    usage: Usage
    finish_reason: str
    report: RenderReport

    trail: tuple[AttemptRecord, ...]
    attempts: int
    latency_ms: int

    ttft_ms: int
    """Time to the *serving* attempt's first token. With streaming this is the
    number that characterizes a provider; ``latency_ms`` keeps meaning total wall
    time, which is dominated by how much the model chose to say."""

    wasted_tokens_out: int
    """Summed across every discarded attempt, not just the last one."""

    key_pool: Literal["shared", "private"]
    """The *serving* attempt's pool (D42) — mirrors :attr:`RouterOutcome.key_pool`."""


type RouteStreamEvent = AttemptStarted | AttemptDelta | AttemptAborted | StreamCompleted


class RoutingFailed(Exception):
    """No candidate served the request. Carries the trail, not just the error.

    **Why a wrapper rather than the bare ``ProviderError``.** The endpoint writes a
    ``requests`` row on the failure path too, and a failure that walked three
    providers is the single row where "what did it try?" is the entire question.
    A normalized error names one provider and one model; it cannot say that two
    others were skipped on an open breaker first. Raising it alone would leave
    ``requests.attempts`` empty on exactly the requests it exists to explain.

    **Deliberately not a ``ProviderError`` subclass.** It is a routing outcome, not
    a provider's answer, and making it one would let it slip through an
    ``except ProviderError`` written to mean something narrower — including the
    router's own, which is how a failure would end up re-entering the loop that
    produced it.

    ``error`` stays untranslated so the endpoint hands it to the existing
    ``to_app_error``, which needs no knowledge of this class.
    """

    def __init__(
        self,
        error: ProviderError,
        *,
        spec: ModelSpec | None,
        trail: tuple[AttemptRecord, ...],
        attempts: int,
        latency_ms: int,
        wasted_tokens_out: int = 0,
        key_pool: Literal["shared", "private"] | None = None,
    ) -> None:
        self.error = error
        self.spec = spec
        """The last candidate attempted, or ``None`` when every one was skipped.
        The error names the provider and model; only this names the *slot*."""

        self.trail = trail
        self.attempts = attempts
        self.latency_ms = latency_ms

        self.wasted_tokens_out = wasted_tokens_out
        """Tokens the discarded streamed attempts really generated. Always 0 on
        the non-streaming path — a failed ``complete`` produced nothing — and the
        reason this failure still has a token cost worth recording."""

        self.key_pool = key_pool
        """The pool the *last attempted* candidate resolved to (D42), or
        ``None`` when every candidate was skipped and nothing ever resolved.
        A failed stream still has to report which pool served its last real
        attempt — never a stale value carried over from an earlier one."""

        super().__init__(str(error))


async def route(
    *,
    registry: ProviderRegistry,
    breaker: CircuitBreaker,
    history: list[CanonicalMessage],
    params: GenParams,
    requested: str,
    pinned: str | None = None,
    metrics: LatencyTable | None = None,
    rank_by_latency: bool = True,
    resolver: AttachmentResolver | None = None,
    timeout_s: float = DEFAULT_READ_TIMEOUT_S,
    max_attempts: int = MAX_ATTEMPTS,
    quota: QuotaTracker | None = None,
    credentials: ProviderCredentials | None = None,
    clock: Clock = SYSTEM_CLOCK,
    rng: random.Random | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> RouterOutcome:
    """Serve one non-streaming turn, failing over as the errors permit.

    Raises :class:`RoutingFailed` when every candidate is spent, or on the first
    error that is neither retryable nor failover-eligible. The normalized
    :class:`~app.providers.errors.ProviderError` is on ``.error``, untranslated:
    the endpoint hands it to the existing ``to_app_error``, which knows nothing
    about this module.

    ``breaker`` is passed in rather than built here so the caller owns the Redis
    handle and a test can hand over a ``fakeredis``-backed one. ``metrics`` being
    ``None`` disables both ranking and recording, which is what makes a unit test's
    ordering deterministic; ``rank_by_latency`` is where
    ``Settings.ROUTING_LATENCY_RANKING`` lands, so this module never reads
    settings. ``rng`` and ``sleep`` are injected so retry jitter is deterministic
    and tests do not actually wait.

    ``quota`` being ``None`` disables reservation entirely — the same shape as
    ``metrics=None`` — which is Step 3's ``QUOTA_ENFORCEMENT`` kill switch (D15)
    reaching this module without it having to read settings either.

    ``credentials`` answers D36's one question — which key, whose quota scope —
    per *candidate*, not per request (§9.5): a single failover chain can cross a
    provider the caller holds their own key for and one where they still ride
    the shared pool. ``None`` defaults to :class:`~app.keys_resolution.resolver
    .SystemCredentials`, which resolves every provider to the environment's key
    under the shared-pool scope — exactly today's behaviour, which is what
    keeps every pre-Phase-6 test passing unchanged.
    """
    credentials = credentials or SystemCredentials(registry)
    snapshot = metrics.snapshot(COMPLETE) if metrics is not None and rank_by_latency else None
    chain = selection.candidates(registry, requested, pinned=pinned, latency=snapshot)

    jitter = rng if rng is not None else random.Random()
    started = clock.now()
    trail: list[AttemptRecord] = []
    attempts = 0
    last_error: ProviderError | None = None
    # The error names a provider and a model; only the spec names the slot, which
    # is what `requests.served_slot` records on the failure path.
    last_spec: ModelSpec | None = None
    last_key_pool: Literal["shared", "private"] | None = None

    for position, candidate in enumerate(chain):
        if attempts >= max_attempts:
            break

        decision = await breaker.allows(candidate.provider, candidate.model)
        if not decision.allowed:
            trail.append(
                AttemptRecord(
                    n=len(trail) + 1,
                    slot=candidate.slot,
                    provider=candidate.provider,
                    model=candidate.model,
                    outcome="skipped_breaker",
                    breaker=decision.state,
                    retry_after_s=decision.retry_after_s,
                )
            )
            logger.info(
                "router.candidate_skipped",
                provider=candidate.provider,
                model=candidate.model,
                slot=candidate.slot,
                breaker=decision.state,
                retry_after_s=decision.retry_after_s,
            )
            continue

        # `spec` diverges from `candidate` only when a ContextTooLong re-fit
        # narrows the window; everything user-visible about them is identical.
        spec = candidate
        refit_used = False
        retries_used = 0

        while True:
            attempt_started = clock.now()
            adapter = registry.adapter_for_spec(spec)
            resolved = await credentials.for_provider(spec.provider, spec.model)
            reservation: Reservation | None = None

            try:
                payload, report = await render(history, spec, params, adapter, resolver=resolver)

                # D17: the reservation *is* the check, made after render because
                # the only trustworthy token estimate is the one render just
                # measured. A render failure above never reaches here, so it
                # never reserves and never counts as an attempt (below) —
                # exactly the "attempts: 0" behaviour D17 calls out by name.
                if quota is not None:
                    quota_decision = await quota.reserve(
                        spec,
                        scope=resolved.scope,
                        estimated_tokens=report.estimated_tokens,
                        request_id=str(uuid4()),
                        # D39: the shared path's own extra ceiling, reserved
                        # atomically alongside the model's windows.
                        extra_grants=_extra_grants(spec, resolved),
                    )
                    if not quota_decision.allowed:
                        trail.append(
                            AttemptRecord(
                                n=len(trail) + 1,
                                slot=spec.slot,
                                provider=spec.provider,
                                model=spec.model,
                                outcome="skipped_quota",
                                breaker=decision.state,
                                retry_after_s=quota_decision.retry_after_s,
                                blocked_window=quota_decision.blocked_window,
                                key_pool=resolved.pool,
                            )
                        )
                        logger.info(
                            "router.candidate_skipped_quota",
                            provider=spec.provider,
                            model=spec.model,
                            slot=spec.slot,
                            blocked_window=quota_decision.blocked_window,
                            retry_after_s=quota_decision.retry_after_s,
                            degraded=quota_decision.degraded,
                        )
                        # No round trip, no attempt spent (D17, trap 8) — straight
                        # to the next candidate. Retrying the same one is pointless:
                        # the window it just failed is still the window it has.
                        break
                    reservation = quota_decision.reservation

                attempts += 1
                completion = await adapter.complete(payload, resolved.key, timeout=timeout_s)
            except ProviderError as exc:
                latency_ms = _elapsed_ms(clock, attempt_started)
                if quota is not None and reservation is not None:
                    # The provider counted the request the moment it left the
                    # process, and it stays counted no matter what happens next
                    # (trap 6) — only the token estimate corrects, down to what
                    # was really generated: nothing, on this path.
                    await quota.commit(reservation, tokens_in=0, tokens_out=0)
                await _reconcile_hint(quota, spec, scope=resolved.scope)
                last_error = exc
                last_spec = spec
                last_key_pool = resolved.pool
                trail.append(
                    AttemptRecord(
                        n=len(trail) + 1,
                        slot=spec.slot,
                        provider=spec.provider,
                        model=spec.model,
                        outcome="error",
                        error_code=exc.code,
                        latency_ms=latency_ms,
                        breaker=decision.state,
                        key_pool=resolved.pool,
                    )
                )
                logger.warning(
                    "router.attempt_failed",
                    **exc.log_fields(),
                    slot=spec.slot,
                    attempt=attempts,
                    latency_ms=latency_ms,
                    detail=str(exc),
                )

                # --- The one special case: a failure with a fix ---------------
                # `isinstance` here reads `limit_tokens`, which only this class
                # declares; it is not a routing branch. The routing decision
                # below is still made on the flags.
                retryable = exc.retryable_same_provider
                if isinstance(exc, ContextTooLong):
                    if not refit_used and exc.limit_tokens is not None and attempts < max_attempts:
                        # Honest substitution: the model's real window is smaller
                        # than the config claimed, so re-fit against the truth.
                        spec = replace(spec, context_window=exc.limit_tokens)
                        refit_used = True
                        logger.info(
                            "router.refitting",
                            provider=spec.provider,
                            model=spec.model,
                            limit_tokens=exc.limit_tokens,
                        )
                        continue
                    # Out of re-fits. The class is retryable *because* of the
                    # re-fit; without one, a retry re-sends the identical payload
                    # and gets the identical error. It is not failover-eligible
                    # either — a history that overflowed here does not fit better
                    # on a candidate whose window is usually smaller — so this
                    # falls through to the abort path, which is correct.
                    retryable = False

                # --- Retry the same candidate? -------------------------------
                if retryable and retries_used < MAX_SAME_PROVIDER_RETRIES:
                    if _retry_would_starve_failover(
                        attempts=attempts,
                        max_attempts=max_attempts,
                        candidates_remaining=len(chain) - position - 1,
                    ):
                        # Yield the last attempt to a provider that has not
                        # already failed. ADR-015.
                        logger.info(
                            "router.retry_yielded",
                            provider=spec.provider,
                            model=spec.model,
                            attempts=attempts,
                        )
                    else:
                        retries_used += 1
                        await sleep(_retry_delay_s(retries_used, jitter))
                        continue

                # --- Fail over, or stop --------------------------------------
                await breaker.record_failure(decision, exc)
                if exc.failover_eligible:
                    break

                logger.info("router.aborted", error_code=exc.code, attempts=attempts)
                raise RoutingFailed(
                    exc,
                    spec=spec,
                    trail=tuple(trail),
                    attempts=attempts,
                    latency_ms=_elapsed_ms(clock, started),
                    key_pool=resolved.pool,
                ) from exc
            else:
                latency_ms = _elapsed_ms(clock, attempt_started)
                if quota is not None and reservation is not None:
                    # Correct the estimate to the truth, in either direction.
                    await quota.commit(
                        reservation,
                        tokens_in=completion.usage.tokens_in,
                        tokens_out=completion.usage.tokens_out,
                    )
                await _reconcile_hint(quota, spec, scope=resolved.scope)
                await breaker.record_success(decision)
                if metrics is not None:
                    # Successful attempts only — a provider that 429s in 80ms is
                    # the fastest thing in the fleet and the worst choice. See
                    # `usage/metrics.py`.
                    metrics.record(spec.provider, spec.model, COMPLETE, latency_ms)

                trail.append(
                    AttemptRecord(
                        n=len(trail) + 1,
                        slot=spec.slot,
                        provider=spec.provider,
                        model=spec.model,
                        outcome="ok",
                        latency_ms=latency_ms,
                        breaker=decision.state,
                        key_pool=resolved.pool,
                    )
                )
                logger.info(
                    "router.served",
                    provider=spec.provider,
                    model=spec.model,
                    slot=spec.slot,
                    requested_slot=requested,
                    attempts=attempts,
                    latency_ms=latency_ms,
                )
                return RouterOutcome(
                    spec=spec,
                    completion=completion,
                    report=report,
                    trail=tuple(trail),
                    attempts=attempts,
                    latency_ms=_elapsed_ms(clock, started),
                    key_pool=resolved.pool,
                )

    logger.warning(
        "router.exhausted",
        requested_slot=requested,
        attempts=attempts,
        candidates=len(chain),
        error_code=last_error.code if last_error is not None else None,
    )
    raise RoutingFailed(
        last_error if last_error is not None else _all_skipped(chain, tuple(trail)),
        spec=last_spec,
        trail=tuple(trail),
        attempts=attempts,
        latency_ms=_elapsed_ms(clock, started),
        key_pool=last_key_pool,
    )


async def route_stream(
    *,
    registry: ProviderRegistry,
    breaker: CircuitBreaker,
    history: list[CanonicalMessage],
    params: GenParams,
    requested: str,
    pinned: str | None = None,
    metrics: LatencyTable | None = None,
    rank_by_latency: bool = True,
    resolver: AttachmentResolver | None = None,
    timeout_s: float = DEFAULT_READ_TIMEOUT_S,
    idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
    first_token_timeout_s: float = DEFAULT_FIRST_TOKEN_TIMEOUT_S,
    max_attempts: int = MAX_ATTEMPTS,
    quota: QuotaTracker | None = None,
    credentials: ProviderCredentials | None = None,
    clock: Clock = SYSTEM_CLOCK,
    rng: random.Random | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncGenerator[RouteStreamEvent, None]:
    """The streaming twin of :func:`route`, and D1's restart machinery underneath.

    Typed as an ``AsyncGenerator`` rather than the ``AsyncIterator`` the seam
    declared, because the orchestrator needs ``aclose()``: a client that leaves
    mid-generation has to close this loop deterministically rather than when the
    garbage collector notices, and only the generator type promises that.

    Same candidate chain, same breaker questions, same flag-driven decisions,
    same one budget of three. What differs is that a failure can now arrive
    *after* text has already been generated, and three rules follow from that:

    **A stream that yielded nothing is an** :class:`~app.providers.errors.EmptyResponse`.
    A 200 that ends without a delta is the streaming form of the 200-with-nothing
    free tiers produce all day, and it has to normalize to the same class or
    invariant 4 ("an empty generation is an error, not a stored message") would
    be enforced on one path and not the other.

    **The first token gets its own, tighter budget** (D13,
    :data:`~app.providers.base.DEFAULT_FIRST_TOKEN_TIMEOUT_S`). It is enforced
    here rather than inside ``stream`` because only the loop knows the difference
    between "the first gap" and "a gap", and only the loop can act on it.

    **Once a delta has been delivered, the same candidate is never retried.** It
    has demonstrated that it accepts a connection and then dies; a second go at
    it spends an attempt from the same budget as a candidate that has not failed
    at all. A restart is for a *new* provider (D1) — the same-provider retry
    stays where it is genuinely a retry, before anything was streamed.

    Records the TTFT series (:data:`~app.usage.metrics.STREAM`), not the total-
    latency one, and only for an attempt that ran to completion: an attempt that
    produced one token and then died is not evidence that the provider is fast.

    Raises :class:`RoutingFailed` exactly as :func:`route` does. Whether that
    reaches the client as a JSON envelope or as an in-band ``done`` is D13's
    question and the orchestrator's to answer — this loop does not know that a
    client exists.

    **The reservation is per attempt, same as** :func:`route`. A restart makes a
    fresh one; a mid-stream abort commits the tokens it really generated (D1 step
    4) rather than losing them, which is Step 5's whole reason for existing on
    this path.

    ``credentials`` is D36's per-candidate resolver, exactly as :func:`route`
    takes it — ``None`` defaults to :class:`~app.keys_resolution.resolver
    .SystemCredentials`.
    """
    credentials = credentials or SystemCredentials(registry)
    snapshot = metrics.snapshot(STREAM) if metrics is not None and rank_by_latency else None
    chain = selection.candidates(registry, requested, pinned=pinned, latency=snapshot)

    jitter = rng if rng is not None else random.Random()
    started = clock.now()
    trail: list[AttemptRecord] = []
    attempts = 0
    wasted_total = 0
    last_error: ProviderError | None = None
    last_spec: ModelSpec | None = None
    last_key_pool: Literal["shared", "private"] | None = None

    for position, candidate in enumerate(chain):
        if attempts >= max_attempts:
            break

        decision = await breaker.allows(candidate.provider, candidate.model)
        if not decision.allowed:
            trail.append(
                AttemptRecord(
                    n=len(trail) + 1,
                    slot=candidate.slot,
                    provider=candidate.provider,
                    model=candidate.model,
                    outcome="skipped_breaker",
                    breaker=decision.state,
                    retry_after_s=decision.retry_after_s,
                )
            )
            logger.info(
                "router.candidate_skipped",
                provider=candidate.provider,
                model=candidate.model,
                slot=candidate.slot,
                breaker=decision.state,
                retry_after_s=decision.retry_after_s,
            )
            continue

        spec = candidate
        refit_used = False
        retries_used = 0

        while True:
            attempt_started = clock.now()
            adapter = registry.adapter_for_spec(spec)
            resolved = await credentials.for_provider(spec.provider, spec.model)
            reservation: Reservation | None = None

            delivered_chars = 0
            ttft_ms: int | None = None
            usage: Usage | None = None
            finish_reason = "stop"

            try:
                payload, report = await render(history, spec, params, adapter, resolver=resolver)

                # Same ordering as `route` (D17): render first for the token
                # estimate, reserve second, and only a granted reservation counts
                # as an attempt. A render failure below never reaches this block.
                if quota is not None:
                    quota_decision = await quota.reserve(
                        spec,
                        scope=resolved.scope,
                        estimated_tokens=report.estimated_tokens,
                        request_id=str(uuid4()),
                        extra_grants=_extra_grants(spec, resolved),
                    )
                    if not quota_decision.allowed:
                        trail.append(
                            AttemptRecord(
                                n=len(trail) + 1,
                                slot=spec.slot,
                                provider=spec.provider,
                                model=spec.model,
                                outcome="skipped_quota",
                                breaker=decision.state,
                                retry_after_s=quota_decision.retry_after_s,
                                blocked_window=quota_decision.blocked_window,
                                key_pool=resolved.pool,
                            )
                        )
                        logger.info(
                            "router.candidate_skipped_quota",
                            provider=spec.provider,
                            model=spec.model,
                            slot=spec.slot,
                            blocked_window=quota_decision.blocked_window,
                            retry_after_s=quota_decision.retry_after_s,
                            degraded=quota_decision.degraded,
                        )
                        break
                    reservation = quota_decision.reservation

                attempts += 1
                yield AttemptStarted(attempt=attempts, spec=spec)

                chunks = adapter.stream(
                    payload, resolved.key, timeout_s, idle_timeout_s
                ).__aiter__()
                try:
                    while True:
                        try:
                            if ttft_ms is None:
                                chunk = await asyncio.wait_for(
                                    chunks.__anext__(), first_token_timeout_s
                                )
                            else:
                                chunk = await chunks.__anext__()
                        except StopAsyncIteration:
                            break
                        except TimeoutError as exc:
                            # Indistinguishable, to everything above, from the
                            # idle stall `_stream_events` raises — and it should
                            # be: both mean "accepted the connection, said
                            # nothing", and both are cheap to abandon here.
                            raise Unavailable(
                                "first-token stall: no chunk from provider within the budget",
                                provider=spec.provider,
                                model=spec.model,
                            ) from exc

                        if ttft_ms is None:
                            ttft_ms = _elapsed_ms(clock, attempt_started)
                        if chunk.usage is not None:
                            usage = chunk.usage
                        if chunk.finish_reason:
                            finish_reason = chunk.finish_reason
                        if chunk.delta:
                            delivered_chars += len(chunk.delta)
                            yield AttemptDelta(text=chunk.delta)
                finally:
                    # Reached on a fault, on a client-side close, and on the happy
                    # path alike. Without it an abandoned upstream connection is
                    # returned to the pool only when the generator is collected,
                    # which on a free-tier pool is the difference between failing
                    # over and running out of sockets doing it.
                    #
                    # Guarded because Contract A promises an ``AsyncIterator``, not
                    # a generator: every adapter here happens to be one, and an
                    # adapter that is not — a test double, a local model — must not
                    # crash the loop for lacking a method it never advertised.
                    aclose = getattr(chunks, "aclose", None)
                    if aclose is not None:
                        await aclose()

                if delivered_chars == 0:
                    raise EmptyResponse(
                        "stream closed without producing any content",
                        provider=spec.provider,
                        model=spec.model,
                    )
            except ProviderError as exc:
                latency_ms = _elapsed_ms(clock, attempt_started)
                wasted = _estimate_discarded_tokens(delivered_chars)
                wasted_total += wasted
                if quota is not None and reservation is not None:
                    # Those tokens were really generated and really charged
                    # (§1.1 step 4, D17 trap 7) — zero here only when nothing was
                    # delivered before the fault, same as the non-streaming path.
                    await quota.commit(reservation, tokens_in=0, tokens_out=wasted)
                await _reconcile_hint(quota, spec, scope=resolved.scope)
                last_error = exc
                last_spec = spec
                last_key_pool = resolved.pool
                trail.append(
                    AttemptRecord(
                        n=len(trail) + 1,
                        slot=spec.slot,
                        provider=spec.provider,
                        model=spec.model,
                        outcome="error",
                        error_code=exc.code,
                        latency_ms=latency_ms,
                        wasted_tokens_out=wasted,
                        breaker=decision.state,
                        key_pool=resolved.pool,
                    )
                )
                logger.warning(
                    "router.stream_attempt_failed",
                    **exc.log_fields(),
                    slot=spec.slot,
                    attempt=attempts,
                    latency_ms=latency_ms,
                    discarded_chars=delivered_chars,
                    wasted_tokens_out=wasted,
                    detail=str(exc),
                )
                yield AttemptAborted(
                    attempt=attempts,
                    spec=spec,
                    error=exc,
                    discarded_chars=delivered_chars,
                    wasted_tokens_out=wasted,
                )

                retryable = exc.retryable_same_provider
                if isinstance(exc, ContextTooLong):
                    # Only before the first delta: mid-generation the prompt has
                    # already been accepted, so there is nothing left to re-fit
                    # and the error means something else entirely.
                    if (
                        delivered_chars == 0
                        and not refit_used
                        and exc.limit_tokens is not None
                        and attempts < max_attempts
                    ):
                        spec = replace(spec, context_window=exc.limit_tokens)
                        refit_used = True
                        logger.info(
                            "router.refitting",
                            provider=spec.provider,
                            model=spec.model,
                            limit_tokens=exc.limit_tokens,
                        )
                        continue
                    retryable = False

                if delivered_chars > 0:
                    # See the docstring: a candidate that died mid-generation has
                    # earned a restart, not a second chance.
                    retryable = False

                if retryable and retries_used < MAX_SAME_PROVIDER_RETRIES:
                    if _retry_would_starve_failover(
                        attempts=attempts,
                        max_attempts=max_attempts,
                        candidates_remaining=len(chain) - position - 1,
                    ):
                        logger.info(
                            "router.retry_yielded",
                            provider=spec.provider,
                            model=spec.model,
                            attempts=attempts,
                        )
                    else:
                        retries_used += 1
                        await sleep(_retry_delay_s(retries_used, jitter))
                        continue

                await breaker.record_failure(decision, exc)
                if exc.failover_eligible:
                    break

                logger.info("router.aborted", error_code=exc.code, attempts=attempts)
                raise RoutingFailed(
                    exc,
                    spec=spec,
                    trail=tuple(trail),
                    attempts=attempts,
                    latency_ms=_elapsed_ms(clock, started),
                    wasted_tokens_out=wasted_total,
                    key_pool=resolved.pool,
                ) from exc
            else:
                latency_ms = _elapsed_ms(clock, attempt_started)
                # `ttft_ms is None` is unreachable here — a stream with no chunk
                # at all raised EmptyResponse above — but mypy cannot see that and
                # a silent 0 would be a wrong measurement rather than a missing one.
                first_token_ms = ttft_ms if ttft_ms is not None else latency_ms
                final_usage = (
                    usage
                    if usage is not None
                    else Usage(
                        tokens_in=report.estimated_tokens,
                        tokens_out=_estimate_discarded_tokens(delivered_chars),
                        estimated=True,
                    )
                )
                if quota is not None and reservation is not None:
                    await quota.commit(
                        reservation,
                        tokens_in=final_usage.tokens_in,
                        tokens_out=final_usage.tokens_out,
                    )
                await _reconcile_hint(quota, spec, scope=resolved.scope)
                await breaker.record_success(decision)
                if metrics is not None:
                    metrics.record(spec.provider, spec.model, STREAM, first_token_ms)

                trail.append(
                    AttemptRecord(
                        n=len(trail) + 1,
                        slot=spec.slot,
                        provider=spec.provider,
                        model=spec.model,
                        outcome="ok",
                        latency_ms=latency_ms,
                        breaker=decision.state,
                        key_pool=resolved.pool,
                    )
                )
                logger.info(
                    "router.stream_served",
                    provider=spec.provider,
                    model=spec.model,
                    slot=spec.slot,
                    requested_slot=requested,
                    attempts=attempts,
                    latency_ms=latency_ms,
                    ttft_ms=first_token_ms,
                    wasted_tokens_out=wasted_total,
                )
                yield StreamCompleted(
                    spec=spec,
                    usage=final_usage,
                    finish_reason=finish_reason,
                    report=report,
                    trail=tuple(trail),
                    attempts=attempts,
                    latency_ms=_elapsed_ms(clock, started),
                    ttft_ms=first_token_ms,
                    wasted_tokens_out=wasted_total,
                    key_pool=resolved.pool,
                )
                return

    logger.warning(
        "router.stream_exhausted",
        requested_slot=requested,
        attempts=attempts,
        candidates=len(chain),
        error_code=last_error.code if last_error is not None else None,
        wasted_tokens_out=wasted_total,
    )
    raise RoutingFailed(
        last_error if last_error is not None else _all_skipped(chain, tuple(trail)),
        spec=last_spec,
        trail=tuple(trail),
        attempts=attempts,
        latency_ms=_elapsed_ms(clock, started),
        wasted_tokens_out=wasted_total,
        key_pool=last_key_pool,
    )


def _extra_grants(spec: ModelSpec, resolved: ResolvedKey) -> tuple[WindowGrant, ...]:
    """D39's personal-cap grant, or none — the router's one-line branch.

    Only the shared path can have a cap: the private path has no cap by
    construction (§9.4), and :attr:`ResolvedKey.shared_daily_cap` is always
    ``None`` there anyway, so ``allocations.shared_pool_grants`` would return
    ``()`` regardless — this check just skips the (harmless) call.
    """
    if resolved.pool != "shared" or resolved.shared_daily_cap is None or resolved.user_id is None:
        return ()
    return allocations.shared_pool_grants(
        spec, user_id=resolved.user_id, cap=resolved.shared_daily_cap
    )


async def _reconcile_hint(
    quota: QuotaTracker | None, spec: ModelSpec, *, scope: keys.Scope
) -> None:
    """Drain Step 6's contextvar sink after every attempt, success or failure.

    Draining happens unconditionally, even with ``quota`` disabled, because
    :func:`~app.providers.base.take_hint` clearing on read is what stops a
    hint from being misattributed to the *next* attempt in the loop (D18) —
    the next candidate's own response, if any, publishes its own. Applying
    the hint is what costs a Redis round trip, so that part is skipped
    outright when there is no tracker to correct.
    """
    hint = take_hint()
    if quota is not None and hint is not None:
        await quota.apply_hint(spec, scope=scope, hint=hint)


def _estimate_discarded_tokens(chars: int) -> int:
    """Characters over four. See :data:`DISCARDED_CHARS_PER_TOKEN`."""
    return chars // DISCARDED_CHARS_PER_TOKEN


def _retry_would_starve_failover(
    *, attempts: int, max_attempts: int, candidates_remaining: int
) -> bool:
    """Would spending the last attempt on a retry cost us the failover?

    ADR-015's reconciliation. There is one budget of three, so a retry and a
    different candidate compete for the same slot — and when only one is left, a
    candidate that has not failed yet is the better bet than a second go at one
    that just did. When nothing else remains, the retry is all there is and it
    goes ahead.
    """
    return attempts >= max_attempts - 1 and candidates_remaining > 0


def _retry_delay_s(attempt: int, rng: random.Random) -> float:
    """Exponential backoff with full jitter.

    Full jitter rather than a fixed delay because retries that all fire at the
    same offset re-synchronize into the thundering herd the backoff was meant to
    break up.
    """
    ceiling = min(RETRY_MAX_DELAY_S, RETRY_BASE_DELAY_S * (2 ** (attempt - 1)))
    return rng.uniform(0.0, ceiling)


def _elapsed_ms(clock: Clock, since: datetime) -> int:
    return int((clock.now() - since).total_seconds() * 1000)


def _all_skipped(chain: tuple[ModelSpec, ...], trail: tuple[AttemptRecord, ...]) -> ProviderError:
    """Every candidate was skipped, so nothing was ever attempted — but not every
    skip means the same thing, and the client's error has to say which.

    A breaker-only skip is what a fleet-wide outage looks like on the request
    *after* the one that opened everything: ``Unavailable``, as before. But if
    even one candidate was skipped on quota, the honest answer is ``RateLimited``
    with a ``Retry-After`` the client can actually use — the gateway did not fail,
    it declined to spend a budget it knows is gone, and conflating that with an
    outage would send a client's retry loop at a key that is not coming back any
    sooner for it.
    """
    quota_skips = [record for record in trail if record.outcome == "skipped_quota"]
    if quota_skips:
        first = quota_skips[0]
        retry_after_s = min(
            (record.retry_after_s for record in quota_skips if record.retry_after_s is not None),
            default=None,
        )
        return RateLimited(
            "every candidate is blocked on quota",
            provider=first.provider,
            model=first.model,
            retry_after_s=retry_after_s,
        )

    first_candidate = chain[0] if chain else None
    return Unavailable(
        "every candidate is in circuit-breaker cooldown",
        provider=first_candidate.provider if first_candidate is not None else "none",
        model=first_candidate.model if first_candidate is not None else "none",
    )


__all__ = [
    "DISCARDED_CHARS_PER_TOKEN",
    "MAX_ATTEMPTS",
    "MAX_SAME_PROVIDER_RETRIES",
    "AttemptAborted",
    "AttemptDelta",
    "AttemptOutcome",
    "AttemptRecord",
    "AttemptStarted",
    "RouteStreamEvent",
    "RouterOutcome",
    "RoutingFailed",
    "StreamCompleted",
    "route",
    "route_stream",
]
