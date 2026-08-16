"""D8's 50/50 split (``app/quota/lanes.py``), and the direction test that makes
getting it backwards visible.

Reversing the split is invisible by inspection — a ``reserved_fraction`` of 0.5
applied to the wrong lane still produces *a* smaller number, and nothing raises.
The only way to catch it is to assert which lane shrank, which is what
``test_the_split_favors_the_answer_lane_never_the_perception_one`` and the
tracker-integration test below do.
"""

from __future__ import annotations

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
# reserve_perception — the Phase 4 seam
# --------------------------------------------------------------------------- #
async def test_reserve_perception_raises_rather_than_returning_something_plausible(
    redis_client: FakeRedis,
) -> None:
    """The hard rule: Phase 2+ seams are typed signatures raising
    ``NotImplementedError``, never a silently-passing stub."""
    scripts = LuaScriptRegistry(redis_client)
    scripts.load_dir()
    tracker = QuotaTracker(
        redis_client, scripts, _limits_config(), clock=FixedClock(NOON), headroom=0.0
    )

    with pytest.raises(NotImplementedError, match="Phase 4"):
        await lanes.reserve_perception(
            tracker,
            _spec(0.5),
            scope=SCOPE,
            estimated_tokens=100,
            request_id=REQUEST_ID,
        )


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
