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

**Not here yet:** the breaker's fail-open counter ADR-010 asks for. This module
holds one series type and nothing reads a counter, so adding one now would be a
number written and never looked at; the structured ``breaker.fail_open`` event is
countable in aggregation until Phase 7's ``/metrics`` endpoint gives it a reader.
"""

from __future__ import annotations

from collections.abc import Mapping
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


__all__ = [
    "COMPLETE",
    "EMPTY_SNAPSHOT",
    "EWMA_ALPHA",
    "MIN_SAMPLES",
    "STREAM",
    "LatencySample",
    "LatencySnapshot",
    "LatencyTable",
    "RoutingMode",
]
