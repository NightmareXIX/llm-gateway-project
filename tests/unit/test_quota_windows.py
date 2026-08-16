"""``quota/windows.py`` — pure reset-semantics tests against a frozen clock.

No Redis and no real clock: every function under test takes ``now`` as an
argument, so DST behaviour — the thing most likely to be silently wrong — is a
table test rather than something that only fails once a year in production.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import ModelLimits, ResetPolicy
from app.quota import windows

UTC_NOON = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# resets_at / ttl_s — the three reset kinds
# --------------------------------------------------------------------------- #
def test_rolling_60s_resets_sixty_seconds_out() -> None:
    assert windows.resets_at("rolling_60s", now=UTC_NOON) == UTC_NOON + timedelta(seconds=60)
    assert windows.ttl_s("rolling_60s", now=UTC_NOON) == 60


def test_fixed_daily_utc_resets_at_next_midnight_utc() -> None:
    now = datetime(2026, 6, 15, 23, 59, 0, tzinfo=UTC)
    assert windows.resets_at("fixed_daily_utc", now=now) == datetime(
        2026, 6, 16, 0, 0, 0, tzinfo=UTC
    )


def test_fixed_daily_utc_at_exactly_midnight_rolls_to_the_next_day() -> None:
    now = datetime(2026, 6, 16, 0, 0, 0, tzinfo=UTC)
    assert windows.resets_at("fixed_daily_utc", now=now) == datetime(
        2026, 6, 17, 0, 0, 0, tzinfo=UTC
    )


def test_fixed_daily_pt_resets_at_local_midnight_converted_to_utc() -> None:
    # 2026-06-15 12:00 UTC is 05:00 Pacific (PDT, UTC-7 in June).
    resets = windows.resets_at("fixed_daily_pt", now=UTC_NOON)
    assert resets == datetime(2026, 6, 16, 7, 0, 0, tzinfo=UTC)


def test_ttl_s_is_always_at_least_one_second() -> None:
    just_before_midnight = datetime(2026, 6, 15, 23, 59, 59, 500000, tzinfo=UTC)
    assert windows.ttl_s("fixed_daily_utc", now=just_before_midnight) >= 1


# --------------------------------------------------------------------------- #
# fixed_daily_pt — DST transitions
# --------------------------------------------------------------------------- #
# US Pacific: "spring forward" 2027-03-14 02:00 -> 03:00 (23h day, PST -> PDT),
# "fall back" 2027-11-07 02:00 -> 01:00 (25h day, PDT -> PST).
def test_fixed_daily_pt_spans_23_hours_across_spring_forward() -> None:
    before = datetime(2027, 3, 13, 10, 0, 0, tzinfo=UTC)  # 2027-03-13 02:00 PST
    first_reset = windows.resets_at("fixed_daily_pt", now=before)
    second_reset = windows.resets_at("fixed_daily_pt", now=first_reset)
    assert second_reset - first_reset == timedelta(hours=23)


def test_fixed_daily_pt_spans_25_hours_across_fall_back() -> None:
    before = datetime(2027, 11, 6, 9, 0, 0, tzinfo=UTC)  # 2027-11-06 01:00 PST
    first_reset = windows.resets_at("fixed_daily_pt", now=before)
    second_reset = windows.resets_at("fixed_daily_pt", now=first_reset)
    assert second_reset - first_reset == timedelta(hours=25)


def test_fixed_daily_pt_never_produces_a_24_hour_gap_in_transition_weeks() -> None:
    before = datetime(2027, 3, 13, 10, 0, 0, tzinfo=UTC)
    first_reset = windows.resets_at("fixed_daily_pt", now=before)
    second_reset = windows.resets_at("fixed_daily_pt", now=first_reset)
    assert second_reset - first_reset != timedelta(hours=24)


# --------------------------------------------------------------------------- #
# declared — null windows dropped, never read as unlimited or zero
# --------------------------------------------------------------------------- #
def test_declared_drops_null_windows() -> None:
    limits = ModelLimits(
        rpm=10,
        rpd=250,
        tpm=250_000,
        tpd=None,
        reset=ResetPolicy(rpm="rolling_60s", rpd="fixed_daily_pt", tpm="rolling_60s"),
    )
    specs = windows.declared(limits)
    assert {spec.window for spec in specs} == {"rpm", "rpd", "tpm"}


def test_declared_preserves_limit_and_reset_per_window() -> None:
    limits = ModelLimits(
        rpm=30,
        rpd=1000,
        tpm=None,
        tpd=None,
        reset=ResetPolicy(rpm="rolling_60s", rpd="fixed_daily_utc"),
    )
    specs = {spec.window: spec for spec in windows.declared(limits)}
    assert specs["rpm"].limit == 30
    assert specs["rpm"].reset == "rolling_60s"
    assert specs["rpm"].cost_is_tokens is False
    assert specs["rpd"].limit == 1000
    assert specs["rpd"].reset == "fixed_daily_utc"


def test_declared_marks_token_windows_as_token_cost() -> None:
    limits = ModelLimits(
        rpm=None,
        rpd=None,
        tpm=250_000,
        tpd=100_000,
        reset=ResetPolicy(tpm="rolling_60s", tpd="fixed_daily_utc"),
    )
    specs = {spec.window: spec for spec in windows.declared(limits)}
    assert specs["tpm"].cost_is_tokens is True
    assert specs["tpd"].cost_is_tokens is True


def test_declared_with_all_windows_null_returns_empty() -> None:
    limits = ModelLimits(rpm=None, rpd=None, tpm=None, tpd=None)
    assert windows.declared(limits) == ()


def test_declared_raises_when_limit_has_no_matching_reset() -> None:
    limits = ModelLimits(rpm=30, reset=ResetPolicy())  # rpm limit, no reset kind
    with pytest.raises(ValueError, match="rpm"):
        windows.declared(limits)


# --------------------------------------------------------------------------- #
# WindowState
# --------------------------------------------------------------------------- #
def test_window_state_remaining_and_exhausted() -> None:
    state = windows.WindowState(window="rpm", limit=10, used=7, resets_at=UTC_NOON)
    assert state.remaining == 3
    assert state.exhausted is False


def test_window_state_exhausted_at_the_limit() -> None:
    state = windows.WindowState(window="rpm", limit=10, used=10, resets_at=UTC_NOON)
    assert state.exhausted is True
    assert state.remaining == 0


def test_window_state_remaining_never_goes_negative() -> None:
    # A hint (D18) or a stale reservation can push `used` past `limit`.
    state = windows.WindowState(window="rpm", limit=10, used=13, resets_at=UTC_NOON)
    assert state.remaining == 0


# --------------------------------------------------------------------------- #
# sliding_count — D20's two-bucket interpolation
# --------------------------------------------------------------------------- #
def test_sliding_count_at_window_start_counts_previous_bucket_in_full() -> None:
    assert windows.sliding_count(10, 4, elapsed_fraction=0.0) == 14.0


def test_sliding_count_halfway_through_counts_half_the_previous_bucket() -> None:
    assert windows.sliding_count(10, 4, elapsed_fraction=0.5) == 9.0


def test_sliding_count_at_window_end_ignores_previous_bucket() -> None:
    assert windows.sliding_count(10, 4, elapsed_fraction=1.0) == 4.0


def test_sliding_count_rejects_fraction_outside_unit_interval() -> None:
    with pytest.raises(ValueError, match="elapsed_fraction"):
        windows.sliding_count(10, 4, elapsed_fraction=1.5)
    with pytest.raises(ValueError, match="elapsed_fraction"):
        windows.sliding_count(10, 4, elapsed_fraction=-0.1)
