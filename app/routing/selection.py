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
from app.usage.metrics import LatencySnapshot

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
    latency: LatencySnapshot | None = None,
) -> tuple[ModelSpec, ...]:
    """The ordered failover chain for a request — Phase 2's D1/D2 loop input.

    Three rules, applied in this order:

    **A pin wins outright (D3).** Once a conversation has made a tool call,
    ``conversations.pinned_model`` names the one model whose tool-call history is
    intelligible, and a chain of alternatives is exactly the wrong answer. The
    result is a one-element tuple and ``requested`` is ignored.

    **A named slot leads, then spills (D10).** Its own candidates come first in
    priority order; the rest of the fleet follows, minus what is already in the
    list. D2 says an exhausted slot fails over silently and then discloses, and
    without the spill there is nothing to disclose — ``substituted`` compares the
    requested slot to the served one, and inside a single slot those can never
    differ. See ADR-011.

    **``auto`` expands to the whole fleet.** Slots in ``providers.yaml``
    declaration order, each slot's candidates in priority order, flattened and
    de-duplicated on ``(provider, model)`` keeping the first occurrence.
    De-duplication is on the *pair*, not the provider: two Groq models are two
    genuinely different candidates, since free-tier limits are per-model, and
    collapsing on provider would erase a whole failover chain.

    ``latency`` reorders the finished list where it has evidence (D11, ADR-014).
    It is a parameter rather than a module-level table read so this function stays
    pure — a function of (registry, request, snapshot) — which is what keeps its
    tests a table and lets Phase 3's ``/v1/models`` call it without standing up a
    request. Passing ``None`` is config order, exactly.

    Raises :class:`~app.providers.registry.UnknownSlot` for a slot name or a pin
    the table does not carry, for the reason :func:`resolve_slot` documents: the
    same lookup failure is a 400 or a 500 depending on where the name came from,
    and only the caller knows which.
    """
    if pinned is not None:
        return (_resolve_pin(registry, pinned),)

    fleet = _fleet(registry)

    if requested == AUTO:
        chain = fleet
    else:
        named = registry.candidates(requested)
        seen = {spec.key for spec in named}
        chain = [*named, *(spec for spec in fleet if spec.key not in seen)]

    return _rank(chain, latency)


def _fleet(registry: ProviderRegistry) -> list[ModelSpec]:
    """Every routable candidate, once, in config order — the ``auto`` chain."""
    flattened: list[ModelSpec] = []
    seen: set[tuple[str, str]] = set()

    for slot in registry.slots():
        for spec in registry.candidates(slot):
            if spec.key in seen:
                continue
            seen.add(spec.key)
            flattened.append(spec)

    return flattened


def _resolve_pin(registry: ProviderRegistry, pinned: str) -> ModelSpec:
    """Find the spec a ``provider/model`` pin names.

    The format is ``provider/model`` — what ``registry.describe()`` prints, and
    what the ``conversations.pinned_model`` column is named for. A *slot* name
    would have been simpler and is wrong: a slot's primary candidate changes with
    a ``providers.yaml`` edit, so the pin would silently move to a different
    model, which is the one thing pinning exists to prevent.

    A pinned model that has left the config — a provider disabled, a free tier
    withdrawn — raises rather than falling back to the fleet. Serving a pinned
    conversation on some other model is how tool-call history becomes incoherent,
    and D3 scopes that out rather than papering over it.
    """
    for slot in registry.slots():
        for spec in registry.candidates(slot):
            if f"{spec.provider}/{spec.model}" == pinned:
                return spec

    raise UnknownSlot(f"pinned model {pinned!r} is not in the routable table")


def _rank(chain: list[ModelSpec], latency: LatencySnapshot | None) -> tuple[ModelSpec, ...]:
    """Reorder by measured latency, where there is enough evidence to (D11).

    **Ranked candidates trade places with each other; nothing else moves.** The
    positions held by candidates the snapshot has an opinion about are collected,
    those candidates are sorted by EWMA, and they are written back into exactly
    those positions. A candidate below the sample threshold keeps its config
    index, so a cold process reproduces config order *exactly* rather than
    scattering unmeasured candidates to one end of the list.

    Note where this does **not** happen: inside the breaker filter. Availability
    outranks latency, and it does so because the router checks the breaker per
    candidate as it walks this list — a skip costs no attempt (D12), so a fast
    candidate sorted to the front cannot consume the budget of a healthy one
    behind it.
    """
    if latency is None:
        return tuple(chain)

    measured: list[tuple[int, float, ModelSpec]] = []
    for index, spec in enumerate(chain):
        ewma_ms = latency.ranking_for(spec)
        if ewma_ms is not None:
            measured.append((index, ewma_ms, spec))

    if len(measured) < 2:
        # One opinion cannot reorder anything against itself.
        return tuple(chain)

    # Stable, so two candidates with identical EWMAs keep their config order.
    ordered = sorted(measured, key=lambda entry: entry[1])

    result = list(chain)
    for (position, _, _), (_, _, spec) in zip(measured, ordered, strict=True):
        result[position] = spec
    return tuple(result)
