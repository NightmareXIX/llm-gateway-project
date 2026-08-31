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

**And, below, D49's exposition** — where the unit under test is a *string*. Both
ways a hand-rolled exporter goes wrong are silent ones: a non-cumulative
histogram parses fine and charts wrong, and a label that quietly grows a
``user_id`` breaks the scraper's cardinality long after the commit that did it.
Each gets an assertion here rather than a paragraph.
"""

from __future__ import annotations

import re

import pytest

from app.providers.types import ModelSpec
from app.routing.circuit_breaker import CircuitBreaker
from app.usage.metrics import (
    BREAKER_FAIL_OPEN_TOTAL,
    BREAKER_STATE,
    COMPLETE,
    DURATION_MS,
    MIN_SAMPLES,
    NO_KEY_POOL,
    QUOTA_REMAINING,
    REQUESTS_TOTAL,
    STREAM,
    UNKNOWN_LABEL,
    BreakerGauge,
    LatencyTable,
    MetricsRegistry,
    QuotaGauge,
    render_exposition,
)

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


# --------------------------------------------------------------------------- #
# D49 — the exposition format
#
# The unit under test is a string, and the two ways a hand-rolled exporter goes
# wrong are both silent: a non-cumulative histogram parses fine and charts
# wrong, and a label that quietly becomes a `user_id` breaks the scraper's
# cardinality long after the commit that did it. Both get an assertion here.
# --------------------------------------------------------------------------- #
UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)

LABEL_PATTERN = re.compile(r"\{(.*)\}")


def label_values(body: str) -> list[str]:
    """Every label value in every sample line."""
    values: list[str] = []
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        match = LABEL_PATTERN.search(line)
        if match is None:
            continue
        values.extend(re.findall(r'="([^"]*)"', match.group(1)))
    return values


def samples(body: str, name: str) -> dict[str, float]:
    """``{label_block: value}`` for one metric name, exactly."""
    found: dict[str, float] = {}
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        head, _, value = line.rpartition(" ")
        metric, _, labels = head.partition("{")
        if metric == name:
            found[labels.rstrip("}")] = float(value)
    return found


def test_every_family_declares_its_type() -> None:
    """A sample with no ``# TYPE`` above it is scraped as untyped, which turns a
    counter into a gauge and makes ``rate()`` meaningless."""
    registry = MetricsRegistry()
    registry.record_request(provider=PROVIDER, model=MODEL, status="ok", key_pool="shared")
    registry.observe_duration(provider=PROVIDER, mode=COMPLETE, ms=120.0)
    registry.record_breaker_fail_open(PROVIDER, MODEL)

    body = render_exposition(
        registry,
        breakers=[BreakerGauge(provider=PROVIDER, model=MODEL, state="closed")],
        quota=[QuotaGauge(provider=PROVIDER, model=MODEL, window="rpd", remaining=900)],
    )

    for name, kind in (
        (REQUESTS_TOTAL, "counter"),
        (DURATION_MS, "histogram"),
        (BREAKER_FAIL_OPEN_TOTAL, "counter"),
        (BREAKER_STATE, "gauge"),
        (QUOTA_REMAINING, "gauge"),
    ):
        assert f"# HELP {name} " in body
        assert f"# TYPE {name} {kind}" in body


def test_a_body_ends_in_a_newline() -> None:
    """Prometheus treats a body that does not as a parse error, not as an empty
    result — so a fresh process with no samples still has to end in one."""
    assert render_exposition(MetricsRegistry()).endswith("\n")


def test_a_counter_counts_each_outcome_once_under_its_own_labels() -> None:
    registry = MetricsRegistry()
    registry.record_request(provider=PROVIDER, model=MODEL, status="ok", key_pool="shared")
    registry.record_request(provider=PROVIDER, model=MODEL, status="ok", key_pool="shared")
    registry.record_request(provider=PROVIDER, model=MODEL, status="error", key_pool="private")

    found = samples(render_exposition(registry), REQUESTS_TOTAL)

    assert found[f'provider="{PROVIDER}",model="{MODEL}",status="ok",key_pool="shared"'] == 2
    assert found[f'provider="{PROVIDER}",model="{MODEL}",status="error",key_pool="private"'] == 1


def test_an_outcome_that_never_reached_a_candidate_labels_itself_unknown() -> None:
    """A stream that failed on an open breaker before resolving anything has no
    provider. ``unknown`` is one bounded extra label value; an empty string reads
    as a bug, and dropping the sample loses the failure entirely."""
    registry = MetricsRegistry()
    registry.record_request(provider=None, model=None, status="error", key_pool=None)

    found = samples(render_exposition(registry), REQUESTS_TOTAL)

    assert (
        found[
            f'provider="{UNKNOWN_LABEL}",model="{UNKNOWN_LABEL}",'
            f'status="error",key_pool="{NO_KEY_POOL}"'
        ]
        == 1
    )


def test_the_histogram_is_cumulative_and_its_last_bucket_equals_the_count() -> None:
    """The one failure mode that parses fine and charts wrong."""
    registry = MetricsRegistry(buckets=(10.0, 100.0, 1000.0))
    for ms in (5.0, 50.0, 50.0, 500.0, 90_000.0):
        registry.observe_duration(provider=PROVIDER, mode=COMPLETE, ms=ms)

    body = render_exposition(registry)
    buckets = samples(body, f"{DURATION_MS}_bucket")
    base = f'provider="{PROVIDER}",mode="complete"'

    assert buckets[f'{base},le="10"'] == 1
    assert buckets[f'{base},le="100"'] == 3
    assert buckets[f'{base},le="1000"'] == 4
    assert buckets[f'{base},le="+Inf"'] == 5

    counts = samples(body, f"{DURATION_MS}_count")
    sums = samples(body, f"{DURATION_MS}_sum")
    assert counts[base] == 5
    assert buckets[f'{base},le="+Inf"'] == counts[base]
    assert sums[base] == 5.0 + 50.0 + 50.0 + 500.0 + 90_000.0


def test_the_two_modes_are_separate_series() -> None:
    """Total streaming latency is dominated by output length, so mixing it with
    the non-streaming series makes both numbers mean nothing — the same reason
    ``LatencyTable`` keeps two series per candidate."""
    registry = MetricsRegistry()
    registry.observe_duration(provider=PROVIDER, mode=COMPLETE, ms=100.0)
    registry.observe_duration(provider=PROVIDER, mode=STREAM, ms=100.0)

    counts = samples(render_exposition(registry), f"{DURATION_MS}_count")

    assert counts[f'provider="{PROVIDER}",mode="complete"'] == 1
    assert counts[f'provider="{PROVIDER}",mode="stream"'] == 1


def test_a_label_value_with_a_quote_or_a_backslash_is_escaped() -> None:
    """A model name is config, and config is edited by hand. One unescaped quote
    makes the whole scrape unparseable rather than one sample wrong."""
    registry = MetricsRegistry()
    registry.record_request(provider='we"ird', model="back\\slash", status="ok", key_pool="shared")

    body = render_exposition(registry)

    assert 'provider="we\\"ird"' in body
    assert 'model="back\\\\slash"' in body


def test_the_breaker_gauge_encodes_its_three_states_in_order_of_badness() -> None:
    registry = MetricsRegistry()
    body = render_exposition(
        registry,
        breakers=[
            BreakerGauge(provider="a", model="m", state="closed"),
            BreakerGauge(provider="b", model="m", state="half_open"),
            BreakerGauge(provider="c", model="m", state="open"),
        ],
    )
    found = samples(body, BREAKER_STATE)

    assert found['provider="a",model="m"'] == 0
    assert found['provider="b",model="m"'] == 1
    assert found['provider="c",model="m"'] == 2


def test_an_unrecognized_breaker_state_renders_no_sample() -> None:
    """Rather than a default of 0, which would draw a healthy line for a state
    nobody has taught this module about yet."""
    body = render_exposition(
        MetricsRegistry(),
        breakers=[BreakerGauge(provider="a", model="m", state="who_knows")],
    )
    assert samples(body, BREAKER_STATE) == {}


def test_gauges_are_omitted_entirely_when_there_are_none() -> None:
    """What a Redis failure hands in (D49). The counters still render — a metrics
    endpoint that goes dark during an incident is dark exactly when it matters."""
    registry = MetricsRegistry()
    registry.record_request(provider=PROVIDER, model=MODEL, status="ok", key_pool="shared")

    body = render_exposition(registry)

    assert BREAKER_STATE not in body
    assert QUOTA_REMAINING not in body
    assert REQUESTS_TOTAL in body


def test_no_label_anywhere_looks_like_a_uuid() -> None:
    """The privacy rule, asserted rather than merely written down (trap 10).

    Unbounded label cardinality is how a metrics endpoint takes down the thing
    scraping it, and the two identifiers a gateway is tempted to label with —
    ``user_id`` and ``conversation_id`` — are also the two that make the endpoint
    a privacy surface. This is the assertion that keeps a future label from
    quietly becoming one.
    """
    registry = MetricsRegistry()
    registry.record_request(provider=PROVIDER, model=MODEL, status="ok", key_pool="private")
    registry.observe_duration(provider=PROVIDER, mode=STREAM, ms=42.0)
    registry.record_breaker_fail_open(PROVIDER, MODEL)

    body = render_exposition(
        registry,
        breakers=[BreakerGauge(provider=PROVIDER, model=MODEL, state="open")],
        quota=[QuotaGauge(provider=PROVIDER, model=MODEL, window="rpm", remaining=3)],
    )

    assert UUID_PATTERN.search(body) is None
    for value in label_values(body):
        assert UUID_PATTERN.search(value) is None


def test_two_scrapes_of_an_unchanged_registry_are_byte_identical() -> None:
    """Dict order is insertion order, so a body that is not explicitly sorted
    would depend on which candidate answered first — which makes a hand diff of
    two scrapes useless."""
    registry = MetricsRegistry()
    for provider in ("openrouter", "groq", "gemini"):
        registry.record_request(provider=provider, model=MODEL, status="ok", key_pool="shared")

    assert render_exposition(registry) == render_exposition(registry)


def test_a_registry_with_no_samples_still_renders_the_counter_families() -> None:
    """A freshly booted worker is scraped like any other, and a family that
    appears only once it has data is a family whose ``rate()`` starts with a
    gap."""
    body = render_exposition(MetricsRegistry())

    assert f"# TYPE {REQUESTS_TOTAL} counter" in body
    assert f"# TYPE {DURATION_MS} histogram" in body
    assert samples(body, REQUESTS_TOTAL) == {}


# --------------------------------------------------------------------------- #
# The breaker's fail-open counter (ADR-010, finally read)
# --------------------------------------------------------------------------- #
async def test_a_breaker_without_redis_counts_its_guess() -> None:
    """A fail-open decision produces no user-visible symptom — the request still
    succeeds — so this counter is the only number that says the breaker has
    stopped knowing anything."""

    class _DeadRedis:
        async def hgetall(self, key: str) -> dict[str, str]:
            raise ConnectionError("down")

    registry = MetricsRegistry()
    breaker = CircuitBreaker(_DeadRedis(), metrics=registry)  # type: ignore[arg-type]

    decision = await breaker.peek(PROVIDER, MODEL)

    assert decision.degraded is True
    assert registry.breaker_fail_open_counts()[(PROVIDER, MODEL)] == 1
    assert f'{BREAKER_FAIL_OPEN_TOTAL}{{provider="{PROVIDER}",model="{MODEL}"}} 1' in (
        render_exposition(registry)
    )


async def test_a_breaker_with_no_registry_still_works() -> None:
    """``None`` keeps every pre-Phase-7 caller honest: a breaker built by hand in
    a test counts nothing and needs no edit."""

    class _DeadRedis:
        async def hgetall(self, key: str) -> dict[str, str]:
            raise ConnectionError("down")

    breaker = CircuitBreaker(_DeadRedis())  # type: ignore[arg-type]

    assert (await breaker.peek(PROVIDER, MODEL)).allowed is True
