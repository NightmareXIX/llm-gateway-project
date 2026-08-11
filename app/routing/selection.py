"""Slot selection policy — ``auto`` versus a named slot.

**Why this is not in the registry.** ``registry.py`` is the config table: it
answers "what can serve the ``general`` slot, in what order". ``auto`` is not a
slot in ``config/providers.yaml`` and never will be — it is a *policy sentinel*
meaning "you choose". Teaching the config table to understand a name that is not
in the config would make the table the arbiter of routing policy, which is the
job this module and Phase 2's ``router.py`` own.

**Why it is not inline in the endpoint.** Phase 2 turns the single spec Phase 1
resolves into an ordered candidate list, and Phase 3 filters that list by
remaining quota before the first attempt is made. Both grow here. An endpoint
holding ``"general" if requested == "auto" else requested`` would have to give it
back.

Nothing in this module does I/O or touches a session, so it is a pure function of
the registry — which is what lets Phase 3's ``/v1/models`` reuse it without
standing up a request.
"""

from __future__ import annotations

from app.providers.registry import ProviderRegistry, UnknownSlot
from app.providers.types import ModelSpec

AUTO = "auto"
"""What a client sends to say "gateway, you pick".

Also ``conversations.preferred_slot``'s default, so a thread started without an
explicit choice keeps deferring rather than freezing today's best slot into a
row that outlives the config that produced it.
"""


def resolve_slot(registry: ProviderRegistry, requested: str) -> ModelSpec:
    """The one model Phase 1 will attempt.

    ``auto`` resolves to the first routable slot in config order — ``general``,
    given the current ``providers.yaml``. Ordering *is* the policy here: the
    registry drops slots whose every candidate sits on a disabled provider, so
    "first routable" cannot select something that can never be served.

    A named slot resolves to its highest-priority candidate. Phase 1 stops there
    because there is no failover yet; Phase 2 calls :func:`candidates` instead
    and walks the whole list.

    Raises :class:`~app.providers.registry.UnknownSlot` for a name the table does
    not carry. Deliberately not translated to an HTTP error here — the same
    lookup failure is a client's 400 when the name came off a request body and
    our 500 when it came from ``conversations.preferred_slot``, and only the
    caller knows which.
    """
    if requested == AUTO:
        slots = registry.slots()
        if not slots:  # pragma: no cover — build_registry refuses to construct this
            raise UnknownSlot("no routable slots are configured")
        return registry.primary(slots[0])

    return registry.primary(requested)


def candidates(
    registry: ProviderRegistry,
    requested: str,
    *,
    pinned: str | None = None,
) -> tuple[ModelSpec, ...]:
    """The ordered failover chain for a request — Phase 2's D1/D2 loop input.

    ``auto`` will expand to every routable candidate across slots in priority
    order; a named slot to that slot's own candidates (D2: silently fail over,
    then disclose). ``pinned`` is D3's override — once a conversation has made a
    tool call, ``conversations.pinned_model`` wins over both.
    """
    raise NotImplementedError(
        "Phase 2: the failover candidate chain (D1/D2) and pinning (D3) land with routing/router.py"
    )
