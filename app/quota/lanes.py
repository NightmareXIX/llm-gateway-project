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
4 having to re-derive them; :func:`reserve_perception` is the typed seam Phase
4 fills in, per the hard rule against silently-passing stubs — the signature
and its return type are the contract, only the body is deferred.
"""

from __future__ import annotations

from math import floor
from typing import TYPE_CHECKING, Final

from app.cache import keys
from app.config import ModelLimits
from app.providers.types import ModelSpec
from app.quota import windows

if TYPE_CHECKING:
    from app.quota.tracker import QuotaDecision, QuotaTracker

ANSWER: Final = "answer"
PERCEPTION: Final = "perception"


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

    Unimplemented on purpose (the hard rule: a typed signature raising
    ``NotImplementedError``, never a silently-passing stub). The perception
    lane lands in Phase 4, once ``POST /v1/files`` exists to call it; this
    phase's job is only to make sure the budget it will spend is already
    reserved rather than left for an argument. See D8, and §9 of
    ``doc/reference/phase3.md``.
    """
    raise NotImplementedError("the perception lane lands in Phase 4")


__all__ = [
    "ANSWER",
    "PERCEPTION",
    "answer_share",
    "perception_budget",
    "reserve_perception",
]
