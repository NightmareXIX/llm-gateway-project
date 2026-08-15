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
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Final, Literal

from app.core.clock import SYSTEM_CLOCK, Clock
from app.core.logging import get_logger
from app.memory.canonical import CanonicalMessage
from app.memory.render import AttachmentResolver, RenderReport, render
from app.providers.base import DEFAULT_IDLE_TIMEOUT_S, DEFAULT_READ_TIMEOUT_S
from app.providers.errors import ContextTooLong, ProviderError, Unavailable
from app.providers.registry import ProviderRegistry
from app.providers.types import Completion, GenParams, ModelSpec, StreamChunk
from app.routing import selection
from app.routing.circuit_breaker import BreakerState, CircuitBreaker
from app.usage.metrics import COMPLETE, LatencyTable

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

type AttemptOutcome = Literal["ok", "error", "skipped_breaker"]


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
    """Only on a skip: how long the breaker says this candidate is still out."""

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
    ) -> None:
        self.error = error
        self.spec = spec
        """The last candidate attempted, or ``None`` when every one was skipped.
        The error names the provider and model; only this names the *slot*."""

        self.trail = trail
        self.attempts = attempts
        self.latency_ms = latency_ms
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
    """
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
            attempts += 1
            attempt_started = clock.now()
            adapter = registry.adapter_for_spec(spec)
            key = registry.system_key(spec.provider)

            try:
                payload, report = await render(history, spec, params, adapter, resolver=resolver)
                completion = await adapter.complete(payload, key, timeout=timeout_s)
            except ProviderError as exc:
                latency_ms = _elapsed_ms(clock, attempt_started)
                last_error = exc
                last_spec = spec
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
                ) from exc
            else:
                latency_ms = _elapsed_ms(clock, attempt_started)
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
                )

    logger.warning(
        "router.exhausted",
        requested_slot=requested,
        attempts=attempts,
        candidates=len(chain),
        error_code=last_error.code if last_error is not None else None,
    )
    raise RoutingFailed(
        last_error if last_error is not None else _all_skipped(chain),
        spec=last_spec,
        trail=tuple(trail),
        attempts=attempts,
        latency_ms=_elapsed_ms(clock, started),
    )


def route_stream(
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
    max_attempts: int = MAX_ATTEMPTS,
    clock: Clock = SYSTEM_CLOCK,
    rng: random.Random | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncIterator[StreamChunk]:
    """The streaming twin of :func:`route`. Step 9's orchestrator drives it.

    Declared now, with the same parameters, so the restart state machine has one
    obvious place to attach and cannot grow a second copy of the attempt
    bookkeeping above. The element type is the part Step 9 settles: if the
    orchestrator turns out to need attempt boundaries in-band rather than deltas
    alone, it changes *here* rather than in a parallel loop.

    A plain ``def`` that raises, deliberately — an ``async def`` generator would
    swallow this until the first ``__anext__``, which is a confusing place to
    discover an unbuilt seam. It also records the TTFT series
    (:data:`~app.usage.metrics.STREAM`), not the total-latency one.
    """
    raise NotImplementedError(
        "Phase 2 Step 9: the D1 restart state machine lands with streaming/orchestrator.py"
    )


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


def _all_skipped(chain: tuple[ModelSpec, ...]) -> ProviderError:
    """Every candidate's breaker was open, so nothing was ever attempted.

    A real outcome, not an impossible one: it is what a fleet-wide outage looks
    like on the request *after* the one that opened everything. Normalized into
    the hierarchy so the endpoint's ``to_app_error`` handles it like any other
    upstream failure rather than needing a case of its own.
    """
    first = chain[0] if chain else None
    return Unavailable(
        "every candidate is in circuit-breaker cooldown",
        provider=first.provider if first is not None else "none",
        model=first.model if first is not None else "none",
    )


__all__ = [
    "MAX_ATTEMPTS",
    "MAX_SAME_PROVIDER_RETRIES",
    "AttemptOutcome",
    "AttemptRecord",
    "RouterOutcome",
    "RoutingFailed",
    "route",
    "route_stream",
]
