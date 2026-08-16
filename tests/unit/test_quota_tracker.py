"""The reserve → commit/release lifecycle, against ``fakeredis[lua]``.

Everything here runs the real ``.lua`` files. That is deliberate: the scripts are
where this phase's hardest bug lives, and a test that mocked them would assert
only that Python passes arguments to something.

Four claims are worth the file on their own.

**Fifty concurrent reserves against a limit of ten grant exactly ten.** The test
the whole Lua design exists to pass. A check-then-increment across two round
trips passes every other test in this module and fails this one, silently, in a
way that surfaces days later as a provider rate-limiting earlier than predicted.

**A per-minute counter's TTL is set once.** Refreshed on every increment it never
expires under sustained traffic, ``rpm`` climbs forever, and the gateway serves a
permanent false 429 that looks exactly like a provider outage (trap 1). A daily
counter is the opposite and must refresh, because its TTL converges on a real
instant.

**Every window is checked before any is incremented.** A script that increments
as it goes and bails on window three leaves two counters permanently overstated
(trap 3) — invisible except as budget that quietly stops existing.

**Redis failing means refusing.** D15's asymmetry with the breaker: no
reservation is no attempt, because a key banned for sustained over-limit traffic
does not come back.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from fakeredis.aioredis import FakeRedis

from app.cache import keys
from app.cache.client import LuaScriptRegistry
from app.config import LimitsConfig
from app.core.clock import FixedClock
from app.providers.types import ModelSpec, QuotaHint
from app.quota.tracker import QuotaTracker, Reservation

PROVIDER = "groq"
MODEL = "llama-3.3-70b-versatile"
SCOPE = keys.SYSTEM_SCOPE
REQUEST_ID = "req-0001"

NOON = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _spec(model: str = MODEL, *, provider: str = PROVIDER) -> ModelSpec:
    return ModelSpec(
        slot="fast",
        provider=provider,
        model=model,
        context_window=131072,
        max_output_tokens=32768,
        supports_streaming=True,
        supports_vision=False,
        supports_pdf=False,
        supports_system_field=False,
        max_file_bytes=None,
        priority=0,
    )


def _limits(**overrides: Any) -> LimitsConfig:
    """A limits table shaped like ``config/limits.yaml``'s Groq block.

    Built here rather than read from the real file so a limit change in config
    cannot quietly rewrite what these tests assert. The daily windows are
    ``fixed_daily_utc`` and the per-minute ones ``rolling_60s``, which is the
    combination that makes the TTL rules distinguishable.
    """
    model_limits: dict[str, Any] = {
        "rpm": 10,
        "rpd": 100,
        "tpm": 1000,
        "tpd": 10000,
        "reset": {
            "rpm": "rolling_60s",
            "rpd": "fixed_daily_utc",
            "tpm": "rolling_60s",
            "tpd": "fixed_daily_utc",
        },
    }
    model_limits.update(overrides)
    return LimitsConfig.model_validate(
        {
            "version": 1,
            "limits": {PROVIDER: {MODEL: model_limits}},
            "gateway": {"free": {"rpm": 20, "rpd": 500}},
        }
    )


@pytest.fixture
def scripts(redis_client: FakeRedis) -> LuaScriptRegistry:
    """The real ``app/quota/scripts/*.lua``, registered against fakeredis."""
    registry = LuaScriptRegistry(redis_client)
    registry.load_dir()
    return registry


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOON)


@pytest.fixture
def tracker(redis_client: FakeRedis, scripts: LuaScriptRegistry, clock: FixedClock) -> QuotaTracker:
    """Headroom off by default, so a test that asserts a limit of 10 is asserting
    the number it wrote down. One test turns it on."""
    return QuotaTracker(redis_client, scripts, _limits(), clock=clock, headroom=0.0)


def _counter(window: keys.QuotaWindow, *, model: str = MODEL) -> str:
    return keys.quota(SCOPE, PROVIDER, model, window)


async def _reserve(
    tracker: QuotaTracker, *, tokens: int = 100, request_id: str = REQUEST_ID
) -> Any:
    return await tracker.reserve(
        _spec(), scope=SCOPE, estimated_tokens=tokens, request_id=request_id
    )


# --------------------------------------------------------------------------- #
# The scripts are found at all
# --------------------------------------------------------------------------- #
def test_all_three_scripts_ship_in_the_directory_the_registry_scans(
    scripts: LuaScriptRegistry,
) -> None:
    """``LUA_SCRIPT_DIR`` pointed at an empty directory for two phases. If a
    rename ever unhooks it again the tracker fails *closed* on every request,
    which is a total outage rather than a degradation — so the wiring itself is
    worth one assertion."""
    assert "reserve" in scripts
    assert "commit" in scripts
    assert "release" in scripts


# --------------------------------------------------------------------------- #
# Reserving
# --------------------------------------------------------------------------- #
async def test_a_reservation_increments_every_declared_window(
    tracker: QuotaTracker, redis_client: FakeRedis
) -> None:
    decision = await _reserve(tracker, tokens=250)

    assert decision.allowed
    assert decision.reservation is not None
    assert decision.reservation.windows == ("rpm", "rpd", "tpm", "tpd")

    assert await redis_client.get(_counter("rpm")) == "1"
    assert await redis_client.get(_counter("rpd")) == "1"
    assert await redis_client.get(_counter("tpm")) == "250"
    assert await redis_client.get(_counter("tpd")) == "250"


async def test_the_reservation_hash_records_what_each_window_was_charged(
    tracker: QuotaTracker, redis_client: FakeRedis
) -> None:
    """Commit and release read their amounts from here rather than trusting the
    caller — which is what makes a double release harmless."""
    await _reserve(tracker, tokens=250)

    stored = await redis_client.hgetall(keys.quota_reservation(SCOPE, PROVIDER, MODEL, REQUEST_ID))
    assert stored == {
        "rpm": "1",
        "rpd": "1",
        "tpm": "250",
        "tpd": "250",
        "request_id": REQUEST_ID,
    }
    assert (
        0
        < await redis_client.ttl(keys.quota_reservation(SCOPE, PROVIDER, MODEL, REQUEST_ID))
        <= keys.RESERVATION_TTL_S
    )


async def test_a_blocked_window_names_itself_with_a_usable_retry_after(
    tracker: QuotaTracker, redis_client: FakeRedis
) -> None:
    await redis_client.set(_counter("rpm"), 10, ex=42)

    decision = await _reserve(tracker)

    assert not decision.allowed
    assert not decision.degraded
    assert decision.blocked_window == "rpm"
    assert decision.retry_after_s == pytest.approx(42, abs=1)
    assert decision.resets_at is not None
    assert decision.resets_at > NOON
    assert decision.reservation is None


async def test_a_rejected_reservation_leaves_no_counter_touched(
    tracker: QuotaTracker, redis_client: FakeRedis
) -> None:
    """Trap 3. The blocking window is the *third* one checked, so a script that
    incremented as it went would leave ``rpm`` and ``rpd`` overstated by one with
    no reservation recorded to give them back."""
    await redis_client.set(_counter("tpm"), 950, ex=60)

    decision = await _reserve(tracker, tokens=100)

    assert decision.blocked_window == "tpm"
    assert await redis_client.get(_counter("rpm")) is None
    assert await redis_client.get(_counter("rpd")) is None
    assert await redis_client.get(_counter("tpm")) == "950"
    assert await redis_client.get(_counter("tpd")) is None
    assert not await redis_client.exists(keys.quota_reservation(SCOPE, PROVIDER, MODEL, REQUEST_ID))


async def test_the_limit_is_a_ceiling_not_a_threshold(tracker: QuotaTracker) -> None:
    """Ten reservations against ``rpm: 10`` succeed; the eleventh does not."""
    for index in range(10):
        assert (await _reserve(tracker, tokens=1, request_id=f"req-{index}")).allowed

    assert not (await _reserve(tracker, tokens=1, request_id="req-eleven")).allowed


async def test_a_model_with_no_declared_limits_is_allowed_without_a_round_trip(
    tracker: QuotaTracker, redis_client: FakeRedis
) -> None:
    """Not fail-open by sleight of hand: an unlisted model has no published limit
    to overspend. It writes nothing, so there is nothing to settle either."""
    decision = await tracker.reserve(
        _spec("mystery-model"), scope=SCOPE, estimated_tokens=100, request_id=REQUEST_ID
    )

    assert decision.allowed
    assert decision.reservation is not None
    assert decision.reservation.windows == ()
    assert await redis_client.keys("q:*") == []


async def test_a_null_window_is_skipped_rather_than_read_as_zero(
    redis_client: FakeRedis, scripts: LuaScriptRegistry, clock: FixedClock
) -> None:
    """``limits.yaml``'s ``null`` means "the provider publishes no such window".
    Read as zero it would block every request; read as unlimited it would let a
    real limit be blown through. Dropped is the only correct answer."""
    limits = _limits(tpm=None, tpd=None, reset={"rpm": "rolling_60s", "rpd": "fixed_daily_utc"})
    tracker = QuotaTracker(redis_client, scripts, limits, clock=clock)

    decision = await _reserve(tracker, tokens=100_000)

    assert decision.allowed
    assert decision.reservation is not None
    assert decision.reservation.windows == ("rpm", "rpd")
    assert await redis_client.get(_counter("tpm")) is None


async def test_headroom_reduces_the_limit_the_script_enforces(
    redis_client: FakeRedis, scripts: LuaScriptRegistry, clock: FixedClock
) -> None:
    """D16: ten percent of every published limit is held back, so the effective
    ceiling sits under the real one by more than the fixed window's boundary
    overshoot costs."""
    tracker = QuotaTracker(redis_client, scripts, _limits(), clock=clock, headroom=0.1)

    for index in range(9):
        assert (await _reserve(tracker, tokens=1, request_id=f"req-{index}")).allowed

    blocked = await _reserve(tracker, tokens=1, request_id="req-ten")
    assert not blocked.allowed
    assert blocked.blocked_window == "rpm"


async def test_headroom_never_rounds_a_published_limit_down_to_zero(
    redis_client: FakeRedis, scripts: LuaScriptRegistry, clock: FixedClock
) -> None:
    """A model that could never be called reads exactly like a provider outage."""
    tracker = QuotaTracker(redis_client, scripts, _limits(rpm=1), clock=clock, headroom=0.9)

    assert (await _reserve(tracker, tokens=1)).allowed


async def test_a_negative_token_estimate_is_a_programming_error(tracker: QuotaTracker) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        await _reserve(tracker, tokens=-1)


# --------------------------------------------------------------------------- #
# TTLs — trap 1
# --------------------------------------------------------------------------- #
async def test_a_per_minute_ttl_is_set_once_and_never_refreshed(
    tracker: QuotaTracker, redis_client: FakeRedis, clock: FixedClock
) -> None:
    """The single most expensive bug available in this phase. Refreshed on every
    increment, ``rpm`` never expires under sustained traffic and the gateway
    serves a permanent false 429."""
    await _reserve(tracker, tokens=1, request_id="req-a")
    assert await redis_client.ttl(_counter("rpm")) == keys.QUOTA_WINDOW_TTL_S

    # Age the counter, then spend against it again.
    await redis_client.expire(_counter("rpm"), 20)
    clock.advance(40)
    await _reserve(tracker, tokens=1, request_id="req-b")

    assert await redis_client.ttl(_counter("rpm")) == 20


async def test_a_daily_ttl_is_refreshed_on_every_increment(
    tracker: QuotaTracker, redis_client: FakeRedis, clock: FixedClock
) -> None:
    """The opposite rule, for the opposite reason: "seconds until midnight UTC"
    converges on a real instant, so re-setting it is idempotent and keeps the key
    from expiring early after a mid-day restart."""
    await _reserve(tracker, tokens=1, request_id="req-a")
    assert await redis_client.ttl(_counter("rpd")) == 12 * 3600  # noon → midnight

    await redis_client.expire(_counter("rpd"), 30)
    clock.advance(3600)
    await _reserve(tracker, tokens=1, request_id="req-b")

    assert await redis_client.ttl(_counter("rpd")) == 11 * 3600


# --------------------------------------------------------------------------- #
# Committing
# --------------------------------------------------------------------------- #
async def test_commit_adjusts_token_windows_downward(
    tracker: QuotaTracker, redis_client: FakeRedis
) -> None:
    decision = await _reserve(tracker, tokens=1000)
    assert decision.reservation is not None

    await tracker.commit(decision.reservation, tokens_in=100, tokens_out=50)

    assert await redis_client.get(_counter("tpm")) == "150"
    assert await redis_client.get(_counter("tpd")) == "150"


async def test_commit_adjusts_token_windows_upward(
    tracker: QuotaTracker, redis_client: FakeRedis
) -> None:
    """An estimate that was 20% low is the normal case, not the exceptional one —
    which is why the correction has to work in both directions."""
    decision = await _reserve(tracker, tokens=100)
    assert decision.reservation is not None

    await tracker.commit(decision.reservation, tokens_in=400, tokens_out=200)

    assert await redis_client.get(_counter("tpm")) == "600"


async def test_commit_never_gives_back_the_request(
    tracker: QuotaTracker, redis_client: FakeRedis
) -> None:
    """Trap 6. A 429 you provoked is a request the provider counted, and a failed
    attempt commits rather than releases — with the tokens it really generated,
    which on the non-streaming path is none."""
    decision = await _reserve(tracker, tokens=900)
    assert decision.reservation is not None

    await tracker.commit(decision.reservation, tokens_in=0, tokens_out=0)

    assert await redis_client.get(_counter("rpm")) == "1"
    assert await redis_client.get(_counter("rpd")) == "1"
    assert await redis_client.get(_counter("tpm")) == "0"


async def test_commit_deletes_the_reservation(
    tracker: QuotaTracker, redis_client: FakeRedis
) -> None:
    decision = await _reserve(tracker)
    assert decision.reservation is not None

    await tracker.commit(decision.reservation, tokens_in=10, tokens_out=10)

    assert not await redis_client.exists(keys.quota_reservation(SCOPE, PROVIDER, MODEL, REQUEST_ID))


async def test_an_expired_reservation_commits_nothing(
    tracker: QuotaTracker, redis_client: FakeRedis
) -> None:
    """D17, trap 14. ``RESERVATION_TTL_S`` is 120s and a long generation outlives
    it. The estimate stays counted — over-counting is the safe direction, and
    applying the delta blind would double-count a stream a release already
    refunded."""
    decision = await _reserve(tracker, tokens=900)
    assert decision.reservation is not None
    await redis_client.delete(keys.quota_reservation(SCOPE, PROVIDER, MODEL, REQUEST_ID))

    await tracker.commit(decision.reservation, tokens_in=10, tokens_out=10)

    assert await redis_client.get(_counter("tpm")) == "900"


async def test_committing_a_model_with_no_token_windows_still_clears_the_reservation(
    redis_client: FakeRedis, scripts: LuaScriptRegistry, clock: FixedClock
) -> None:
    """OpenRouter publishes no TPM at all. There is nothing to correct, and the
    hash must still go — otherwise it holds until its TTL and a same-request
    retry accumulates onto a reservation nobody will settle."""
    limits = _limits(tpm=None, tpd=None, reset={"rpm": "rolling_60s", "rpd": "fixed_daily_utc"})
    tracker = QuotaTracker(redis_client, scripts, limits, clock=clock)
    decision = await _reserve(tracker)
    assert decision.reservation is not None

    await tracker.commit(decision.reservation, tokens_in=10, tokens_out=10)

    assert not await redis_client.exists(keys.quota_reservation(SCOPE, PROVIDER, MODEL, REQUEST_ID))


# --------------------------------------------------------------------------- #
# Releasing
# --------------------------------------------------------------------------- #
async def test_release_restores_every_window_exactly(
    tracker: QuotaTracker, redis_client: FakeRedis
) -> None:
    decision = await _reserve(tracker, tokens=250)
    assert decision.reservation is not None

    await tracker.release(decision.reservation)

    assert await redis_client.get(_counter("rpm")) == "0"
    assert await redis_client.get(_counter("rpd")) == "0"
    assert await redis_client.get(_counter("tpm")) == "0"
    assert await redis_client.get(_counter("tpd")) == "0"
    assert not await redis_client.exists(keys.quota_reservation(SCOPE, PROVIDER, MODEL, REQUEST_ID))


async def test_release_only_gives_back_its_own_reservation(
    tracker: QuotaTracker, redis_client: FakeRedis
) -> None:
    """Two requests in flight; one is abandoned. The other's budget stays spent."""
    first = await _reserve(tracker, tokens=100, request_id="req-a")
    await _reserve(tracker, tokens=100, request_id="req-b")
    assert first.reservation is not None

    await tracker.release(first.reservation)

    assert await redis_client.get(_counter("rpm")) == "1"
    assert await redis_client.get(_counter("tpm")) == "100"


async def test_a_second_release_is_harmless(tracker: QuotaTracker, redis_client: FakeRedis) -> None:
    """The amounts live in the hash and the hash is deleted, so a release cannot
    subtract twice what a reserve added once."""
    decision = await _reserve(tracker, tokens=100)
    assert decision.reservation is not None

    await tracker.release(decision.reservation)
    await tracker.release(decision.reservation)

    assert await redis_client.get(_counter("rpm")) == "0"


async def test_release_does_not_resurrect_an_expired_counter(
    tracker: QuotaTracker, redis_client: FakeRedis
) -> None:
    """A window that has already reset needs no refund, and writing one would
    leave a TTL-less key holding a clamped zero forever."""
    decision = await _reserve(tracker, tokens=100)
    assert decision.reservation is not None
    await redis_client.delete(_counter("rpm"))

    await tracker.release(decision.reservation)

    assert await redis_client.get(_counter("rpm")) is None


# --------------------------------------------------------------------------- #
# Concurrency — the test the Lua design exists to pass
# --------------------------------------------------------------------------- #
async def test_fifty_concurrent_reserves_against_a_limit_of_ten_grant_exactly_ten(
    tracker: QuotaTracker, redis_client: FakeRedis
) -> None:
    """Trap 2, and the interview answer. A check followed by an increment across
    two round trips passes every other test in this file: fifty callers all read
    a counter below the limit, all see room, and all increment. The overshoot is
    invisible until a provider rate-limits earlier than predicted, which is a
    banned key away from being permanent."""
    decisions = await asyncio.gather(
        *(_reserve(tracker, tokens=1, request_id=f"req-{index}") for index in range(50))
    )

    assert sum(1 for decision in decisions if decision.allowed) == 10
    assert await redis_client.get(_counter("rpm")) == "10"
    assert {d.blocked_window for d in decisions if not d.allowed} == {"rpm"}


# --------------------------------------------------------------------------- #
# Redis down — D15
# --------------------------------------------------------------------------- #
class _BrokenRedis:
    """Every command raises. What an unreachable Upstash looks like from here."""

    def __init__(self) -> None:
        self.calls = 0

    def pipeline(self, transaction: bool = True) -> Any:
        raise ConnectionError("redis is down")


def _broken_scripts() -> LuaScriptRegistry:
    class _BrokenScript:
        async def __call__(self, keys: Any = None, args: Any = None) -> Any:
            raise ConnectionError("redis is down")

    registry = LuaScriptRegistry(_BrokenRedis())  # type: ignore[arg-type]
    for name in ("reserve", "commit", "release"):
        registry._scripts[name] = _BrokenScript()  # type: ignore[assignment]
    return registry


async def test_reserve_fails_closed_when_redis_is_unreachable(clock: FixedClock) -> None:
    """D15, and the opposite of the breaker's rule (ADR-010). The breaker's
    absence costs one wasted round trip; quota's absence costs a key banned for
    sustained over-limit traffic, which no failover recovers."""
    tracker = QuotaTracker(_BrokenRedis(), _broken_scripts(), _limits(), clock=clock)  # type: ignore[arg-type]

    decision = await _reserve(tracker)

    assert not decision.allowed
    assert decision.degraded
    assert decision.reservation is None


async def test_a_missing_script_also_fails_closed(
    redis_client: FakeRedis, clock: FixedClock
) -> None:
    """A registry that scanned the wrong directory is a packaging mistake, not a
    reason to spend a budget nobody is counting."""
    tracker = QuotaTracker(redis_client, LuaScriptRegistry(redis_client), _limits(), clock=clock)

    decision = await _reserve(tracker)

    assert not decision.allowed
    assert decision.degraded


async def test_settlement_swallows_a_redis_failure(clock: FixedClock) -> None:
    """By the time commit runs the attempt has already happened and there is
    nothing left to refuse — the only casualty is the correction."""
    tracker = QuotaTracker(_BrokenRedis(), _broken_scripts(), _limits(), clock=clock)  # type: ignore[arg-type]
    reservation = Reservation(
        scope=SCOPE,
        provider=PROVIDER,
        model=MODEL,
        request_id=REQUEST_ID,
        tokens=100,
        windows=("rpm", "rpd", "tpm", "tpd"),
    )

    await tracker.commit(reservation, tokens_in=1, tokens_out=1)
    await tracker.release(reservation)


async def test_remaining_reports_nothing_rather_than_guessing_when_redis_is_down(
    clock: FixedClock,
) -> None:
    """Reads are not where fail-closed applies: nothing is being spent, and
    ``/v1/models`` reports ``unknown`` rather than inventing an ``available``."""
    tracker = QuotaTracker(_BrokenRedis(), _broken_scripts(), _limits(), clock=clock)  # type: ignore[arg-type]

    assert await tracker.remaining(_spec(), scope=SCOPE) == ()


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
async def test_remaining_reports_each_window_with_its_own_reset(
    tracker: QuotaTracker,
) -> None:
    await _reserve(tracker, tokens=250)

    states = {state.window: state for state in await tracker.remaining(_spec(), scope=SCOPE)}

    assert states["rpm"].used == 1
    assert states["rpm"].remaining == 9
    assert states["rpm"].resets_at == NOON.replace(minute=1)
    assert states["tpd"].used == 250
    assert states["tpd"].resets_at == datetime(2026, 6, 16, 0, 0, tzinfo=UTC)
    assert not states["rpm"].exhausted


async def test_remaining_marks_an_exhausted_window(
    tracker: QuotaTracker, redis_client: FakeRedis
) -> None:
    await redis_client.set(_counter("rpd"), 100, ex=3600)

    states = {state.window: state for state in await tracker.remaining(_spec(), scope=SCOPE)}

    assert states["rpd"].exhausted
    assert states["rpd"].remaining == 0


async def test_remaining_is_empty_for_a_model_with_no_declared_limits(
    tracker: QuotaTracker,
) -> None:
    assert await tracker.remaining(_spec("mystery-model"), scope=SCOPE) == ()


# --------------------------------------------------------------------------- #
# Scoping
# --------------------------------------------------------------------------- #
async def test_two_scopes_do_not_share_a_counter(
    tracker: QuotaTracker, redis_client: FakeRedis
) -> None:
    """Phase 6 replaces the one constant at every call site with a resolver, and
    nothing else about the tracker changes — which only holds if ``scope`` really
    reaches the key."""
    await tracker.reserve(_spec(), scope=SCOPE, estimated_tokens=1, request_id="req-a")
    await tracker.reserve(_spec(), scope="user-123", estimated_tokens=1, request_id="req-b")

    assert await redis_client.get(_counter("rpm")) == "1"
    assert await redis_client.get(keys.quota("user-123", PROVIDER, MODEL, "rpm")) == "1"


# --------------------------------------------------------------------------- #
# Step 6's seam
# --------------------------------------------------------------------------- #
async def test_apply_hint_is_a_typed_signature_not_a_silent_stub(
    tracker: QuotaTracker,
) -> None:
    """The hard rule: a seam raises, because a stub that returns ``None`` looks
    exactly like a hint that was applied."""
    with pytest.raises(NotImplementedError):
        await tracker.apply_hint(_spec(), scope=SCOPE, hint=QuotaHint(requests_remaining=5))
