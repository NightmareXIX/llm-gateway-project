"""A three-state circuit breaker, per ``(provider, model)``, shared through Redis.

The gateway's memory of failure. Ask it "is Gemini worth trying right now?" and it
answers from state every instance can see, so one worker's discovery that a
provider is down is every worker's — which is the entire reason the state is in
Redis and not in a module-level dict.

```
closed     normal. Consecutive breaker-eligible failures accumulate.
           FAILURE_THRESHOLD of them, or a single RateLimited, → open.
open       skipped without an attempt, for cooldown_s.
half_open  the cooldown elapsed. Exactly one request probes.
           Probe succeeds → closed, counters reset. Probe fails → open, one rung up.
```

**Why it is hand-rolled** (ADR-013, short version): a library's breaker keeps
per-process state and trips on an exception allowlist. Both are wrong here. State
has to be shared or each worker rediscovers the same outage, and the trip rule is
:attr:`~app.providers.errors.ProviderError.breaker_eligible` — a class attribute
on our normalized hierarchy, which no library can know about.

**Three things this module deliberately does not do.**

It does not sweep. ``open → half_open`` is *computed* on read from the elapsed
time, never written by a background task: a gateway with no timer has no timer to
fail, and when every instance is equal there is no correct owner for a sweep.

It does not raise on Redis. Every command is wrapped and every failure is
permissive — ADR-010's fail-open rule. The breaker is an *optimization* that skips
attempts we predict will fail; without it the normalized error hierarchy still
protects the request, at the cost of one wasted round trip. Failing closed would
turn a Redis blip into a total outage of a gateway whose providers were all fine.

It does not decide anything about routing. It answers a question. Step 4's router
asks it, and owns what to do with the answer.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Final, Literal

from redis.asyncio import Redis

from app.cache import keys
from app.core.clock import SYSTEM_CLOCK, Clock
from app.core.logging import get_logger, get_request_id
from app.providers.errors import ProviderError, RateLimited
from app.usage.metrics import MetricsRegistry

logger = get_logger("app.routing.circuit_breaker")

type BreakerState = Literal["closed", "open", "half_open"]

type _Hash = dict[Any, Any]
"""What ``HGETALL`` hands back.

Deliberately loose. The client is built with ``decode_responses=True`` so the
values really are ``str``, but that is a runtime construction argument the type
stubs cannot see — they promise ``bytes | str`` either way. Rather than assert a
narrowing the type system has no basis for, every read goes through the coercing
helpers at the bottom of this module, which have to tolerate junk regardless.
"""

CLOSED: Final[BreakerState] = "closed"
OPEN: Final[BreakerState] = "open"
HALF_OPEN: Final[BreakerState] = "half_open"

FAILURE_THRESHOLD: Final = 5
"""Consecutive breaker-eligible failures that open a closed breaker.

Consecutive, not cumulative — a success resets the count. A provider that fails
one request in twenty is having a bad day, not an outage, and opening on it would
cost more availability than it saves."""

COOLDOWN_INITIAL_S: Final = 30.0
"""The first cooldown. Short enough that a blip costs one skipped candidate."""

COOLDOWN_MAX_S: Final = 300.0
"""Ceiling on the ladder, and on any ``retry_after_s`` a provider hands us.

Capping the provider's own hint looks like ignoring ground truth, and there is a
concrete reason for it: Contract C fixes this hash's TTL at one hour, so a
cooldown longer than that would have the state expire mid-cooldown and the
breaker silently close — worse than re-probing early, and invisible. A free tier
answering "retry in eight hours" is a *quota* fact anyway, and quota is Phase 3's
job with a reset-window model built for it. Re-probing early costs one request."""

COOLDOWN_FACTOR: Final = 2.0
"""Each re-open doubles the previous cooldown, up to the ceiling."""

_FIELD_STATE: Final = "state"
_FIELD_FAILURES: Final = "failures"
_FIELD_OPENED_AT: Final = "opened_at"
_FIELD_COOLDOWN: Final = "cooldown_s"
_FIELD_PROBE_HOLDER: Final = "probe_holder"


@dataclass(frozen=True, slots=True)
class BreakerDecision:
    """The breaker's answer, and the token the caller records its outcome with.

    Carrying the identity and the probe flag here — rather than making the caller
    repeat ``(provider, model)`` on the way back — is what makes three mistakes
    unrepresentable: recording against the wrong key, forgetting that this caller
    held the half-open probe, and recording an outcome for an attempt that was
    never asked about.
    """

    provider: str
    model: str
    allowed: bool
    state: BreakerState
    is_probe: bool = False
    failures: int = 0

    retry_after_s: float | None = None
    """When skipped: seconds until this candidate is worth trying again. Phase 3's
    ``/v1/models`` reports it; Step 4's attempt trail records it."""

    degraded: bool = False
    """This decision was made without Redis. ``allowed`` is a fail-open default
    rather than a judgement — see ADR-010."""


class CircuitBreaker:
    """Reads and writes ``cb:{provider}:{model}``. One instance per request is fine.

    Holds no state of its own: the hash is the state, and two instances in two
    processes are the same breaker. Constructing one is free, so Step 4's router
    builds it from ``RedisDep`` rather than the lifespan holding a singleton.

    The clock is injected (:mod:`app.core.clock`) so the state machine's tests can
    advance time instead of sleeping through a five-minute cooldown.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        clock: Clock = SYSTEM_CLOCK,
        failure_threshold: int = FAILURE_THRESHOLD,
        cooldown_initial_s: float = COOLDOWN_INITIAL_S,
        cooldown_max_s: float = COOLDOWN_MAX_S,
        cooldown_factor: float = COOLDOWN_FACTOR,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self._redis = redis
        self._clock = clock
        self._metrics = metrics
        """Phase 7 Step 4 (D49): where the fail-open counter ADR-010 asked for
        finally lands. Optional and defaulting to ``None`` — the same
        ``None``-keeps-every-existing-caller-honest shape D36 established — so a
        breaker built by hand in a test counts nothing and needs no edit."""
        self._failure_threshold = failure_threshold
        self._cooldown_initial_s = cooldown_initial_s
        self._cooldown_max_s = cooldown_max_s
        self._cooldown_factor = cooldown_factor

    # ----------------------------------------------------------------- #
    # Asking
    # ----------------------------------------------------------------- #
    async def allows(
        self, provider: str, model: str, *, request_id: str | None = None
    ) -> BreakerDecision:
        """Is this candidate worth an attempt right now?

        ``request_id`` identifies the half-open probe's holder and defaults to the
        contextvar the request middleware already bound. It is a parameter so a
        test can name two callers apart without standing up a request.
        """
        key = keys.breaker(provider, model)

        try:
            raw: _Hash = await self._redis.hgetall(key)
        except Exception as exc:
            return self._fail_open(provider, model, exc, action="allows")

        state = _read_state(raw)
        failures = _read_int(raw.get(_FIELD_FAILURES))

        if state == CLOSED:
            return BreakerDecision(
                provider=provider,
                model=model,
                allowed=True,
                state=CLOSED,
                failures=failures,
            )

        opened_at = _read_float(raw.get(_FIELD_OPENED_AT))
        cooldown_s = _read_float(raw.get(_FIELD_COOLDOWN)) or self._cooldown_initial_s
        elapsed = self._now() - opened_at

        if elapsed < cooldown_s:
            return BreakerDecision(
                provider=provider,
                model=model,
                allowed=False,
                state=OPEN if state == OPEN else HALF_OPEN,
                failures=failures,
                retry_after_s=cooldown_s - elapsed,
            )

        return await self._claim_probe(
            key,
            provider=provider,
            model=model,
            failures=failures,
            cooldown_s=cooldown_s,
            request_id=request_id or get_request_id() or "unknown",
        )

    async def _claim_probe(
        self,
        key: str,
        *,
        provider: str,
        model: str,
        failures: int,
        cooldown_s: float,
        request_id: str,
    ) -> BreakerDecision:
        """Try to become the one request allowed through a half-open breaker.

        ``HSETNX`` is the exclusion, and it is exclusive across *instances* — an
        ``asyncio.Lock`` would only be exclusive within one process, which is the
        wrong scope for state every worker shares.

        Claiming also resets ``opened_at``, which is the probe's lease. Without
        one, a process that dies between claiming and reporting would leave
        ``probe_holder`` set until the hash's one-hour TTL, holding a possibly
        healthy provider out of rotation for an hour. With one, a holder that has
        not reported within a full cooldown is stale and the next caller takes
        over. Two callers racing to steal a *stale* probe can both get through;
        that costs one extra request to a provider we were about to probe anyway,
        and is the price of not inventing a sixth hash field (ADR-013).
        """
        probe = BreakerDecision(
            provider=provider,
            model=model,
            allowed=True,
            state=HALF_OPEN,
            is_probe=True,
            failures=failures,
        )

        try:
            claimed = await self._redis.hsetnx(key, _FIELD_PROBE_HOLDER, request_id)
            if claimed:
                await self._write(
                    key, {_FIELD_STATE: HALF_OPEN, _FIELD_OPENED_AT: str(self._now())}
                )
                _log_transition(
                    provider, model, OPEN, HALF_OPEN, failures=failures, cooldown_s=cooldown_s
                )
                return probe

            # Someone holds it. They are either mid-probe or gone.
            raw: _Hash = await self._redis.hgetall(key)
            opened_at = _read_float(raw.get(_FIELD_OPENED_AT))
            lease_elapsed = self._now() - opened_at

            if lease_elapsed < cooldown_s:
                return replace(
                    probe,
                    allowed=False,
                    is_probe=False,
                    retry_after_s=cooldown_s - lease_elapsed,
                )

            await self._write(
                key,
                {
                    _FIELD_STATE: HALF_OPEN,
                    _FIELD_OPENED_AT: str(self._now()),
                    _FIELD_PROBE_HOLDER: request_id,
                },
            )
            logger.warning(
                "breaker.probe_stolen",
                provider=provider,
                model=model,
                previous_holder=raw.get(_FIELD_PROBE_HOLDER),
                lease_elapsed_s=round(lease_elapsed, 3),
            )
            return probe
        except Exception as exc:
            return self._fail_open(provider, model, exc, action="claim_probe")

    async def peek(self, provider: str, model: str) -> BreakerDecision:
        """Read the stored state without claiming a half-open probe.

        For ``GET /v1/models`` (Phase 3 Step 7), which has to answer "is this
        candidate available" without the side effect :meth:`allows` has: on a
        breaker whose cooldown has elapsed, ``allows`` claims the one probe slot
        via ``HSETNX``, on the assumption that the caller is about to spend it on
        a real attempt. A status read is not an attempt, and claiming the probe
        for one would strand a real request behind a stale holder for a full
        cooldown, for nothing.

        So this reads the hash and reports the state exactly as stored — never
        computing a fresh ``half_open`` eligibility the way ``allows`` does.
        ``open`` and ``half_open`` are both "not allowed" here, whether or not a
        probe happens to be claimable right now: half-open means *at most one*
        request gets through, which is not the same claim as "available".
        """
        key = keys.breaker(provider, model)

        try:
            raw: _Hash = await self._redis.hgetall(key)
        except Exception as exc:
            return self._fail_open(provider, model, exc, action="peek")

        state = _read_state(raw)
        if state == CLOSED:
            return BreakerDecision(provider=provider, model=model, allowed=True, state=CLOSED)

        opened_at = _read_float(raw.get(_FIELD_OPENED_AT))
        cooldown_s = _read_float(raw.get(_FIELD_COOLDOWN)) or self._cooldown_initial_s
        elapsed = self._now() - opened_at
        return BreakerDecision(
            provider=provider,
            model=model,
            allowed=False,
            state=state,
            failures=_read_int(raw.get(_FIELD_FAILURES)),
            retry_after_s=max(cooldown_s - elapsed, 0.0),
        )

    # ----------------------------------------------------------------- #
    # Recording
    # ----------------------------------------------------------------- #
    async def record_success(self, decision: BreakerDecision) -> None:
        """This candidate answered. Reset what the failures had accumulated."""
        if decision.degraded:
            return

        key = keys.breaker(decision.provider, decision.model)

        try:
            if decision.is_probe:
                await self._write(
                    key,
                    {
                        _FIELD_STATE: CLOSED,
                        _FIELD_FAILURES: "0",
                        _FIELD_COOLDOWN: str(self._cooldown_initial_s),
                    },
                )
                await self._redis.hdel(key, _FIELD_PROBE_HOLDER)
                _log_transition(
                    decision.provider,
                    decision.model,
                    HALF_OPEN,
                    CLOSED,
                    failures=0,
                    cooldown_s=self._cooldown_initial_s,
                )
                return

            if decision.failures:
                # Consecutive, so one success wipes the streak. Guarded by the
                # count `allows` already read: the common case — a healthy
                # provider answering — issues no Redis command at all.
                await self._write(key, {_FIELD_FAILURES: "0"})
        except Exception as exc:
            self._log_swallowed(decision, exc, action="record_success")

    async def record_failure(self, decision: BreakerDecision, error: ProviderError) -> None:
        """This candidate failed. Count it, and open if it has earned that.

        Only ``breaker_eligible`` errors count, and the flag is a class attribute
        precisely so this is one line no call site can override. ``EmptyResponse``
        is excluded on purpose: a free tier returning 200-with-nothing is annoying
        rather than broken, and opening on it takes a working provider out of
        rotation for the whole cooldown.
        """
        if decision.degraded or not error.breaker_eligible:
            return

        key = keys.breaker(decision.provider, decision.model)
        # Declared only on RateLimited — neither Unavailable nor AuthFailed
        # carries a retry hint, so this cannot be read off the base class.
        retry_after_s = error.retry_after_s if isinstance(error, RateLimited) else None

        try:
            if decision.is_probe:
                # The probe failed: straight back to open, one rung up the ladder.
                previous = await self._read_cooldown(key)
                await self._open(
                    key,
                    decision=decision,
                    from_state=HALF_OPEN,
                    failures=decision.failures,
                    cooldown_s=self._next_cooldown(previous, retry_after_s),
                )
                await self._redis.hdel(key, _FIELD_PROBE_HOLDER)
                return

            # HINCRBY returns the post-increment value, so the threshold test uses
            # what Redis actually stored. A read-then-write would let two
            # instances both see four and both write five.
            failures = int(await self._redis.hincrby(key, _FIELD_FAILURES, 1))
            await self._redis.expire(key, keys.BREAKER_TTL_S)

            if isinstance(error, RateLimited) or failures >= self._failure_threshold:
                # A single 429 opens immediately. Waiting for five of them means
                # four more requests against a provider that has already said it
                # has nothing left, which is how a free-tier key gets banned.
                await self._open(
                    key,
                    decision=decision,
                    from_state=CLOSED,
                    failures=failures,
                    cooldown_s=self._next_cooldown(None, retry_after_s),
                )
        except Exception as exc:
            self._log_swallowed(decision, exc, action="record_failure")

    # ----------------------------------------------------------------- #
    # Internals
    # ----------------------------------------------------------------- #
    async def _open(
        self,
        key: str,
        *,
        decision: BreakerDecision,
        from_state: BreakerState,
        failures: int,
        cooldown_s: float,
    ) -> None:
        await self._write(
            key,
            {
                _FIELD_STATE: OPEN,
                _FIELD_OPENED_AT: str(self._now()),
                _FIELD_COOLDOWN: str(cooldown_s),
                _FIELD_FAILURES: str(failures),
            },
        )
        _log_transition(
            decision.provider,
            decision.model,
            from_state,
            OPEN,
            failures=failures,
            cooldown_s=cooldown_s,
        )

    async def _write(self, key: str, fields: dict[str, str]) -> None:
        """One HSET plus a TTL refresh.

        The refresh is not optional: without it a breaker that opened 59 minutes
        ago expires mid-cooldown and comes back closed, silently undoing its own
        decision.
        """
        mapping: _Hash = dict(fields)
        await self._redis.hset(key, mapping=mapping)
        await self._redis.expire(key, keys.BREAKER_TTL_S)

    async def _read_cooldown(self, key: str) -> float | None:
        raw = await self._redis.hget(key, _FIELD_COOLDOWN)
        return _read_float(raw) or None

    def _next_cooldown(self, previous: float | None, retry_after_s: float | None) -> float:
        """The cooldown for the open we are about to perform.

        The provider's own hint wins when there is one — it is the only number in
        this system derived from the upstream's actual state rather than our
        guess — but it is still floored at the initial cooldown (a hint of zero
        would make ``open`` meaningless) and capped at the ceiling.
        """
        if retry_after_s is not None:
            return min(max(retry_after_s, self._cooldown_initial_s), self._cooldown_max_s)
        if previous is None:
            return self._cooldown_initial_s
        return min(previous * self._cooldown_factor, self._cooldown_max_s)

    def _now(self) -> float:
        return self._clock.now().timestamp()

    def _fail_open(
        self, provider: str, model: str, exc: Exception, *, action: str
    ) -> BreakerDecision:
        """Redis is unreachable, so allow the attempt and say the answer is a guess.

        Warning rather than debug, per ADR-010: everything Redis does here is an
        optimization, so a permanently-down Redis produces no user-visible symptom
        and the log line was, for five phases, the only place it appeared. Phase 7
        Step 4 (D49) gave it a second: ``gateway_breaker_fail_open_total``, which
        is what makes "how long has the breaker been guessing?" answerable from a
        graph rather than from log aggregation.
        """
        self._count_fail_open(provider, model)
        logger.warning(
            "breaker.fail_open",
            provider=provider,
            model=model,
            action=action,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return BreakerDecision(
            provider=provider,
            model=model,
            allowed=True,
            state=CLOSED,
            degraded=True,
        )

    def _log_swallowed(self, decision: BreakerDecision, exc: Exception, *, action: str) -> None:
        """A write we could not make. There is nothing to raise into.

        The attempt already happened and its outcome is already the caller's; the
        only casualty is the breaker's memory of it, which is precisely the thing
        ADR-010 says we are willing to lose.
        """
        self._count_fail_open(decision.provider, decision.model)
        logger.warning(
            "breaker.fail_open",
            provider=decision.provider,
            model=decision.model,
            action=action,
            error=str(exc),
            error_type=type(exc).__name__,
        )

    def _count_fail_open(self, provider: str, model: str) -> None:
        """One place, so the two log sites cannot drift from the one counter."""
        if self._metrics is not None:
            self._metrics.record_breaker_fail_open(provider, model)


# --------------------------------------------------------------------------- #
# Parsing and logging helpers
# --------------------------------------------------------------------------- #
def _log_transition(
    provider: str,
    model: str,
    from_state: BreakerState,
    to_state: BreakerState,
    *,
    failures: int,
    cooldown_s: float,
) -> None:
    """One event name for every transition.

    ``breaker.transition`` with a from/to pair rather than three separate event
    names, following ``auth.token_rejected``'s shape — one event, a discriminating
    field. Phase 7's chaos demo is a log tail of this line, and that only works if
    there is exactly one thing to tail. (``from`` is a Python keyword, hence
    ``from_state``.)
    """
    logger.info(
        "breaker.transition",
        provider=provider,
        model=model,
        from_state=from_state,
        to_state=to_state,
        failures=failures,
        cooldown_s=round(cooldown_s, 3),
    )


def _read_state(raw: _Hash) -> BreakerState:
    """The stored state, defaulting to ``closed``.

    An absent hash is a breaker that has never failed, and an unrecognized value
    is a corrupt one; both are treated as closed, because the failure mode of
    guessing "open" is refusing to use a provider that is probably fine.
    """
    state = raw.get(_FIELD_STATE)
    if state == OPEN:
        return OPEN
    if state == HALF_OPEN:
        return HALF_OPEN
    return CLOSED


def _read_int(raw: Any) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _read_float(raw: Any) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "COOLDOWN_INITIAL_S",
    "COOLDOWN_MAX_S",
    "FAILURE_THRESHOLD",
    "BreakerDecision",
    "BreakerState",
    "CircuitBreaker",
]
