"""The EWMA latency table, and the two properties `auto` depends on.

Small surface, two bugs worth defending against.

**A snapshot must be detached.** ``selection.candidates()`` is documented as pure,
and the whole reason the snapshot is passed in rather than read live is so a
candidate list cannot reorder underneath a retry loop midway through walking it.
A snapshot that aliased the table would give that away silently.

**The threshold must actually hold something back.** ``ranking_for`` returning a
number too early is what makes the first request after a deploy arbitrary instead
of reproducing config order, and that is a bug nobody reports because the gateway
still works — it just routes somewhere different every restart.

The successful-attempts-only rule lives one layer up, in the router, so its test
is in ``test_router.py`` where the failure paths are.
"""

from __future__ import annotations

import pytest

from app.providers.types import ModelSpec
from app.usage.metrics import COMPLETE, MIN_SAMPLES, STREAM, LatencyTable

PROVIDER = "groq"
MODEL = "llama-3.3-70b-versatile"


def spec(provider: str = PROVIDER, model: str = MODEL) -> ModelSpec:
    return ModelSpec(
        slot="general",
        provider=provider,
        model=model,
        context_window=131072,
        max_output_tokens=8192,
        supports_streaming=True,
        supports_vision=False,
        supports_pdf=False,
        supports_system_field=False,
        max_file_bytes=None,
        priority=0,
    )


def warm(table: LatencyTable, ms: float, *, samples: int = MIN_SAMPLES) -> None:
    """Push a candidate over the evidence threshold at a steady latency."""
    for _ in range(samples):
        table.record(PROVIDER, MODEL, COMPLETE, ms)


# --------------------------------------------------------------------------- #
# The average
# --------------------------------------------------------------------------- #
def test_the_first_observation_is_the_average() -> None:
    """No prior means nothing to weight against, so seeding with the observation
    beats seeding with zero — which would make a candidate's first measurement
    read as instantaneous."""
    table = LatencyTable()
    table.record(PROVIDER, MODEL, COMPLETE, 400.0)

    sample = table.snapshot(COMPLETE).entries[(PROVIDER, MODEL)]

    assert sample.ewma_ms == pytest.approx(400.0)
    assert sample.samples == 1


def test_the_average_moves_toward_the_newest_observation() -> None:
    table = LatencyTable(alpha=0.5)
    table.record(PROVIDER, MODEL, COMPLETE, 100.0)
    table.record(PROVIDER, MODEL, COMPLETE, 300.0)

    sample = table.snapshot(COMPLETE).entries[(PROVIDER, MODEL)]

    assert sample.ewma_ms == pytest.approx(200.0)
    assert sample.samples == 2


def test_a_sustained_change_converges() -> None:
    """A provider that degrades has to be demoted within a handful of requests,
    or the ranking is describing last week."""
    table = LatencyTable()
    for _ in range(20):
        table.record(PROVIDER, MODEL, COMPLETE, 100.0)
    for _ in range(20):
        table.record(PROVIDER, MODEL, COMPLETE, 900.0)

    assert table.snapshot(COMPLETE).entries[(PROVIDER, MODEL)].ewma_ms == pytest.approx(
        900.0, rel=0.01
    )


def test_an_invalid_alpha_is_refused_at_construction() -> None:
    """A zero alpha would freeze every series at its first observation, which
    looks like a working table and is not one."""
    with pytest.raises(ValueError, match="alpha"):
        LatencyTable(alpha=0.0)


# --------------------------------------------------------------------------- #
# Two series, never one
# --------------------------------------------------------------------------- #
def test_streaming_and_non_streaming_are_separate_series() -> None:
    """Total streaming latency is dominated by output length, so mixing the two
    would rank streams by whichever model is most terse rather than fastest."""
    table = LatencyTable()
    table.record(PROVIDER, MODEL, COMPLETE, 900.0)
    table.record(PROVIDER, MODEL, STREAM, 90.0)

    assert table.snapshot(COMPLETE).entries[(PROVIDER, MODEL)].ewma_ms == pytest.approx(900.0)
    assert table.snapshot(STREAM).entries[(PROVIDER, MODEL)].ewma_ms == pytest.approx(90.0)


def test_a_snapshot_carries_only_its_own_mode() -> None:
    table = LatencyTable()
    table.record(PROVIDER, MODEL, STREAM, 90.0)

    assert table.snapshot(COMPLETE).entries == {}


# --------------------------------------------------------------------------- #
# The snapshot
# --------------------------------------------------------------------------- #
def test_a_snapshot_does_not_move_when_the_table_does() -> None:
    """The guarantee `selection.candidates()`'s purity rests on."""
    table = LatencyTable()
    warm(table, 100.0)
    taken = table.snapshot(COMPLETE)

    for _ in range(50):
        table.record(PROVIDER, MODEL, COMPLETE, 5000.0)

    assert taken.ranking_for(spec()) == pytest.approx(100.0)


def test_a_candidate_under_the_threshold_is_unranked() -> None:
    """`None` means "no opinion", and the caller must leave the candidate where
    the config put it rather than sorting it to either end."""
    table = LatencyTable()
    warm(table, 100.0, samples=MIN_SAMPLES - 1)

    assert table.snapshot(COMPLETE).ranking_for(spec()) is None


def test_the_threshold_admits_a_candidate_the_moment_it_is_met() -> None:
    table = LatencyTable()
    warm(table, 100.0)

    assert table.snapshot(COMPLETE).ranking_for(spec()) == pytest.approx(100.0)


def test_an_unmeasured_candidate_is_unranked() -> None:
    table = LatencyTable()
    warm(table, 100.0)

    assert table.snapshot(COMPLETE).ranking_for(spec(model="something-else")) is None


def test_a_negative_observation_is_clamped_rather_than_trusted() -> None:
    """A clock that went backwards should not manufacture the fastest provider in
    the fleet — the inversion bug by another route."""
    table = LatencyTable()
    table.record(PROVIDER, MODEL, COMPLETE, -50.0)

    assert table.snapshot(COMPLETE).entries[(PROVIDER, MODEL)].ewma_ms == pytest.approx(0.0)
