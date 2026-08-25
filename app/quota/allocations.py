"""D39's personal cap on the *shared* pool — the mirror image of ``lanes.py``.

§9.4's shared path must check two things: the global shared-pool remaining
(what ``QuotaTracker.reserve`` already enforces) *and* this one user's own
daily slice of it. The private path needs none of this — nothing is being
shared, so ``scope=str(user_id)`` and the existing ``q:{scope}:…`` counters
already give a user their own, uncapped budget (D39).

**This is not D20 again.** ``deps.RateLimiter`` limits how many requests one
user may make *of the gateway*, across every provider, and fails open — it
protects the gateway's own capacity. This limits how much of *one provider's*
shared free tier one user may consume, *per model*, and fails closed with the
rest of quota (Contract C) — a user over their personal cap is exactly as
unservable, on the shared path, as a candidate whose ``rpm`` is spent. They
read as duplicates to a glance; they are answering different questions with
different failure rules.

Nothing here does I/O — the cap value itself is resolved earlier, by
``keys_resolution/resolver.py`` alongside the credential it already has to
look up (D38's one query), and handed to :func:`shared_pool_grants` as a bare
number. That split is what lets this module stay as pure as ``lanes.py``'s
own :func:`~app.quota.lanes.perception_budget`: it reports the fence, it does
not go fetch it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from app.cache import keys
from app.providers.types import ModelSpec

if TYPE_CHECKING:
    from app.quota.tracker import WindowGrant


def shared_pool_grants(
    spec: ModelSpec, *, user_id: UUID, cap: int | None
) -> tuple[WindowGrant, ...]:
    """§9.4's extra ceiling on the shared path. Empty when there is no cap.

    One ``rpd``-labelled grant at :func:`keys.user_allocation`, reusing the
    window label the model's own daily counter uses (see that builder's
    docstring for why the shared hash field this produces inside
    ``reserve.lua``'s reservation record is harmless) — appended by the router
    to the grants ``QuotaTracker.reserve`` would have built on its own, and
    reserved atomically alongside them via
    :meth:`~app.quota.tracker.QuotaTracker.reserve_windows`.

    ``cost_is_tokens=False``: like the model's own ``rpd``, this counts
    requests, not tokens — a user's personal allowance is "N calls a day",
    never "N tokens a day".
    """
    if cap is None:
        return ()

    # Imported here rather than at module level: `tracker.py` imports
    # `lanes.py` (for `answer_share`) at its own top level, and this module
    # sits beside it in the same import order — deferring avoids the same
    # partially-initialized-module hazard `lanes.py::reserve_perception`
    # already documents for its own `WindowGrant` import.
    from app.quota.tracker import WindowGrant

    return (
        WindowGrant(
            window="rpd",
            limit=cap,
            reset="rolling_daily",
            cost_is_tokens=False,
            key=keys.user_allocation(user_id, spec.provider, spec.model),
        ),
    )


__all__ = ["shared_pool_grants"]
