"""An injectable time source.

Anything that compares timestamps takes a :class:`Clock` rather than calling
``datetime.now()`` directly. The JWKS cache expires entries against it, the API
key repo throttles ``last_used_at`` writes against it, and Phase 3's quota
windows will reset against it. The alternative is tests that ``sleep`` — slow,
flaky, and unable to test a 12-hour TTL at all.

Always timezone-aware UTC. A naive datetime compared against an aware one raises
``TypeError`` at runtime, and every ``timestamptz`` column in the schema hands
back aware values.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    """The dependency every time-sensitive component declares."""

    def now(self) -> datetime:
        """The current instant, timezone-aware, UTC."""
        ...


class SystemClock:
    """The real clock. The default everywhere outside tests."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """A clock that only moves when a test tells it to.

    Mutable on purpose — a test advances the same instance the component under
    test is holding, which is what makes "the cache is now stale" expressible
    without waiting.
    """

    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._at = at.astimezone(UTC)

    def now(self) -> datetime:
        return self._at

    def advance(self, seconds: float) -> datetime:
        """Move forward and return the new time."""
        self._at += timedelta(seconds=seconds)
        return self._at

    def set(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._at = at.astimezone(UTC)


SYSTEM_CLOCK: Clock = SystemClock()
"""The process-wide default. Stateless, so sharing one instance is free."""
