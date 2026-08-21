"""D8's 50/50 Gemini split — fencing off the perception lane's half before
anything is allowed to spend it.

Gemini is the only model in the fleet that can read a PDF, and it is also a
perfectly good chat model. If plain chat could spend the whole daily budget,
the one genuinely differentiated feature in the product would stop working
every afternoon. ``reserved_fraction`` (``ModelSpec``, from
``config/providers.yaml``) is the share of a model's published quota held back
from the answer lane; this module is the only place that turns that fraction
into a share of a limit, in either direction.

Nothing here does I/O. :func:`answer_share` is the one line
``quota/tracker.py`` multiplies into every reservation it makes (Step 3);
:func:`perception_budget` gives Phase 4 the other half's numbers without Phase
4 having to re-derive them; :func:`reserve_perception` (Step 5) is the seam
Phase 3 left typed and Phase 4 spends: it walks Contract C's one perception
key against the daily half of :func:`perception_budget`, and the *shared*
``rpm``/``tpm`` counters against their full published ceiling rather than the
answer lane's halved one (D26) — the one honest awkwardness the phase plan
calls out, because :meth:`~app.quota.tracker.QuotaTracker._effective_limit`
bakes :func:`answer_share` into every limit it computes, which is right for
chat and wrong for this. Rather than threading a ``lane`` parameter five call
sites deep, this module computes its own limits and hands them to
:meth:`~app.quota.tracker.QuotaTracker.reserve_windows` directly — the small
internal entry point Step 5 adds to the tracker for exactly this.
"""

from __future__ import annotations

from math import floor
from typing import TYPE_CHECKING, Final

from app.cache import keys
from app.config import ModelLimits
from app.providers.types import ModelSpec
from app.quota import windows

if TYPE_CHECKING:
    from app.quota.tracker import QuotaDecision, QuotaTracker, Reservation, WindowGrant

ANSWER: Final = "answer"
PERCEPTION: Final = "perception"

_DAILY_WINDOWS: Final = frozenset({"rpd", "tpd"})
"""The windows D26 fences daily. Never ``rpm``/``tpm`` — those stay a *shared*
counter with chat, checked against the full published ceiling instead of a
fenced-off share (see the module docstring)."""


def answer_share(spec: ModelSpec) -> float:
    """The fraction of ``spec``'s published quota the answer lane may spend.

    ``1.0 - spec.reserved_fraction`` — chat sees what perception did not fence
    off, never the other way around (trap 15: getting this backwards starves
    the feature the reservation exists to protect, and nothing fails loudly
    when it happens).
    """
    return 1.0 - spec.reserved_fraction


def perception_budget(spec: ModelSpec, limits: ModelLimits) -> dict[keys.QuotaWindow, int]:
    """What Phase 4's lane will have to spend from, window by window.

    Mirrors the answer lane's arithmetic in the other direction —
    ``floor(published * spec.reserved_fraction)`` per window ``limits.yaml``
    actually declares. A ``None`` window (the provider publishes no such
    limit) is dropped exactly as :func:`app.quota.windows.declared` drops it,
    never read as unlimited and never as zero. No headroom applied here:
    headroom is a tracker-instance concern (``QUOTA_HEADROOM_FRACTION``), and
    this function is pure — it reports the fence, not a reservation.
    """
    return {
        window.window: floor(window.limit * spec.reserved_fraction)
        for window in windows.declared(limits)
    }


async def reserve_perception(
    tracker: QuotaTracker,
    spec: ModelSpec,
    *,
    scope: keys.Scope,
    estimated_tokens: int,
    request_id: str,
) -> QuotaDecision:
    """Take budget out of the perception lane's fenced-off half.

    The seam Phase 3 left typed, filled in. ``spec`` is always a
    ``perception``-slot candidate (D26's config, `registry.candidates`), never
    an answer-lane one — Step 6's extractor is the only caller.

    Per window: ``rpd``/``tpd`` (the daily fence) charge
    :func:`keys.quota_perception_lane` — Contract C's one perception key —
    against ``floor(perception_budget(spec, limits)[window] * (1 - headroom))``,
    never below 1 for the same reason
    ``QuotaTracker._effective_limit`` never rounds a real model to zero.
    ``rpm``/``tpm`` charge the *same* key chat's own reservation touches,
    against ``floor(published * (1 - headroom))`` — the full ceiling, not the
    answer lane's halved one, because a per-minute collision resolves itself in
    under sixty seconds and fencing it would refuse an extraction while most of
    the provider's own per-minute allowance sits unused (D26's "starvation is a
    daily problem" reasoning).

    A model with no declared limits at all reserves nothing and is always
    allowed — the same "no published limit, no round trip" rule
    ``QuotaTracker.reserve`` follows.

    D26's fine print: Contract C gives the lane exactly *one* daily key, so a
    hypothetical model declaring both ``rpd`` and ``tpd`` would have both fenced
    through the same counter. Nothing in the fleet does — Gemini's ``tpd`` is
    ``null`` in ``limits.yaml`` for both perception candidates — so this is
    correct for every model this lane actually runs against, not a gap being
    quietly papered over.
    """
    # Imported here rather than at module level: `tracker.py` imports this
    # module (for `answer_share`), so importing it back at import time would
    # try to read `WindowGrant` off a `QuotaTracker` module that has not
    # finished executing yet. By the time this function runs, both modules are
    # fully loaded.
    from app.quota.tracker import WindowGrant

    headroom = tracker.headroom
    lane_key = keys.quota_perception_lane(scope, spec.provider, spec.model)
    model_limits = tracker.limits_for(spec)
    daily_budget = perception_budget(spec, model_limits) if model_limits is not None else {}

    grants: list[WindowGrant] = []
    for window in tracker.declared_windows(spec):
        if window.window in _DAILY_WINDOWS:
            limit = max(1, floor(daily_budget.get(window.window, 0) * (1.0 - headroom)))
            key = lane_key
        else:
            limit = max(1, floor(window.limit * (1.0 - headroom)))
            key = keys.quota(scope, spec.provider, spec.model, window.window)
        grants.append(
            WindowGrant(
                window=window.window,
                limit=limit,
                reset=window.reset,
                cost_is_tokens=window.cost_is_tokens,
                key=key,
            )
        )

    return await tracker.reserve_windows(
        tuple(grants),
        scope=scope,
        provider=spec.provider,
        model=spec.model,
        estimated_tokens=estimated_tokens,
        request_id=request_id,
    )


async def commit_perception(
    tracker: QuotaTracker, reservation: Reservation, *, tokens_in: int, tokens_out: int
) -> None:
    """Correct a perception reservation's token estimate to what was spent.

    Delegates outright: ``reservation.counter_keys`` already names the exact
    keys :func:`reserve_perception` reserved against — ``lane:perception`` for
    the daily window, the shared ``rpm``/``tpm`` keys for the rest — so
    ``QuotaTracker.commit`` settles them correctly with no perception-specific
    logic of its own (Step 5's whole point: only *reserving* needed a lane-
    aware entry point).
    """
    await tracker.commit(reservation, tokens_in=tokens_in, tokens_out=tokens_out)


async def release_perception(tracker: QuotaTracker, reservation: Reservation) -> None:
    """Give back a perception reservation that never reached the provider.

    Same reasoning as :func:`commit_perception`: the reservation already knows
    which keys it touched, so ``QuotaTracker.release`` needs nothing extra.
    """
    await tracker.release(reservation)


__all__ = [
    "ANSWER",
    "PERCEPTION",
    "answer_share",
    "commit_perception",
    "perception_budget",
    "release_perception",
    "reserve_perception",
]
