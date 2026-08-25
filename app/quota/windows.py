"""Per-provider reset semantics — the only place that knows what "per day" means.

Providers disagree about what a window's reset instant is. Groq's day ends at
midnight UTC; Google's ends at midnight in California, which is a different UTC
instant in summer than in winter; a per-minute window just has to expire on its
own schedule rather than on a wall-clock boundary at all. Getting any of that
wrong does not raise an exception — it silently produces a ``resets_at`` that is
off by up to eight hours, or an ``rpm`` counter that never expires and serves a
permanent false 429 that looks exactly like a provider outage (trap 1).

This module does no I/O and calls no clock it was not handed — every function
here is pure, which is what makes DST behaviour a table test instead of a
`ScheduleWakeup` on a real clock at midnight. Nothing calls it yet; the tracker
(Step 3) is the first caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Final
from zoneinfo import ZoneInfo

from app.cache import keys
from app.config import ModelLimits, ResetKind

PACIFIC: Final = ZoneInfo("America/Los_Angeles")
"""Never a hardcoded ``-8``. Wrong for roughly eight months a year (D16, trap 4)."""

_ROLLING_WINDOW_S: Final = keys.QUOTA_WINDOW_TTL_S
"""60. The width of ``rolling_60s``, which — under Contract C's fixed key
schema — is implemented as a fixed window rather than a true rolling one; see
D16 for the reasoning and the bounded overshoot it accepts."""

_ROLLING_DAILY_WINDOW_S: Final = 86_400
"""The width of ``rolling_daily`` (Phase 6 Step 7, D39) — a day, the same
overshoot-accepting fixed-window trick ``rolling_60s`` uses, scaled up for a
counter with no provider-defined midnight to align to."""


@dataclass(frozen=True, slots=True)
class WindowSpec:
    """What one window (``rpm``/``rpd``/``tpm``/``tpd``) is allowed to spend.

    ``limit`` is whatever the caller hands ``declared()`` — headroom and D8's
    lane split are applied by the tracker (Step 3) before a ``WindowSpec``
    reaches the Lua script; this module only knows reset semantics.
    """

    window: keys.QuotaWindow
    limit: int
    reset: ResetKind
    cost_is_tokens: bool
    """``tpm``/``tpd`` reserve a token estimate; ``rpm``/``rpd`` reserve exactly 1."""


@dataclass(frozen=True, slots=True)
class WindowState:
    """A window's usage as of some instant — what ``/v1/models`` and the
    tracker's ``remaining()`` report."""

    window: keys.QuotaWindow
    limit: int
    used: int
    resets_at: datetime

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit


_TOKEN_WINDOWS: Final = frozenset({"tpm", "tpd"})
_ALL_WINDOWS: Final[tuple[keys.QuotaWindow, ...]] = ("rpm", "rpd", "tpm", "tpd")


def resets_at(reset: ResetKind, *, now: datetime) -> datetime:
    """The next instant this window's counter reaches zero again.

    ``now`` must be timezone-aware; every caller in this codebase gets it from
    a :class:`~app.core.clock.Clock`, never from a bare ``datetime.now()``.
    """
    if reset == "rolling_60s":
        return now + timedelta(seconds=_ROLLING_WINDOW_S)
    if reset == "rolling_daily":
        return now + timedelta(seconds=_ROLLING_DAILY_WINDOW_S)
    if reset == "fixed_daily_utc":
        return _next_midnight(now.astimezone(UTC), UTC)
    if reset == "fixed_daily_pt":
        return _next_midnight(now.astimezone(PACIFIC), PACIFIC).astimezone(UTC)
    raise AssertionError(f"unhandled ResetKind: {reset!r}")


def ttl_s(reset: ResetKind, *, now: datetime) -> int:
    """Seconds from ``now`` until :func:`resets_at`. What the counter's Redis
    key should live for — ``rpm``/``tpm`` set it once at creation, ``rpd``/``tpd``
    refresh it on every increment (D16, trap 1)."""
    delta = resets_at(reset, now=now) - now
    return max(1, round(delta.total_seconds()))


def _next_midnight(local_now: datetime, tz: ZoneInfo | timezone) -> datetime:
    """The next local midnight strictly after ``local_now``, in ``tz``.

    Naive ``date() + 1 day`` arithmetic is exactly what breaks across a DST
    transition: adding 24 hours to 2026-03-07T23:30 Pacific does not land on
    local midnight because the clocks jump. Building the boundary from the
    *date* and re-localizing avoids that — ``ZoneInfo`` resolves the resulting
    wall-clock time to whatever UTC offset is actually in effect that day.
    """
    next_date = local_now.date() + timedelta(days=1)
    return datetime(next_date.year, next_date.month, next_date.day, tzinfo=tz)


def declared(limits: ModelLimits) -> tuple[WindowSpec, ...]:
    """The windows a provider actually publishes, as ``WindowSpec``s.

    A ``None`` limit means ``limits.yaml`` recorded "this provider does not
    publish this window" — dropped here, never read as unlimited and never as
    zero. A window declared without a matching ``reset`` entry is a config bug,
    not a runtime one: it fails loudly rather than guessing a reset kind.
    """
    specs: list[WindowSpec] = []
    for window in _ALL_WINDOWS:
        limit = getattr(limits, window)
        if limit is None:
            continue
        reset = getattr(limits.reset, window)
        if reset is None:
            raise ValueError(f"window {window!r} has a limit but no declared reset kind")
        specs.append(
            WindowSpec(
                window=window,
                limit=limit,
                reset=reset,
                cost_is_tokens=window in _TOKEN_WINDOWS,
            )
        )
    return tuple(specs)


def sliding_count(previous: int, current: int, *, elapsed_fraction: float) -> float:
    """D20's two-bucket interpolation: a sliding window built from two fixed
    buckets rather than a sorted set of timestamps.

    ``elapsed_fraction`` is how far into the *current* bucket ``now`` sits, in
    ``[0.0, 1.0]``. At 0.0 the current bucket has just started and the previous
    bucket still counts in full; at 1.0 the current bucket is about to roll over
    and the previous bucket has aged out entirely.
    """
    if not 0.0 <= elapsed_fraction <= 1.0:
        raise ValueError(f"elapsed_fraction must be in [0.0, 1.0], got {elapsed_fraction!r}")
    return previous * (1.0 - elapsed_fraction) + current
