"""The in-process latency table `auto` ranks candidates with (D11).

**What it measures, and the one rule that matters.** Only attempts that produced
usable content update a series. A provider that 429s in 80ms is the fastest thing
in the fleet by wall clock and the worst possible choice, so feeding failures into
the average would make ``auto`` actively seek out whatever is most broken. The
symptom of getting this wrong is not an error — it is a gateway that gets *worse*
the longer it runs, which is a miserable thing to diagnose after the fact. The
router calls :meth:`LatencyTable.record` on the success path and nowhere else, and
``tests/unit/test_router.py`` asserts it.

**Two series per ``(provider, model)``, not one.** Streaming ranks on time to
first token; the non-streaming path ranks on total latency. They are not
comparable numbers — total streaming latency is dominated by output length, so
ranking streams by it just prefers whichever model is most terse. A request ranks
against the series matching its own mode, which is why :meth:`snapshot` takes a
mode and hands back only that half of the table.

**EWMA, not a true p50.** A rolling percentile needs a retained sample window per
candidate; an exponentially-weighted mean needs one float and answers the same
question well enough to sort three items. Revisit if the ordering ever looks
unstable.

**Why this is not in Redis.** Contract C is frozen. Cross-instance latency sharing
would need a new key format, and that is a change worth making with sign-off
rather than as a side effect of Phase 2 Step 4. Two workers on one instance
converge on their own within a few dozen requests, and staleness is
self-correcting: the only way to update a candidate's number is to actually use
it. ADR-014 records the trade.

The table is mutable and process-local; a :class:`LatencySnapshot` taken from it
is not. That split is what lets ``selection.candidates()`` stay a pure function of
(registry, request, snapshot) while the numbers underneath it keep moving.

**The second half of this module is Phase 7's** (D49): :class:`MetricsRegistry`
and :func:`render_exposition`, which back ``GET /metrics``. It lives here rather
than in a new file because it is the same kind of number as the table above —
process-local, unshared, and justified by ADR-014's reasoning rather than in
spite of it — and because the breaker's fail-open counter this docstring spent
five phases promising finally has a reader.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from app.providers.types import ModelSpec

type RoutingMode = Literal["complete", "stream"]
"""Which series an observation belongs to. ``stream`` means TTFT."""

COMPLETE: Final[RoutingMode] = "complete"
STREAM: Final[RoutingMode] = "stream"

EWMA_ALPHA: Final = 0.3
"""Weight of the newest observation.

0.3 puts roughly half the weight on the last two samples, so a provider that
degrades is demoted within a handful of requests without a single slow response
reordering the fleet.
"""

MIN_SAMPLES: Final = 5
"""Successful samples before a candidate is ranked at all.

D11's "rank only where there is evidence" guardrail. Below it a candidate keeps
its config position, so a cold process behaves exactly like the config-order
design until it has learned something — which is also what makes the first
request after a deploy predictable rather than arbitrary.
"""

type _SeriesKey = tuple[str, str, RoutingMode]


@dataclass(frozen=True, slots=True)
class LatencySample:
    """One candidate's number, and how much evidence is behind it."""

    ewma_ms: float
    samples: int


@dataclass(frozen=True, slots=True)
class LatencySnapshot:
    """One mode's view of the table, taken once per request and passed down.

    Taken once and *passed* rather than read live, so a candidate list cannot
    reorder underneath a retry loop that is midway through walking it.
    """

    mode: RoutingMode
    entries: Mapping[tuple[str, str], LatencySample]
    min_samples: int = MIN_SAMPLES

    def ranking_for(self, spec: ModelSpec) -> float | None:
        """This candidate's EWMA in milliseconds, or ``None`` if it is unranked.

        ``None`` is not "slow" — it is "no opinion", and the caller must leave the
        candidate where the config put it rather than sorting it to either end.
        """
        sample = self.entries.get(spec.key)
        if sample is None or sample.samples < self.min_samples:
            return None
        return sample.ewma_ms


EMPTY_SNAPSHOT: Final = LatencySnapshot(mode=COMPLETE, entries=MappingProxyType({}))
"""A snapshot that ranks nothing — config order, exactly. Handy in tests."""


class LatencyTable:
    """Per-process EWMA latency, keyed by ``(provider, model, mode)``.

    One instance lives on ``app.state`` for the lifetime of the process. Nothing
    here is async and nothing awaits: an EWMA update is three floating-point
    operations, so a lock would cost more than the race it prevents, and the
    worst a lost update can do is delay a reordering by one request.
    """

    def __init__(self, *, alpha: float = EWMA_ALPHA, min_samples: int = MIN_SAMPLES) -> None:
        if not 0.0 < alpha <= 1.0:
            raise ValueError(f"alpha must be in (0, 1]; got {alpha}")
        self._alpha = alpha
        self._min_samples = min_samples
        self._series: dict[_SeriesKey, LatencySample] = {}

    def record(self, provider: str, model: str, mode: RoutingMode, ms: float) -> None:
        """Fold one **successful** attempt into its series.

        Callers must not report failures here. See the module docstring: a fast
        failure is the fastest measurement in the system and ranking on it inverts
        the whole mechanism.
        """
        observation = max(0.0, ms)
        key: _SeriesKey = (provider, model, mode)
        previous = self._series.get(key)

        if previous is None:
            self._series[key] = LatencySample(ewma_ms=observation, samples=1)
            return

        ewma = self._alpha * observation + (1.0 - self._alpha) * previous.ewma_ms
        self._series[key] = LatencySample(ewma_ms=ewma, samples=previous.samples + 1)

    def snapshot(self, mode: RoutingMode) -> LatencySnapshot:
        """A detached, read-only view of one mode's series.

        Detached on purpose: a snapshot handed to ``selection.candidates()`` must
        not move while the router is walking the list it produced.
        """
        entries = {
            (provider, model): sample
            for (provider, model, series_mode), sample in self._series.items()
            if series_mode == mode
        }
        return LatencySnapshot(
            mode=mode,
            entries=MappingProxyType(entries),
            min_samples=self._min_samples,
        )


# --------------------------------------------------------------------------- #
# D49 — `GET /metrics`: the counters, the histogram, and the exposition format
# --------------------------------------------------------------------------- #
DURATION_BUCKETS_MS: Final[tuple[float, ...]] = (
    25.0,
    50.0,
    100.0,
    250.0,
    500.0,
    1000.0,
    2500.0,
    5000.0,
    10_000.0,
    30_000.0,
    60_000.0,
)
"""Fixed bucket bounds, in milliseconds, for ``gateway_request_duration_ms``.

Fixed rather than configurable: changing a histogram's bounds between scrapes
makes the series before and after unmergeable, so this is a decision to make
once and leave alone. The range is chosen for what this gateway actually sees —
a cached replay lands in the first bucket, a short completion in the middle, and
a long streamed answer near the top, with 60s as the last finite bound because
anything slower has already hit an adapter timeout.
"""

NO_KEY_POOL: Final = "none"
"""``key_pool`` label for an outcome that spent no credential at all — a cache
hit, or a failure that never resolved one. Prometheus has no null label value,
and an empty string reads as "we forgot to set it"."""

UNKNOWN_LABEL: Final = "unknown"
"""``provider``/``model`` label when a turn failed before any candidate was
reached. A bounded extra value, not free text: there is exactly one of it."""

BREAKER_STATE_CODES: Final[Mapping[str, int]] = MappingProxyType(
    {"closed": 0, "half_open": 1, "open": 2}
)
"""``gateway_breaker_state``'s numeric encoding, ordered by badness.

A gauge carries a number, so the three states are encoded rather than labelled.
Ordering them 0/1/2 means ``max_over_time`` and a ``> 0`` alert both say
something true without the alert having to enumerate state names.
"""

REQUESTS_TOTAL: Final = "gateway_requests_total"
DURATION_MS: Final = "gateway_request_duration_ms"
BREAKER_FAIL_OPEN_TOTAL: Final = "gateway_breaker_fail_open_total"
BREAKER_STATE: Final = "gateway_breaker_state"
QUOTA_REMAINING: Final = "gateway_quota_remaining"

type _RequestKey = tuple[str, str, str, str]
"""``(provider, model, status, key_pool)``."""

type _DurationKey = tuple[str, RoutingMode]
"""``(provider, mode)`` — no model, deliberately. Latency is a property of the
upstream far more than of which of its models answered, and the cross product of
models and buckets is the cheapest place in this file to make cardinality a
problem."""


@dataclass(slots=True)
class _Histogram:
    """One ``(provider, mode)`` series: per-bucket counts, a sum and a count.

    Counts are stored *non*-cumulatively and accumulated at render time. That is
    what makes it impossible to emit a bucket series that is not monotonic:
    the rendering is the only place addition happens, so there is no state a
    lost increment could leave inconsistent.
    """

    bounds: tuple[float, ...]
    counts: list[int]
    total_ms: float = 0.0
    count: int = 0

    def observe(self, ms: float) -> None:
        observation = max(0.0, ms)
        self.count += 1
        self.total_ms += observation
        for index, bound in enumerate(self.bounds):
            if observation <= bound:
                self.counts[index] += 1
                return
        # Above every finite bound: it lands in `+Inf` alone, which `cumulative`
        # reads off `count` rather than off this list.

    def cumulative(self) -> list[tuple[str, int]]:
        """``[(le, count_at_or_below), ...]``, ending at ``+Inf``.

        Cumulative and ending in ``+Inf`` is not a stylistic choice — a
        non-cumulative histogram parses fine and charts wrong, which is the worst
        failure mode a hand-rolled exporter has.
        """
        running = 0
        rows: list[tuple[str, int]] = []
        for bound, bucket in zip(self.bounds, self.counts, strict=True):
            running += bucket
            rows.append((_format_number(bound), running))
        rows.append(("+Inf", self.count))
        return rows


@dataclass(frozen=True, slots=True)
class BreakerGauge:
    """One candidate's stored breaker state, read live at scrape time.

    ``state`` is a plain ``str`` rather than
    :data:`~app.routing.circuit_breaker.BreakerState`: this module is imported
    *by* the breaker (for the fail-open counter), so importing it back would be a
    cycle, and an unrecognized state simply renders no sample.
    """

    provider: str
    model: str
    state: str


@dataclass(frozen=True, slots=True)
class QuotaGauge:
    """One ``(candidate, window)``'s remaining budget, read live at scrape time."""

    provider: str
    model: str
    window: str
    remaining: int


class MetricsRegistry:
    """Process-local counters and histograms, for ``GET /metrics`` (D49).

    **Process-local, like** :class:`LatencyTable` **and for the same reason**
    (ADR-014). Sharing these across workers would need new Contract C keys, and
    that is a change with sign-off rather than a side effect of a polish phase.
    Render runs two workers, so a scrape hits one of them and the counters are a
    *sample* rather than a total — documented in ``docs/limitations.md``, not
    papered over. The gauges are read live from Redis and are therefore correct
    on whichever worker answers.

    **No locks**, same reasoning the :class:`LatencyTable` docstring already
    gives: an increment is cheaper than the lock protecting it, and the worst a
    lost one can do is understate a counter by one.

    **Labels never carry a ``user_id``, an email, a conversation id, or free
    text.** Unbounded label cardinality is how a metrics endpoint takes down the
    thing scraping it, and the two identifiers a gateway is tempted to label with
    are also the two that make the endpoint a privacy surface. Every label value
    this class accepts comes from a bounded set — a provider name, a model name,
    a status, a pool label — and ``tests/unit/test_metrics.py`` asserts that no
    rendered label anywhere looks like a UUID.
    """

    def __init__(self, *, buckets: Sequence[float] = DURATION_BUCKETS_MS) -> None:
        self._bounds = tuple(sorted(buckets))
        self._requests: dict[_RequestKey, int] = {}
        self._durations: dict[_DurationKey, _Histogram] = {}
        self._breaker_fail_open: dict[tuple[str, str], int] = {}

    # ----------------------------------------------------------------- #
    # Writing
    # ----------------------------------------------------------------- #
    def record_request(
        self, *, provider: str | None, model: str | None, status: str, key_pool: str | None
    ) -> None:
        """One terminal outcome. Called from ``usage/logger.py``'s facades only.

        The facades are already the one place every terminal outcome passes
        through, which is why the counter lives there and not in the router: a
        counter incremented at three call sites is a counter that will be wrong
        within two phases.
        """
        key: _RequestKey = (
            provider or UNKNOWN_LABEL,
            model or UNKNOWN_LABEL,
            status,
            key_pool or NO_KEY_POOL,
        )
        self._requests[key] = self._requests.get(key, 0) + 1

    def observe_duration(self, *, provider: str | None, mode: RoutingMode, ms: float) -> None:
        """One turn's wall-clock latency, in milliseconds.

        A cache hit is deliberately *not* observed here: a replay's latency is a
        property of Redis, and folding it in would drag every provider's
        distribution toward zero in proportion to how well the cache is working.
        """
        key: _DurationKey = (provider or UNKNOWN_LABEL, mode)
        histogram = self._durations.get(key)
        if histogram is None:
            histogram = _Histogram(bounds=self._bounds, counts=[0] * len(self._bounds))
            self._durations[key] = histogram
        histogram.observe(ms)

    def record_breaker_fail_open(self, provider: str, model: str) -> None:
        """The counter ADR-010 asked for and this module's docstring promised.

        A breaker decision made without Redis is invisible otherwise: the request
        still succeeds, so the only trace is a log line. This is the number that
        answers "how long has the breaker been guessing?".
        """
        key = (provider, model)
        self._breaker_fail_open[key] = self._breaker_fail_open.get(key, 0) + 1

    # ----------------------------------------------------------------- #
    # Reading
    # ----------------------------------------------------------------- #
    def request_counts(self) -> Mapping[_RequestKey, int]:
        return MappingProxyType(dict(self._requests))

    def breaker_fail_open_counts(self) -> Mapping[tuple[str, str], int]:
        return MappingProxyType(dict(self._breaker_fail_open))

    def duration_series(self) -> Mapping[_DurationKey, _Histogram]:
        return MappingProxyType(dict(self._durations))


def render_exposition(
    registry: MetricsRegistry,
    *,
    breakers: Sequence[BreakerGauge] = (),
    quota: Sequence[QuotaGauge] = (),
) -> str:
    """The whole endpoint body, in Prometheus text format 0.0.4.

    Hand-rolled rather than ``prometheus_client``: five metric families and an
    exposition format that is a hundred lines of string building do not justify a
    runtime dependency, and the hand-rolled circuit breaker set this precedent for
    the same reason.

    The two gauge families are **omitted entirely** — HELP and TYPE included —
    when their sequence is empty, which is what a Redis failure hands in. A
    metrics endpoint that 500s during an incident is a metrics endpoint that is
    useless exactly when it is needed; one that reports the counters it *can*
    still see is not.

    Every series is emitted in sorted order so two scrapes of an unchanged
    process are byte-identical, which is what makes the output diffable by hand.
    """
    lines: list[str] = []

    lines.append(f"# HELP {REQUESTS_TOTAL} Terminal request outcomes recorded by this worker.")
    lines.append(f"# TYPE {REQUESTS_TOTAL} counter")
    for (provider, model, status, key_pool), count in sorted(registry.request_counts().items()):
        lines.append(
            _sample(
                REQUESTS_TOTAL,
                (
                    ("provider", provider),
                    ("model", model),
                    ("status", status),
                    ("key_pool", key_pool),
                ),
                count,
            )
        )

    lines.append(f"# HELP {DURATION_MS} Request wall-clock latency in milliseconds.")
    lines.append(f"# TYPE {DURATION_MS} histogram")
    for (provider, mode), histogram in sorted(registry.duration_series().items()):
        base = (("provider", provider), ("mode", mode))
        for le, cumulative in histogram.cumulative():
            lines.append(_sample(f"{DURATION_MS}_bucket", (*base, ("le", le)), cumulative))
        lines.append(_sample(f"{DURATION_MS}_sum", base, histogram.total_ms))
        lines.append(_sample(f"{DURATION_MS}_count", base, histogram.count))

    lines.append(
        f"# HELP {BREAKER_FAIL_OPEN_TOTAL} Breaker decisions made without Redis (ADR-010)."
    )
    lines.append(f"# TYPE {BREAKER_FAIL_OPEN_TOTAL} counter")
    for (provider, model), count in sorted(registry.breaker_fail_open_counts().items()):
        lines.append(
            _sample(BREAKER_FAIL_OPEN_TOTAL, (("provider", provider), ("model", model)), count)
        )

    if breakers:
        lines.append(
            f"# HELP {BREAKER_STATE} Circuit breaker state: 0 closed, 1 half_open, 2 open."
        )
        lines.append(f"# TYPE {BREAKER_STATE} gauge")
        for breaker_gauge in sorted(breakers, key=lambda g: (g.provider, g.model)):
            code = BREAKER_STATE_CODES.get(breaker_gauge.state)
            if code is None:
                continue
            lines.append(
                _sample(
                    BREAKER_STATE,
                    (("provider", breaker_gauge.provider), ("model", breaker_gauge.model)),
                    code,
                )
            )

    if quota:
        lines.append(f"# HELP {QUOTA_REMAINING} Budget left in a window under the shared pool.")
        lines.append(f"# TYPE {QUOTA_REMAINING} gauge")
        for quota_gauge in sorted(quota, key=lambda g: (g.provider, g.model, g.window)):
            lines.append(
                _sample(
                    QUOTA_REMAINING,
                    (
                        ("provider", quota_gauge.provider),
                        ("model", quota_gauge.model),
                        ("window", quota_gauge.window),
                    ),
                    quota_gauge.remaining,
                )
            )

    # Prometheus requires the body to end in a newline; a scrape of a body that
    # does not is a parse error rather than an empty result.
    return "\n".join(lines) + "\n"


def _sample(name: str, labels: Sequence[tuple[str, str]], value: float) -> str:
    rendered = ",".join(f'{key}="{_escape(text)}"' for key, text in labels)
    return f"{name}{{{rendered}}} {_format_number(value)}"


def _escape(value: str) -> str:
    """Backslash, double quote and newline, in that order.

    Order matters: escaping the quote first would then have its own backslash
    escaped by the next pass, doubling it.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_number(value: float) -> str:
    """Integers without a trailing ``.0``; everything else as a plain decimal.

    Cosmetic for a parser, which reads both, and not cosmetic for a human
    eyeballing ``curl localhost:8000/metrics`` — which is this endpoint's own
    definition of done.
    """
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


__all__ = [
    "BREAKER_FAIL_OPEN_TOTAL",
    "BREAKER_STATE",
    "BREAKER_STATE_CODES",
    "COMPLETE",
    "DURATION_BUCKETS_MS",
    "DURATION_MS",
    "EMPTY_SNAPSHOT",
    "EWMA_ALPHA",
    "MIN_SAMPLES",
    "NO_KEY_POOL",
    "QUOTA_REMAINING",
    "REQUESTS_TOTAL",
    "STREAM",
    "UNKNOWN_LABEL",
    "BreakerGauge",
    "LatencySample",
    "LatencySnapshot",
    "LatencyTable",
    "MetricsRegistry",
    "QuotaGauge",
    "RoutingMode",
    "render_exposition",
]
