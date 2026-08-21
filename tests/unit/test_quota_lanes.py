"""D8's 50/50 split (``app/quota/lanes.py``), and the direction test that makes
getting it backwards visible.

Reversing the split is invisible by inspection — a ``reserved_fraction`` of 0.5
applied to the wrong lane still produces *a* smaller number, and nothing raises.
The only way to catch it is to assert which lane shrank, which is what
``test_the_split_favors_the_answer_lane_never_the_perception_one`` and the
tracker-integration test below do.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from fakeredis.aioredis import FakeRedis

from app.cache import keys
from app.cache.client import LuaScriptRegistry
from app.config import LimitsConfig, ModelLimits
from app.core.clock import FixedClock
from app.providers.types import ModelSpec
from app.quota import lanes
from app.quota.tracker import QuotaTracker

PROVIDER = "gemini"
MODEL = "gemini-3.6-flash"
SCOPE = keys.SYSTEM_SCOPE
REQUEST_ID = "req-lane-0001"

NOON = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


def _spec(reserved_fraction: float, *, model: str = MODEL) -> ModelSpec:
    return ModelSpec(
        slot="vision",
        provider=PROVIDER,
        model=model,
        context_window=1_000_000,
        max_output_tokens=8192,
        supports_streaming=True,
        supports_vision=True,
        supports_pdf=True,
        supports_system_field=True,
        max_file_bytes=None,
        priority=0,
        reserved_fraction=reserved_fraction,
    )


def _model_limits(**overrides: Any) -> ModelLimits:
    raw: dict[str, Any] = {
        "rpm": 10,
        "rpd": 250,
        "tpm": 250_000,
        "tpd": None,
        "reset": {"rpm": "rolling_60s", "rpd": "fixed_daily_pt", "tpm": "rolling_60s"},
    }
    raw.update(overrides)
    return ModelLimits.model_validate(raw)


def _limits_config() -> LimitsConfig:
    return LimitsConfig.model_validate(
        {
            "version": 1,
            "limits": {PROVIDER: {MODEL: _model_limits().model_dump(mode="json")}},
            "gateway": {"free": {"rpm": 20, "rpd": 500}},
        }
    )


# --------------------------------------------------------------------------- #
# answer_share
# --------------------------------------------------------------------------- #
def test_a_reserved_fraction_of_half_halves_the_answer_share() -> None:
    assert lanes.answer_share(_spec(0.5)) == pytest.approx(0.5)


def test_a_reserved_fraction_of_zero_leaves_the_answer_share_whole() -> None:
    assert lanes.answer_share(_spec(0.0)) == pytest.approx(1.0)


def test_the_split_favors_the_answer_lane_never_the_perception_one() -> None:
    """Getting D8 backwards means ``answer_share`` growing as
    ``reserved_fraction`` grows. It must shrink."""
    generous = lanes.answer_share(_spec(0.1))
    stingy = lanes.answer_share(_spec(0.9))
    assert stingy < generous


# --------------------------------------------------------------------------- #
# perception_budget
# --------------------------------------------------------------------------- #
def test_perception_budget_is_the_other_half_of_each_declared_window() -> None:
    budget = lanes.perception_budget(_spec(0.5), _model_limits())
    assert budget == {"rpm": 5, "rpd": 125, "tpm": 125_000}


def test_perception_budget_drops_windows_the_provider_does_not_publish() -> None:
    """``tpd`` is ``None`` in Gemini's declared limits — dropped, never read as
    zero and never as unlimited (mirrors ``windows.declared``)."""
    budget = lanes.perception_budget(_spec(0.5), _model_limits())
    assert "tpd" not in budget


def test_perception_budget_is_empty_when_nothing_is_reserved() -> None:
    assert lanes.perception_budget(_spec(0.0), _model_limits()) == {
        "rpm": 0,
        "rpd": 0,
        "tpm": 0,
    }


# --------------------------------------------------------------------------- #
# reserve_perception — Step 5
# --------------------------------------------------------------------------- #
def _tracker(
    redis_client: FakeRedis, *, headroom: float = 0.0, limits: LimitsConfig | None = None
) -> QuotaTracker:
    scripts = LuaScriptRegistry(redis_client)
    scripts.load_dir()
    return QuotaTracker(
        redis_client, scripts, limits or _limits_config(), clock=FixedClock(NOON), headroom=headroom
    )


def _limits_config_rpd_only(*, rpd: int = 250) -> LimitsConfig:
    """Isolates the daily window from ``rpm``/``tpm`` so a test loop reserving
    dozens of times exhausts *only* the fence it means to measure — with the
    full model declared, the shared ``rpm: 10`` ceiling would block first and
    the test would be asserting the wrong window's number."""
    return LimitsConfig.model_validate(
        {
            "version": 1,
            "limits": {PROVIDER: {MODEL: {"rpd": rpd, "reset": {"rpd": "fixed_daily_pt"}}}},
            "gateway": {"free": {"rpm": 20, "rpd": 500}},
        }
    )


def _rpd_key() -> str:
    return keys.quota(SCOPE, PROVIDER, MODEL, "rpd")


def _lane_key() -> str:
    return keys.quota_perception_lane(SCOPE, PROVIDER, MODEL)


def _rpm_key() -> str:
    return keys.quota(SCOPE, PROVIDER, MODEL, "rpm")


async def test_reserve_perception_grants_exactly_the_fenced_off_daily_half(
    redis_client: FakeRedis,
) -> None:
    """``rpd: 250`` and ``reserved_fraction: 0.5`` → ``perception_budget``'s 125.
    The 126th perception reservation of the day is refused."""
    tracker = _tracker(redis_client, limits=_limits_config_rpd_only())

    for index in range(125):
        decision = await lanes.reserve_perception(
            tracker, _spec(0.5), scope=SCOPE, estimated_tokens=1, request_id=f"req-{index}"
        )
        assert decision.allowed, f"reservation {index} should fit inside the daily fence"

    blocked = await lanes.reserve_perception(
        tracker, _spec(0.5), scope=SCOPE, estimated_tokens=1, request_id="req-126th"
    )
    assert not blocked.allowed
    assert blocked.blocked_window == "rpd"


async def test_reserve_perception_charges_the_lane_key_never_rpd(
    redis_client: FakeRedis,
) -> None:
    """D26: the daily fence is ``lane:perception``, and plain ``:rpd`` is
    untouched by a perception reservation."""
    tracker = _tracker(redis_client)

    decision = await lanes.reserve_perception(
        tracker, _spec(0.5), scope=SCOPE, estimated_tokens=1, request_id=REQUEST_ID
    )

    assert decision.allowed
    assert await redis_client.get(_lane_key()) == "1"
    assert await redis_client.get(_rpd_key()) is None


async def test_a_chat_reservation_and_a_perception_reservation_touch_different_daily_keys(
    redis_client: FakeRedis,
) -> None:
    """Spending the whole answer-lane daily budget must not touch, and must not
    be starved by, the perception lane's — the two counters are independent."""
    tracker = _tracker(redis_client, limits=_limits_config_rpd_only())

    for index in range(125):
        chat = await tracker.reserve(
            _spec(0.5), scope=SCOPE, estimated_tokens=1, request_id=f"chat-{index}"
        )
        assert chat.allowed, f"chat reservation {index} should fit its own half"

    perception = await lanes.reserve_perception(
        tracker, _spec(0.5), scope=SCOPE, estimated_tokens=1, request_id="perception-0"
    )
    assert perception.allowed, "chat exhausting its half must not starve perception's"


async def test_a_chat_reservation_and_a_perception_reservation_share_the_rpm_key(
    redis_client: FakeRedis,
) -> None:
    """D26: ``rpm``/``tpm`` are *shared* counters, not fenced ones. A perception
    reservation increments the exact key a chat reservation would."""
    tracker = _tracker(redis_client)

    await tracker.reserve(_spec(0.5), scope=SCOPE, estimated_tokens=1, request_id="chat-0")
    await lanes.reserve_perception(
        tracker, _spec(0.5), scope=SCOPE, estimated_tokens=1, request_id="perception-0"
    )

    assert await redis_client.get(_rpm_key()) == "2"


async def test_a_chat_reservation_can_exhaust_the_shared_rpm_key_against_perception(
    redis_client: FakeRedis,
) -> None:
    """The mirror of D26's "starvation is a daily problem" reasoning: nothing
    stops the *minute* window from colliding — a perception reservation checks
    the shared key against the full published ceiling (10), not the halved one
    chat itself is limited to (5)."""
    tracker = _tracker(redis_client)

    await redis_client.set(_rpm_key(), 9, ex=60)

    decision = await lanes.reserve_perception(
        tracker, _spec(0.5), scope=SCOPE, estimated_tokens=1, request_id=REQUEST_ID
    )
    assert decision.allowed, "the 10th unit should still fit the full published rpm ceiling"

    blocked = await lanes.reserve_perception(
        tracker, _spec(0.5), scope=SCOPE, estimated_tokens=1, request_id="req-eleventh"
    )
    assert not blocked.allowed
    assert blocked.blocked_window == "rpm"


async def test_the_two_daily_limits_sum_to_the_published_limit_minus_headroom(
    redis_client: FakeRedis,
) -> None:
    """D8's whole point: the answer lane's effective ``rpd`` and the perception
    lane's effective ``rpd`` never overlap and never leave the published number
    unaccounted for, modulo the two independent flooring operations."""
    tracker = _tracker(redis_client, headroom=0.1, limits=_limits_config_rpd_only())
    spec = _spec(0.5)

    answer_count = 0
    for index in range(300):
        decision = await tracker.reserve(
            spec, scope=SCOPE, estimated_tokens=1, request_id=f"chat-{index}"
        )
        if not decision.allowed:
            break
        answer_count += 1

    perception_count = 0
    for index in range(300):
        decision = await lanes.reserve_perception(
            tracker, spec, scope=SCOPE, estimated_tokens=1, request_id=f"perception-{index}"
        )
        if not decision.allowed:
            break
        perception_count += 1

    published_rpd = _model_limits().rpd
    assert published_rpd is not None
    assert answer_count + perception_count == pytest.approx(published_rpd * 0.9, abs=2)


async def test_reserve_perception_reserves_nothing_for_a_model_with_no_declared_limits(
    redis_client: FakeRedis,
) -> None:
    tracker = _tracker(redis_client)

    decision = await lanes.reserve_perception(
        tracker,
        _spec(0.5, model="mystery-model"),
        scope=SCOPE,
        estimated_tokens=1,
        request_id=REQUEST_ID,
    )

    assert decision.allowed
    assert decision.reservation is not None
    assert decision.reservation.windows == ()


async def test_fifty_concurrent_perception_reserves_against_a_daily_fence_of_ten_grant_exactly_ten(
    redis_client: FakeRedis,
) -> None:
    """The interview answer, run against the lane's own counter: a
    reserved_fraction chosen so the daily fence lands on exactly 10."""
    tracker = _tracker(redis_client, limits=_limits_config_rpd_only())
    spec = _spec(0.04)  # floor(250 * 0.04) == 10

    decisions = await asyncio.gather(
        *(
            lanes.reserve_perception(
                tracker, spec, scope=SCOPE, estimated_tokens=1, request_id=f"req-{index}"
            )
            for index in range(50)
        )
    )

    assert sum(1 for decision in decisions if decision.allowed) == 10
    assert await redis_client.get(_lane_key()) == "10"


async def test_commit_perception_delegates_to_the_trackers_own_commit(
    redis_client: FakeRedis,
) -> None:
    tracker = _tracker(redis_client)
    decision = await lanes.reserve_perception(
        tracker, _spec(0.5), scope=SCOPE, estimated_tokens=1000, request_id=REQUEST_ID
    )
    assert decision.reservation is not None

    await lanes.commit_perception(tracker, decision.reservation, tokens_in=100, tokens_out=50)

    assert await redis_client.get(keys.quota(SCOPE, PROVIDER, MODEL, "tpm")) == "150"


async def test_release_perception_delegates_to_the_trackers_own_release(
    redis_client: FakeRedis,
) -> None:
    tracker = _tracker(redis_client)
    decision = await lanes.reserve_perception(
        tracker, _spec(0.5), scope=SCOPE, estimated_tokens=1, request_id=REQUEST_ID
    )
    assert decision.reservation is not None

    await lanes.release_perception(tracker, decision.reservation)

    assert await redis_client.get(_lane_key()) == "0"
    assert not await redis_client.exists(keys.quota_reservation(SCOPE, PROVIDER, MODEL, REQUEST_ID))


# --------------------------------------------------------------------------- #
# Direction, proven against the real reservation path
# --------------------------------------------------------------------------- #
async def test_the_tracker_reserves_against_the_shrunk_answer_budget_not_the_full_one(
    redis_client: FakeRedis,
) -> None:
    """``QuotaTracker._budget`` is the one call site D8's split reaches. A
    ``reserved_fraction`` of 0.5 must make a request for the *eleventh* unit of
    a published-20 ``rpm`` window fail — the answer lane only ever sees 10 — and
    a ``reserved_fraction`` of 0.0 must let all 20 through."""
    scripts = LuaScriptRegistry(redis_client)
    scripts.load_dir()
    limits = LimitsConfig.model_validate(
        {
            "version": 1,
            "limits": {
                PROVIDER: {
                    MODEL: {
                        "rpm": 20,
                        "reset": {"rpm": "rolling_60s"},
                    }
                }
            },
            "gateway": {"free": {"rpm": 20, "rpd": 500}},
        }
    )
    halved = QuotaTracker(redis_client, scripts, limits, clock=FixedClock(NOON), headroom=0.0)

    for i in range(10):
        decision = await halved.reserve(
            _spec(0.5), scope=SCOPE, estimated_tokens=1, request_id=f"req-{i}"
        )
        assert decision.allowed, f"reservation {i} should fit inside the halved rpm budget"

    eleventh = await halved.reserve(
        _spec(0.5), scope=SCOPE, estimated_tokens=1, request_id="req-eleventh"
    )
    assert not eleventh.allowed
    assert eleventh.blocked_window == "rpm"


async def test_a_zero_reserved_fraction_leaves_the_full_published_limit_spendable(
    redis_client: FakeRedis,
) -> None:
    scripts = LuaScriptRegistry(redis_client)
    scripts.load_dir()
    limits = LimitsConfig.model_validate(
        {
            "version": 1,
            "limits": {PROVIDER: {MODEL: {"rpm": 20, "reset": {"rpm": "rolling_60s"}}}},
            "gateway": {"free": {"rpm": 20, "rpd": 500}},
        }
    )
    whole = QuotaTracker(redis_client, scripts, limits, clock=FixedClock(NOON), headroom=0.0)

    for i in range(20):
        decision = await whole.reserve(
            _spec(0.0), scope=SCOPE, estimated_tokens=1, request_id=f"req-{i}"
        )
        assert decision.allowed, f"reservation {i} should fit inside the unreserved rpm budget"

    twenty_first = await whole.reserve(
        _spec(0.0), scope=SCOPE, estimated_tokens=1, request_id="req-21st"
    )
    assert not twenty_first.allowed
