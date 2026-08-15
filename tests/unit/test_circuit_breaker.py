"""The breaker as a state machine: closed → open → half_open → closed, and back.

Every test here is deterministic because time is injected. A cooldown ladder that
tops out at five minutes is untestable with ``sleep`` — the suite would take
longer than the phase did — and `FixedClock` is the seam that exists for it.

What these tests are actually defending, in order of how expensive the bug is:

**The trip rule.** Opening on the wrong error class takes a working free-tier
provider out of rotation for a full cooldown. ``EmptyResponse`` is the one that
matters: it is a failure, it is failover-eligible, and it must not count.

**The probe's exclusivity.** "Exactly one request may probe" is the whole point of
half-open. If it leaked, a recovering provider would be hit by every concurrent
request at once — a thundering herd aimed precisely at something that has just
demonstrated it is fragile.

**The stale-probe lease.** A holder that dies must not hold a healthy provider out
of rotation until the hash's one-hour TTL. This is the failure that would look
like "the gateway stopped using Gemini this afternoon and nobody knows why".

**Fail-open.** ADR-010. A dead Redis must produce a permissive answer, never an
exception — the breaker is an optimization, and an optimization that can take the
gateway down is worse than no optimization at all.

``fakeredis`` throughout: `FakeRedis` is a real async client, so the concurrent
case interleaves at genuine ``await`` points and ``HSETNX`` stays atomic across
them. This is the first ``asyncio.gather`` test in the repo.
"""

from __future__ import annotations

import asyncio

import pytest
from fakeredis.aioredis import FakeRedis

from app.cache import keys
from app.core.clock import FixedClock
from app.providers.errors import (
    AuthFailed,
    BadRequest,
    ContentFiltered,
    ContextTooLong,
    EmptyResponse,
    ProviderError,
    RateLimited,
    Unavailable,
)
from app.routing.circuit_breaker import (
    COOLDOWN_INITIAL_S,
    COOLDOWN_MAX_S,
    FAILURE_THRESHOLD,
    BreakerDecision,
    CircuitBreaker,
)

PROVIDER = "groq"
MODEL = "llama-3.3-70b-versatile"
KEY = keys.breaker(PROVIDER, MODEL)


@pytest.fixture
def breaker(redis_client: FakeRedis, frozen_clock: FixedClock) -> CircuitBreaker:
    return CircuitBreaker(redis_client, clock=frozen_clock)


def unavailable() -> Unavailable:
    return Unavailable("502 from upstream", provider=PROVIDER, model=MODEL, status_code=502)


def rate_limited(retry_after_s: float | None = None) -> RateLimited:
    return RateLimited(
        "quota exhausted",
        provider=PROVIDER,
        model=MODEL,
        retry_after_s=retry_after_s,
        status_code=429,
    )


async def drive_open(breaker: CircuitBreaker, error: ProviderError | None = None) -> None:
    """Take a closed breaker to open the way the router would — by failing."""
    for _ in range(FAILURE_THRESHOLD):
        decision = await breaker.allows(PROVIDER, MODEL)
        await breaker.record_failure(decision, error or unavailable())


# --------------------------------------------------------------------------- #
# closed
# --------------------------------------------------------------------------- #
async def test_an_unknown_candidate_is_allowed(breaker: CircuitBreaker) -> None:
    """No state means no evidence of failure. The default has to be permissive, or
    every cold process would refuse every provider until something proved itself."""
    decision = await breaker.allows(PROVIDER, MODEL)

    assert decision.allowed is True
    assert decision.state == "closed"
    assert decision.is_probe is False
    assert decision.degraded is False


async def test_the_happy_path_writes_nothing(
    breaker: CircuitBreaker, redis_client: FakeRedis
) -> None:
    """A healthy provider answering is the common case, and it must not cost a
    round trip. `allows` already read the failure count, so `record_success` knows
    there is nothing to reset without asking again."""
    decision = await breaker.allows(PROVIDER, MODEL)
    await breaker.record_success(decision)

    assert await redis_client.exists(KEY) == 0


async def test_failures_below_the_threshold_stay_closed(
    breaker: CircuitBreaker, redis_client: FakeRedis
) -> None:
    """A provider that fails one request in twenty is having a bad day, not an
    outage. Opening on it would cost more availability than it saves."""
    for _ in range(FAILURE_THRESHOLD - 1):
        decision = await breaker.allows(PROVIDER, MODEL)
        await breaker.record_failure(decision, unavailable())

    decision = await breaker.allows(PROVIDER, MODEL)

    assert decision.allowed is True
    assert decision.state == "closed"
    assert decision.failures == FAILURE_THRESHOLD - 1
    assert await redis_client.hget(KEY, "state") is None


async def test_the_threshold_failure_opens_the_breaker(breaker: CircuitBreaker) -> None:
    await drive_open(breaker)

    decision = await breaker.allows(PROVIDER, MODEL)

    assert decision.allowed is False
    assert decision.state == "open"
    assert decision.retry_after_s == pytest.approx(COOLDOWN_INITIAL_S)


async def test_a_success_resets_the_streak(breaker: CircuitBreaker) -> None:
    """Consecutive, not cumulative. Without the reset, a provider that fails once
    an hour would eventually open for reasons no one could reconstruct."""
    for _ in range(FAILURE_THRESHOLD - 1):
        decision = await breaker.allows(PROVIDER, MODEL)
        await breaker.record_failure(decision, unavailable())

    await breaker.record_success(await breaker.allows(PROVIDER, MODEL))

    for _ in range(FAILURE_THRESHOLD - 1):
        decision = await breaker.allows(PROVIDER, MODEL)
        await breaker.record_failure(decision, unavailable())

    assert (await breaker.allows(PROVIDER, MODEL)).allowed is True


# --------------------------------------------------------------------------- #
# Which errors count
# --------------------------------------------------------------------------- #
async def test_a_single_rate_limit_opens_immediately(breaker: CircuitBreaker) -> None:
    """Waiting for five 429s means four more requests against a provider that has
    already said it has nothing left — which is how a free-tier key gets banned,
    not rate-limited."""
    decision = await breaker.allows(PROVIDER, MODEL)
    await breaker.record_failure(decision, rate_limited())

    assert (await breaker.allows(PROVIDER, MODEL)).state == "open"


@pytest.mark.parametrize(
    "error",
    [
        EmptyResponse("200 with nothing in it", provider=PROVIDER, model=MODEL),
        BadRequest("we sent something malformed", provider=PROVIDER, model=MODEL),
        ContextTooLong("history too long", provider=PROVIDER, model=MODEL, limit_tokens=8192),
        ContentFiltered("declined on safety grounds", provider=PROVIDER, model=MODEL),
    ],
    ids=["empty_response", "bad_request", "context_too_long", "content_filtered"],
)
async def test_a_breaker_ineligible_failure_is_not_recorded_at_all(
    breaker: CircuitBreaker, redis_client: FakeRedis, error: ProviderError
) -> None:
    """`EmptyResponse` is the one that would actually bite: free tiers return
    200-with-nothing often enough that counting it would take a working provider
    out of rotation regularly. The other three are our bug or the user's prompt —
    neither says anything about whether the provider is up."""
    for _ in range(FAILURE_THRESHOLD * 2):
        decision = await breaker.allows(PROVIDER, MODEL)
        await breaker.record_failure(decision, error)

    assert await redis_client.exists(KEY) == 0
    assert (await breaker.allows(PROVIDER, MODEL)).allowed is True


async def test_auth_failure_counts_like_any_other_eligible_error(
    breaker: CircuitBreaker,
) -> None:
    """A revoked key is an ops problem, but from the router's side it is a provider
    that cannot serve. It carries `alert`; the breaker still just counts it."""
    for _ in range(FAILURE_THRESHOLD):
        decision = await breaker.allows(PROVIDER, MODEL)
        await breaker.record_failure(
            decision, AuthFailed("401", provider=PROVIDER, model=MODEL, status_code=401)
        )

    assert (await breaker.allows(PROVIDER, MODEL)).state == "open"


# --------------------------------------------------------------------------- #
# open
# --------------------------------------------------------------------------- #
async def test_retry_after_counts_down_as_time_passes(
    breaker: CircuitBreaker, frozen_clock: FixedClock
) -> None:
    """Phase 3's `/v1/models` reports this number to a client that will render it,
    so it has to shrink rather than repeat the original cooldown."""
    await drive_open(breaker)
    frozen_clock.advance(10)

    decision = await breaker.allows(PROVIDER, MODEL)

    assert decision.allowed is False
    assert decision.retry_after_s == pytest.approx(COOLDOWN_INITIAL_S - 10)


async def test_the_providers_own_retry_hint_becomes_the_cooldown(
    breaker: CircuitBreaker,
) -> None:
    """The only number in this system derived from the upstream's real state
    rather than our guess. It outranks the ladder."""
    decision = await breaker.allows(PROVIDER, MODEL)
    await breaker.record_failure(decision, rate_limited(retry_after_s=120))

    assert (await breaker.allows(PROVIDER, MODEL)).retry_after_s == pytest.approx(120)


async def test_an_enormous_retry_hint_is_capped(breaker: CircuitBreaker) -> None:
    """Contract C fixes this hash's TTL at one hour. An eight-hour cooldown would
    expire mid-cooldown and the breaker would silently close — worse than
    re-probing early, and invisible when it happens."""
    decision = await breaker.allows(PROVIDER, MODEL)
    await breaker.record_failure(decision, rate_limited(retry_after_s=8 * 60 * 60))

    assert (await breaker.allows(PROVIDER, MODEL)).retry_after_s == pytest.approx(COOLDOWN_MAX_S)
    assert COOLDOWN_MAX_S < keys.BREAKER_TTL_S


async def test_a_zero_retry_hint_still_produces_a_real_cooldown(
    breaker: CircuitBreaker,
) -> None:
    """A hint of zero would make `open` mean nothing, and the next request would
    probe instantly — which is the hammering the breaker exists to prevent."""
    decision = await breaker.allows(PROVIDER, MODEL)
    await breaker.record_failure(decision, rate_limited(retry_after_s=0))

    assert (await breaker.allows(PROVIDER, MODEL)).retry_after_s == pytest.approx(
        COOLDOWN_INITIAL_S
    )


async def test_an_open_breaker_expires_no_sooner_than_its_cooldown(
    breaker: CircuitBreaker, redis_client: FakeRedis
) -> None:
    """Without the TTL refresh on every write, a breaker that opened 59 minutes
    into the hash's life would expire mid-cooldown and come back closed, undoing
    its own decision."""
    await drive_open(breaker)

    ttl = await redis_client.ttl(KEY)

    assert ttl == pytest.approx(keys.BREAKER_TTL_S, abs=2)


# --------------------------------------------------------------------------- #
# half_open
# --------------------------------------------------------------------------- #
async def test_the_cooldown_elapsing_admits_a_probe(
    breaker: CircuitBreaker, frozen_clock: FixedClock, redis_client: FakeRedis
) -> None:
    await drive_open(breaker)
    frozen_clock.advance(COOLDOWN_INITIAL_S)

    decision = await breaker.allows(PROVIDER, MODEL)

    assert decision.allowed is True
    assert decision.is_probe is True
    assert decision.state == "half_open"
    # Written, not merely computed — so `HGETALL` during an incident shows the
    # state the code is acting on.
    assert await redis_client.hget(KEY, "state") == "half_open"


async def test_only_one_of_twenty_concurrent_callers_probes(
    breaker: CircuitBreaker, frozen_clock: FixedClock
) -> None:
    """The point of half-open. If it leaked, a recovering provider would take the
    full concurrent load the instant its cooldown expired — a thundering herd
    aimed at something that has just demonstrated it is fragile.

    HSETNX is what makes this hold across *instances*; an asyncio.Lock would only
    hold within one process, which is the wrong scope for shared state.
    """
    await drive_open(breaker)
    frozen_clock.advance(COOLDOWN_INITIAL_S)

    decisions = await asyncio.gather(
        *(breaker.allows(PROVIDER, MODEL, request_id=f"req-{n}") for n in range(20))
    )

    assert sum(decision.is_probe for decision in decisions) == 1
    assert sum(decision.allowed for decision in decisions) == 1


async def test_a_competitor_claiming_mid_call_refuses_us_rather_than_admitting_both(
    redis_client: FakeRedis, frozen_clock: FixedClock
) -> None:
    """The interleaving the twenty-caller test does *not* reach, and the one that
    actually happens across instances: two workers both read an elapsed cooldown,
    then both try to claim. Coverage says the gathered test settles the race in
    `allows` — the losers see the winner's reset `opened_at` and never get as far
    as HSETNX — so the branch that handles losing at HSETNX itself needs its own
    test, or it is defensive code no one has ever run.

    `_RacingRedis` lets the competitor in at exactly that moment.
    """
    breaker = CircuitBreaker(redis_client, clock=frozen_clock)
    await drive_open(breaker)
    frozen_clock.advance(COOLDOWN_INITIAL_S)

    class _RacingRedis:
        """Delegates everything, but lets a rival claim the probe first."""

        def __init__(self) -> None:
            self.rival_claimed = False

        async def hsetnx(self, key: str, field: str, value: str) -> int:
            if not self.rival_claimed:
                self.rival_claimed = True
                await CircuitBreaker(redis_client, clock=frozen_clock).allows(
                    PROVIDER, MODEL, request_id="rival"
                )
            result: int = await redis_client.hsetnx(key, field, value)
            return result

        def __getattr__(self, name: str) -> object:
            return getattr(redis_client, name)

    racing = CircuitBreaker(_RacingRedis(), clock=frozen_clock)  # type: ignore[arg-type]

    decision = await racing.allows(PROVIDER, MODEL, request_id="us")

    assert decision.allowed is False
    assert decision.is_probe is False
    assert await redis_client.hget(KEY, "probe_holder") == "rival"


async def test_a_successful_probe_closes_the_breaker(
    breaker: CircuitBreaker, frozen_clock: FixedClock, redis_client: FakeRedis
) -> None:
    await drive_open(breaker)
    frozen_clock.advance(COOLDOWN_INITIAL_S)

    probe = await breaker.allows(PROVIDER, MODEL)
    await breaker.record_success(probe)

    after = await breaker.allows(PROVIDER, MODEL)
    assert after.allowed is True
    assert after.state == "closed"
    assert after.failures == 0
    # Cleared on both outcomes, or the next half-open could never be claimed.
    assert await redis_client.hget(KEY, "probe_holder") is None


async def test_a_failed_probe_reopens_one_rung_up_the_ladder(
    breaker: CircuitBreaker, frozen_clock: FixedClock, redis_client: FakeRedis
) -> None:
    """A provider that fails its probe is less likely to be ready next time, not
    more. A flat cooldown would re-probe a dead provider every thirty seconds
    forever."""
    await drive_open(breaker)
    frozen_clock.advance(COOLDOWN_INITIAL_S)

    probe = await breaker.allows(PROVIDER, MODEL)
    await breaker.record_failure(probe, unavailable())

    decision = await breaker.allows(PROVIDER, MODEL)
    assert decision.allowed is False
    assert decision.retry_after_s == pytest.approx(COOLDOWN_INITIAL_S * 2)
    assert await redis_client.hget(KEY, "probe_holder") is None


async def test_the_ladder_stops_at_the_ceiling(
    breaker: CircuitBreaker, frozen_clock: FixedClock
) -> None:
    """Unbounded doubling reaches "try again in nine hours" within a dozen
    failures, and a provider that recovers in the meantime is never noticed."""
    await drive_open(breaker)

    for _ in range(10):
        cooldown = (await breaker.allows(PROVIDER, MODEL)).retry_after_s
        assert cooldown is not None
        frozen_clock.advance(cooldown)
        probe = await breaker.allows(PROVIDER, MODEL)
        await breaker.record_failure(probe, unavailable())

    assert (await breaker.allows(PROVIDER, MODEL)).retry_after_s == pytest.approx(COOLDOWN_MAX_S)


async def test_a_second_caller_is_refused_while_a_probe_is_in_flight(
    breaker: CircuitBreaker, frozen_clock: FixedClock
) -> None:
    await drive_open(breaker)
    frozen_clock.advance(COOLDOWN_INITIAL_S)

    await breaker.allows(PROVIDER, MODEL, request_id="prober")
    second = await breaker.allows(PROVIDER, MODEL, request_id="latecomer")

    assert second.allowed is False
    assert second.is_probe is False
    assert second.state == "half_open"


async def test_a_dead_probers_claim_is_stolen_once_its_lease_expires(
    breaker: CircuitBreaker, frozen_clock: FixedClock, redis_client: FakeRedis
) -> None:
    """The failure this prevents is the expensive one: a process that dies between
    claiming the probe and reporting its outcome would otherwise hold a possibly
    healthy provider out of rotation until the hash's one-hour TTL, and the
    symptom is "the gateway quietly stopped using Gemini this afternoon".
    """
    await drive_open(breaker)
    frozen_clock.advance(COOLDOWN_INITIAL_S)
    await breaker.allows(PROVIDER, MODEL, request_id="prober-that-dies")

    # One full cooldown with no outcome recorded: the holder is not coming back.
    frozen_clock.advance(COOLDOWN_INITIAL_S)
    successor = await breaker.allows(PROVIDER, MODEL, request_id="successor")

    assert successor.allowed is True
    assert successor.is_probe is True
    assert await redis_client.hget(KEY, "probe_holder") == "successor"


# --------------------------------------------------------------------------- #
# Redis unreachable — ADR-010
# --------------------------------------------------------------------------- #
class _DeadRedis:
    """Fails every command, the way an unreachable server does."""

    def __getattr__(self, name: str) -> object:
        async def command(*args: object, **kwargs: object) -> object:
            raise ConnectionError("connection refused")

        return command


async def test_an_unreachable_redis_allows_the_attempt(frozen_clock: FixedClock) -> None:
    """ADR-010. The breaker is an optimization that skips attempts we predict will
    fail; without the prediction the normalized error hierarchy still protects the
    request. Failing closed would turn a Redis blip into a total outage of a
    gateway whose providers were all healthy."""
    breaker = CircuitBreaker(_DeadRedis(), clock=frozen_clock)  # type: ignore[arg-type]

    decision = await breaker.allows(PROVIDER, MODEL)

    assert decision.allowed is True
    assert decision.degraded is True


async def test_redis_dying_between_asking_and_recording_does_not_raise(
    frozen_clock: FixedClock,
) -> None:
    """The realistic shape of this failure: Redis was up when we asked and gone by
    the time we recorded. The attempt already happened and its outcome is already
    the caller's — there is nothing left to raise into, and the only casualty is
    the breaker's memory of it.

    The decision is deliberately *not* degraded, so this exercises the write path
    rather than the early return that a degraded decision takes.
    """
    breaker = CircuitBreaker(_DeadRedis(), clock=frozen_clock)  # type: ignore[arg-type]
    live = BreakerDecision(provider=PROVIDER, model=MODEL, allowed=True, state="closed", failures=3)

    await breaker.record_success(live)
    await breaker.record_failure(live, unavailable())


async def test_a_failing_probe_outcome_survives_redis_dying(frozen_clock: FixedClock) -> None:
    """The probe branch writes more than the ordinary one — state, cooldown, and
    an HDEL — so it fails in a different place and needs its own line."""
    breaker = CircuitBreaker(_DeadRedis(), clock=frozen_clock)  # type: ignore[arg-type]
    probe = BreakerDecision(
        provider=PROVIDER, model=MODEL, allowed=True, state="half_open", is_probe=True
    )

    await breaker.record_success(probe)
    await breaker.record_failure(probe, unavailable())


# --------------------------------------------------------------------------- #
# Corrupt state
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "field",
    ["state", "failures", "opened_at", "cooldown_s"],
)
async def test_a_corrupt_field_is_treated_as_no_evidence(
    breaker: CircuitBreaker, redis_client: FakeRedis, field: str
) -> None:
    """Nothing should be able to write garbage here, which is exactly why the
    parsing must not raise if something does: an exception from the breaker would
    turn a cosmetic data problem into a failed request. Unreadable state means no
    evidence of failure, and no evidence means allow — the same default a cold
    process starts from.
    """
    await redis_client.hset(KEY, mapping={"state": "open", field: "not-a-number"})

    decision = await breaker.allows(PROVIDER, MODEL)

    assert decision.allowed is True


async def test_a_degraded_decision_is_never_written_back(
    breaker: CircuitBreaker, redis_client: FakeRedis
) -> None:
    """A decision made without Redis says nothing about the breaker's real state.
    Recording against it would write a failure count that was never read."""
    degraded = BreakerDecision(
        provider=PROVIDER, model=MODEL, allowed=True, state="closed", failures=4, degraded=True
    )

    await breaker.record_failure(degraded, unavailable())
    await breaker.record_success(degraded)

    assert await redis_client.exists(KEY) == 0


async def test_redis_dying_mid_claim_admits_the_probe(
    redis_client: FakeRedis, frozen_clock: FixedClock
) -> None:
    """Fail-open applies inside the claim too, not only at the first read. Redis
    going down between the two is the one moment where refusing would be easiest
    to write and hardest to justify — the candidate might be perfectly healthy and
    we would be skipping it on no evidence at all."""

    class _DiesOnClaim:
        async def hsetnx(self, *args: object, **kwargs: object) -> int:
            raise ConnectionError("connection refused")

        def __getattr__(self, name: str) -> object:
            return getattr(redis_client, name)

    await CircuitBreaker(redis_client, clock=frozen_clock).allows(PROVIDER, MODEL)
    await drive_open(CircuitBreaker(redis_client, clock=frozen_clock))
    frozen_clock.advance(COOLDOWN_INITIAL_S)

    decision = await CircuitBreaker(_DiesOnClaim(), clock=frozen_clock).allows(  # type: ignore[arg-type]
        PROVIDER, MODEL
    )

    assert decision.allowed is True
    assert decision.degraded is True


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
async def test_breakers_are_per_model_not_per_provider(breaker: CircuitBreaker) -> None:
    """Free-tier limits are per model, so one Groq model exhausting itself says
    nothing about another. Collapsing them onto the provider would erase a whole
    failover chain on the first 429."""
    await drive_open(breaker)

    other = await breaker.allows(PROVIDER, "llama-3.1-8b-instant")

    assert other.allowed is True
    assert (await breaker.allows(PROVIDER, MODEL)).allowed is False
