"""Reserve → commit/release: the gateway's own record of what it has left to spend.

Before an attempt leaves the process the router writes down "I am about to spend
one request and roughly nine hundred tokens"; afterwards it corrects that to what
was really spent, or hands it back if nothing was. That is the whole shape of
this module, and the reason it is not three lines is concurrency — see
``scripts/reserve.lua``, which is where the interesting half lives.

**Fail closed.** Contract C's asymmetry, and D15's reasoning for it: the breaker
fails *open* because its absence costs one wasted round trip, while quota's
absence costs a provider key banned for sustained over-limit traffic — which is
not a degraded gateway but a gateway with one fewer provider, permanently, that
no amount of failover recovers. So every Redis failure inside :meth:`reserve`
produces ``allowed=False, degraded=True`` and the router treats that candidate
exactly as it treats an exhausted one. With Redis down the whole chain answers
that way and the request fails, which is the truth: the gateway does not know
what it has left, so it is not spending it.

:meth:`commit` and :meth:`release` swallow-and-log instead, the way the breaker's
writes do. By the time either is called the attempt has already happened and
there is nothing left to refuse.

**Policy lives here, arithmetic lives in Lua.** Headroom (D16) and D8's lane
split are applied to a published limit before the script is handed a number, so
the scripts stay testable as pure counter mechanics and nothing about the split
has to be expressed in a language with no way to test it.

**The tracker holds no state.** The Redis keys are the state, so constructing one
per request is correct and free — the same reasoning ``deps.get_breaker`` follows.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from math import floor
from typing import Any, Final

from redis.asyncio import Redis

from app.cache import keys
from app.cache.client import LuaScriptRegistry
from app.config import LimitsConfig
from app.core.clock import SYSTEM_CLOCK, Clock
from app.core.logging import get_logger
from app.providers.types import ModelSpec, QuotaHint
from app.quota import lanes, windows
from app.quota.windows import WindowSpec, WindowState

logger = get_logger("app.quota.tracker")

RESERVE: Final = "reserve"
COMMIT: Final = "commit"
RELEASE: Final = "release"
"""Script names, which are the ``.lua`` filenames :class:`LuaScriptRegistry`
discovered. A typo here is a ``KeyError`` at reserve time, which fails closed —
loud in the log and safe in behaviour, but still a bug rather than a condition."""

_CONVERGING_RESETS: Final = frozenset({"fixed_daily_utc", "fixed_daily_pt"})
"""Windows whose TTL may be refreshed on every increment.

A daily counter's TTL is "seconds until the provider's next reset", which
converges on a real instant and is therefore idempotent to re-set. ``rolling_60s``
is the opposite: refreshing it on every increment means it never expires under
sustained traffic, and the counter climbs forever behind a permanent false 429
(D16, trap 1). Named as a set rather than written as ``!= "rolling_60s"`` so a
fourth :data:`~app.config.ResetKind` has to be classified deliberately.
"""


@dataclass(frozen=True, slots=True)
class Reservation:
    """Budget taken out on an attempt, and the receipt that gives it back.

    Carries the identity the commit and the release need — the same reasoning
    :class:`~app.routing.circuit_breaker.BreakerDecision` follows: a caller that
    had to restate ``(scope, provider, model, request_id)`` on the way back is a
    caller that can restate it wrongly.
    """

    scope: keys.Scope
    provider: str
    model: str
    request_id: str
    tokens: int
    """The estimate that was reserved against the token windows. What
    :meth:`QuotaTracker.commit` corrects."""

    windows: tuple[keys.QuotaWindow, ...]
    """Which windows were actually incremented. Empty when the model declares no
    limits at all, in which case nothing was written and nothing needs undoing."""

    @property
    def is_empty(self) -> bool:
        return not self.windows


@dataclass(frozen=True, slots=True)
class QuotaDecision:
    """May this candidate be attempted, and if not, when is it worth asking again?"""

    allowed: bool
    reservation: Reservation | None = None
    blocked_window: keys.QuotaWindow | None = None
    retry_after_s: float | None = None
    resets_at: datetime | None = None

    degraded: bool = False
    """Decided without Redis, so ``allowed`` is ``False`` by policy rather than by
    measurement (D15). The router records the skip the same way either way; this
    flag is what tells the log line apart."""


class QuotaTracker:
    """Reads and writes ``q:{scope}:{provider}:{model}:*``. One per request is fine."""

    def __init__(
        self,
        redis: Redis,
        scripts: LuaScriptRegistry,
        limits: LimitsConfig,
        *,
        clock: Clock = SYSTEM_CLOCK,
        headroom: float = 0.0,
    ) -> None:
        self._redis = redis
        self._scripts = scripts
        self._limits = limits
        self._clock = clock
        self._headroom = headroom

    # ----------------------------------------------------------------- #
    # Reserving
    # ----------------------------------------------------------------- #
    async def reserve(
        self,
        spec: ModelSpec,
        *,
        scope: keys.Scope,
        estimated_tokens: int,
        request_id: str,
    ) -> QuotaDecision:
        """Take out budget for one attempt, atomically, across every window.

        ``estimated_tokens`` is the render pipeline's ``RenderReport``
        measurement, which is ``adapter.estimate_tokens`` over the finished
        payload — the only trustworthy number available before the call, and the
        reason D17 puts the reservation *after* render rather than before it.
        """
        if estimated_tokens < 0:
            raise ValueError(f"estimated_tokens must be non-negative, got {estimated_tokens}")

        window_specs = self._budget(spec)
        reservation = Reservation(
            scope=scope,
            provider=spec.provider,
            model=spec.model,
            request_id=request_id,
            tokens=estimated_tokens,
            windows=tuple(window.window for window in window_specs),
        )

        if not window_specs:
            # Nothing declared, nothing to enforce, no round trip. Not the same
            # as fail-open: this candidate has no published limit to overspend.
            return QuotaDecision(allowed=True, reservation=reservation)

        now = self._clock.now()
        args: list[Any] = [len(window_specs)]
        for window in window_specs:
            args += [
                window.window,
                window.limit,
                self._cost(window, estimated_tokens),
                windows.ttl_s(window.reset, now=now),
                "1" if window.reset in _CONVERGING_RESETS else "0",
            ]
        args += [keys.RESERVATION_TTL_S, request_id]

        try:
            raw: Any = await self._scripts[RESERVE](
                keys=[
                    keys.quota_reservation(scope, spec.provider, spec.model, request_id),
                    *self._counter_keys(scope, spec.provider, spec.model, reservation.windows),
                ],
                args=args,
            )
        except Exception as exc:
            return self._fail_closed(spec, scope, exc)

        if int(raw[0]) == 1:
            return QuotaDecision(allowed=True, reservation=reservation)

        return self._blocked(
            spec, scope, window_specs, blocked=str(raw[1]), ttl=int(raw[2]), now=now
        )

    def _blocked(
        self,
        spec: ModelSpec,
        scope: keys.Scope,
        window_specs: tuple[WindowSpec, ...],
        *,
        blocked: str,
        ttl: int,
        now: datetime,
    ) -> QuotaDecision:
        """Turn the script's ``{0, window, ttl}`` into a decision the router can log.

        The script's TTL wins over a recomputed window width because a fixed
        window is aligned to its *first* increment: a minute counter created
        forty seconds ago has twenty seconds left, and ``windows.ttl_s`` would
        confidently say sixty. Only when the key vanished between the check and
        the TTL read — a real race, and a rare one — is the pure function the
        better answer available.
        """
        window = next((w for w in window_specs if w.window == blocked), None)
        if ttl <= 0 and window is not None:
            ttl = windows.ttl_s(window.reset, now=now)

        retry_after_s = float(max(ttl, 0))
        logger.info(
            "quota.blocked",
            provider=spec.provider,
            model=spec.model,
            scope=scope,
            window=blocked,
            limit=window.limit if window is not None else None,
            retry_after_s=retry_after_s,
        )
        return QuotaDecision(
            allowed=False,
            blocked_window=_as_window(blocked),
            retry_after_s=retry_after_s,
            resets_at=now + timedelta(seconds=retry_after_s),
        )

    def _fail_closed(self, spec: ModelSpec, scope: keys.Scope, exc: Exception) -> QuotaDecision:
        """Redis did not answer, so this candidate is not attempted (D15).

        Warning rather than error: one unreachable Redis produces one of these
        per candidate per request, and the request itself already fails with a
        ``RateLimited`` the client can see. The line exists so the cause is in
        the log rather than inferred from a fleet that suddenly refuses
        everything.
        """
        logger.warning(
            "quota.fail_closed",
            provider=spec.provider,
            model=spec.model,
            scope=scope,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return QuotaDecision(allowed=False, degraded=True)

    # ----------------------------------------------------------------- #
    # Settling
    # ----------------------------------------------------------------- #
    async def commit(self, reservation: Reservation, *, tokens_in: int, tokens_out: int) -> None:
        """Correct the token estimate to what the attempt really spent.

        Called for every attempt that reached the provider, successful or not.
        A failed one commits the tokens that were really generated — zero on the
        non-streaming path, D1's wasted-token estimate for a stream that died
        mid-sentence — and never gets its *request* counters back, because the
        provider counted the request (trap 6).
        """
        if reservation.is_empty:
            return

        actual = tokens_in + tokens_out
        now = self._clock.now()
        token_windows = tuple(
            window
            for window in self._declared(reservation.provider, reservation.model)
            if window.cost_is_tokens and window.window in reservation.windows
        )

        args: list[Any] = [len(token_windows)]
        for window in token_windows:
            args += [
                window.window,
                actual,
                windows.ttl_s(window.reset, now=now),
                "1" if window.reset in _CONVERGING_RESETS else "0",
            ]

        # Called even with no token windows: the reservation hash still has to be
        # deleted, and the script's EXISTS/DEL is the one place that happens.
        raw = await self._run_settlement(
            COMMIT, reservation, tuple(window.window for window in token_windows), args
        )
        if raw is not None and int(raw[0]) == 0:
            # D17: a generation can outlive RESERVATION_TTL_S, and the estimate
            # stays counted. Over-counting is the safe direction; this line is
            # how a counter that drifted upward is explainable afterwards.
            logger.warning(
                "quota.reservation_expired",
                provider=reservation.provider,
                model=reservation.model,
                scope=reservation.scope,
                request_id=reservation.request_id,
                reserved_tokens=reservation.tokens,
                actual_tokens=actual,
            )

    async def release(self, reservation: Reservation) -> None:
        """Give everything back. Only for an attempt that never left the process."""
        if reservation.is_empty:
            return

        # Driven by the reservation rather than by config: a `limits.yaml` edit
        # between reserve and release must not orphan a counter that was really
        # incremented. Nothing here needs a TTL or a limit — a release only ever
        # subtracts, and the amounts come from the hash.
        args: list[Any] = [len(reservation.windows), *reservation.windows]

        await self._run_settlement(RELEASE, reservation, reservation.windows, args)

    async def _run_settlement(
        self,
        script: str,
        reservation: Reservation,
        settled: tuple[keys.QuotaWindow, ...],
        args: list[Any],
    ) -> Any | None:
        """Run ``commit``/``release`` and swallow whatever Redis does about it.

        There is nothing left to refuse by the time either runs — the attempt has
        already happened and its outcome is already the caller's. The only
        casualty of a failure here is the correction itself, which leaves the
        estimate standing.
        """
        try:
            return await self._scripts[script](
                keys=[
                    keys.quota_reservation(
                        reservation.scope,
                        reservation.provider,
                        reservation.model,
                        reservation.request_id,
                    ),
                    *self._counter_keys(
                        reservation.scope, reservation.provider, reservation.model, settled
                    ),
                ],
                args=args,
            )
        except Exception as exc:
            logger.warning(
                "quota.settlement_failed",
                action=script,
                provider=reservation.provider,
                model=reservation.model,
                scope=reservation.scope,
                request_id=reservation.request_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None

    # ----------------------------------------------------------------- #
    # Reporting
    # ----------------------------------------------------------------- #
    async def remaining(self, spec: ModelSpec, *, scope: keys.Scope) -> tuple[WindowState, ...]:
        """What is left in each declared window, for ``GET /v1/models`` (Step 7).

        An empty tuple means "we do not know" — either the model declares no
        limits or Redis did not answer — and the endpoint reports ``unknown``
        rather than inventing an ``available``. Reads are not the place fail-closed
        applies: nothing is being spent, and a status page that lies in the
        confident direction is worse than one that admits the gap.
        """
        window_specs = self._budget(spec)
        if not window_specs:
            return ()

        counter_keys = self._counter_keys(
            scope, spec.provider, spec.model, tuple(window.window for window in window_specs)
        )
        try:
            pipeline = self._redis.pipeline(transaction=False)
            for key in counter_keys:
                pipeline.get(key)
                pipeline.ttl(key)
            raw: list[Any] = await pipeline.execute()
        except Exception as exc:
            logger.warning(
                "quota.remaining_unavailable",
                provider=spec.provider,
                model=spec.model,
                scope=scope,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return ()

        now = self._clock.now()
        states: list[WindowState] = []
        for index, window in enumerate(window_specs):
            used = _read_int(raw[index * 2])
            ttl = _read_int(raw[index * 2 + 1])
            # Same reasoning as `_blocked`: a live counter's TTL is the only thing
            # that knows where its fixed window actually started.
            resets_at = (
                now + timedelta(seconds=ttl)
                if ttl > 0
                else windows.resets_at(window.reset, now=now)
            )
            states.append(
                WindowState(
                    window=window.window, limit=window.limit, used=used, resets_at=resets_at
                )
            )
        return tuple(states)

    async def apply_hint(self, spec: ModelSpec, *, scope: keys.Scope, hint: QuotaHint) -> None:
        """Correct the local counters toward a provider's own published remainder.

        Step 6's seam, typed rather than stubbed (D18). The transport — a
        contextvar sink in ``providers/base.py``, because ``complete()`` returns a
        ``Completion`` and the ``httpx.Response`` never escapes the adapter —
        lands with it, as does the rule that a hint may only ever move a counter
        *up*: a hint is up to one request stale, and letting it grant budget
        reintroduces the race the reserve script exists to close.
        """
        raise NotImplementedError("QuotaHint reconciliation lands in Phase 3 Step 6")

    # ----------------------------------------------------------------- #
    # Internals
    # ----------------------------------------------------------------- #
    def _declared(self, provider: str, model: str) -> tuple[WindowSpec, ...]:
        """The windows ``limits.yaml`` publishes for this model, at face value."""
        limits = self._limits.for_model(provider, model)
        if limits is None:
            # Not fail-open by sleight of hand: a model with no published limits
            # has no limit to overspend. It is still a config gap worth seeing,
            # because the far likelier cause is a typo in providers.yaml that
            # stopped a real limit from ever being found.
            logger.warning("quota.no_declared_limits", provider=provider, model=model)
            return ()
        return windows.declared(limits)

    def _budget(self, spec: ModelSpec) -> tuple[WindowSpec, ...]:
        """Declared windows with the published limits reduced to what chat may spend."""
        return tuple(
            replace(window, limit=self._effective_limit(window.limit, spec))
            for window in self._declared(spec.provider, spec.model)
        )

    def _effective_limit(self, published: int, spec: ModelSpec) -> int:
        """``floor(published * (1 - headroom) * lanes.answer_share(spec))``, never below 1.

        Headroom is D16's answer to the fixed window's boundary overshoot: a
        fixed counter permits up to 2x the limit across a reset, and holding back
        ten percent of every published limit keeps the effective ceiling under the
        real one by more than that jitter costs.

        D8's lane split multiplies in here too, via ``lanes.answer_share(spec)`` —
        so a ``reserved_fraction`` of 0.5 halves the *answer* budget rather than
        the perception one (trap 15). This is the one line ``quota/lanes.py``
        (Step 4) changes; the module it calls into owns the direction of the split
        so getting it backwards is a one-function bug rather than one smeared
        across every call site.

        The floor at 1 is deliberate: rounding a published limit down to zero
        would make a configured model permanently unspendable, which reads
        exactly like a provider outage.
        """
        return max(1, floor(published * (1.0 - self._headroom) * lanes.answer_share(spec)))

    @staticmethod
    def _cost(window: WindowSpec, estimated_tokens: int) -> int:
        return estimated_tokens if window.cost_is_tokens else 1

    @staticmethod
    def _counter_keys(
        scope: keys.Scope, provider: str, model: str, settled: tuple[keys.QuotaWindow, ...]
    ) -> list[str]:
        """Counter keys in window order — the order the scripts index KEYS by."""
        return [keys.quota(scope, provider, model, window) for window in settled]


def _as_window(name: str) -> keys.QuotaWindow | None:
    """Narrow the script's echoed window name back to the literal type.

    The script echoes what it was handed, so this cannot fail in practice; it
    returns ``None`` rather than asserting because a mismatch here would mean the
    router refuses a request over a name it could not classify, and a decision
    without a window label is still a correct decision.
    """
    if name in ("rpm", "rpd", "tpm", "tpd"):
        return name  # type: ignore[return-value]
    return None


def _read_int(raw: Any) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "QuotaDecision",
    "QuotaTracker",
    "Reservation",
]
