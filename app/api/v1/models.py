"""``GET /v1/models`` — live status for every routable slot, no upstream call.

D21: everything this endpoint reports is already local — the breaker's
``cb:{provider}:{model}`` hash, the quota tracker's ``q:{scope}:{provider}:
{model}:*`` counters, the registry's slot table. A status page that called a
provider to build itself would turn a page load into round trips against the
very budgets it exists to report on (trap 11).

**Status per candidate, in order:** the breaker first — ``open`` or
``half_open`` (as *stored*, never as :meth:`~app.routing.circuit_breaker.
CircuitBreaker.allows` would compute it, which claims a probe as a side effect
this read-only endpoint must not have) means ``unavailable``. Otherwise any
exhausted quota window means ``rate_limited``. Otherwise, if the tracker cannot
say — Redis unreachable, or the model declares no windows at all — ``unknown``.
Otherwise ``available``.

**Status per slot** is the best of its candidates (D17's own reasoning, read
backwards): the router fails over silently across a slot's chain, so a slot
with one healthy candidate *is* servable, even if the ones ahead of it are not.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from fastapi import APIRouter

from app.api.v1.chat import CredentialsDep
from app.cache import keys
from app.config import get_providers_config
from app.core.clock import SYSTEM_CLOCK, Clock
from app.deps import BreakerDep, QuotaDep, RegistryDep
from app.keys_resolution.resolver import ResolvedKey, resolves_private_for_every_provider
from app.providers.types import ModelSpec
from app.quota.tracker import QuotaTracker
from app.routing import selection
from app.routing.circuit_breaker import CircuitBreaker
from app.schemas.errors import AUTHENTICATED_ERROR_RESPONSES
from app.schemas.models import (
    CandidateStatus,
    ModelEntry,
    ModelsResponse,
    SlotStatus,
    WindowStatus,
)

router = APIRouter(prefix="/v1", tags=["models"], responses=AUTHENTICATED_ERROR_RESPONSES)

_CREATED_PLACEHOLDER: Final[int] = 0
"""See ``ModelEntry.created``'s docstring — a fixed value, never a real clock."""

_AUTO_DESCRIPTION: Final = "Gateway picks the best available option."
"""Mirrors the overview's own sketch of ``auto``'s entry (§8)."""

_STATUS_RANK: Final[dict[SlotStatus, int]] = {
    "available": 0,
    "rate_limited": 1,
    "unavailable": 2,
    "unknown": 3,
}
"""Best to worst, for :func:`_summarize`. Lower wins."""


@router.get("/models", response_model=ModelsResponse)
async def list_models(
    registry: RegistryDep,
    breaker: BreakerDep,
    quota: QuotaDep,
    credentials: CredentialsDep,
) -> ModelsResponse:
    """Every routable slot, ``auto`` included, with live per-candidate status.

    Authenticated via ``CredentialsDep``, whose own dependency chain requires
    the principal (``_get_credentials`` in ``api/v1/chat.py``) — a request
    with no session gets the same 401 it always did, even though this
    function no longer names ``PrincipalDep`` directly. Personalized per
    caller since Phase 6 Step 9 (§9.7, D41): each candidate's status is
    resolved under *that caller's* real scope, and a slot the shared pool
    cannot serve is listed only for someone who can actually reach it.
    """
    providers_config = get_providers_config()

    resolved_cache: dict[tuple[str, str], ResolvedKey] = {}
    status_cache: dict[tuple[str, str, str], CandidateStatus] = {}

    async def resolved_for(spec: ModelSpec) -> ResolvedKey:
        # Memoized because the same (provider, model) pair can appear in more
        # than one slot's chain and always appears again in `auto`'s fleet.
        # `UserCredentials.for_provider` already memoizes its own database
        # round trip per provider (D38); this cache is what keeps a repeat
        # call from even reaching that lookup.
        if spec.key not in resolved_cache:
            resolved_cache[spec.key] = await credentials.for_provider(spec.provider, spec.model)
        return resolved_cache[spec.key]

    async def status_for(spec: ModelSpec) -> CandidateStatus:
        resolved = await resolved_for(spec)
        cache_key = (*spec.key, resolved.scope)
        if cache_key not in status_cache:
            status_cache[cache_key] = await _candidate_status(
                spec, breaker=breaker, quota=quota, scope=resolved.scope, clock=SYSTEM_CLOCK
            )
        return status_cache[cache_key]

    entries: list[ModelEntry] = []

    fleet = selection.candidates(registry, selection.AUTO)
    fleet_statuses = tuple([await status_for(spec) for spec in fleet])
    auto_status, auto_resets_at = _summarize(fleet_statuses)
    entries.append(
        ModelEntry(
            id=selection.AUTO,
            created=_CREATED_PLACEHOLDER,
            owned_by=None,
            status=auto_status,
            resets_at=auto_resets_at,
            description=_AUTO_DESCRIPTION,
            candidates=fleet_statuses,
        )
    )

    for slot_name in registry.slots():
        specs = registry.candidates(slot_name)
        if registry.requires_private_key(slot_name):
            reachable = await resolves_private_for_every_provider(specs, credentials)
            if not reachable:
                # D41: a slot the caller cannot actually reach is not a slot
                # they should be told about — the same treatment `internal`
                # gets, one layer later because this one *is* routable for
                # someone.
                continue
        statuses = tuple([await status_for(spec) for spec in specs])
        status, resets_at = _summarize(statuses)
        entries.append(
            ModelEntry(
                id=slot_name,
                created=_CREATED_PLACEHOLDER,
                owned_by=specs[0].provider,
                status=status,
                resets_at=resets_at,
                description=providers_config.slots[slot_name].description,
                candidates=statuses,
            )
        )

    return ModelsResponse(data=tuple(entries))


async def _candidate_status(
    spec: ModelSpec,
    *,
    breaker: CircuitBreaker,
    quota: QuotaTracker | None,
    scope: keys.Scope,
    clock: Clock,
) -> CandidateStatus:
    """One ``(provider, model)`` pair's live status. No I/O beyond Redis."""
    decision = await breaker.peek(spec.provider, spec.model)
    if not decision.allowed:
        resets_at = (
            clock.now() + timedelta(seconds=decision.retry_after_s)
            if decision.retry_after_s is not None
            else None
        )
        return CandidateStatus(
            provider=spec.provider,
            model=spec.model,
            status="unavailable",
            breaker_state=decision.state,
            resets_at=resets_at,
        )

    if quota is None:
        # QUOTA_ENFORCEMENT is off: nothing quota-blocks a real request either,
        # so there is nothing honest to report beyond the breaker's answer.
        return CandidateStatus(
            provider=spec.provider,
            model=spec.model,
            status="available",
            breaker_state=decision.state,
        )

    states = await quota.remaining(spec, scope=scope)
    if not states:
        # Redis unreachable, or this model declares no windows at all — the
        # tracker's own contract is that both look like this (see
        # `QuotaTracker.remaining`'s docstring), and neither is confidently
        # "available".
        return CandidateStatus(
            provider=spec.provider,
            model=spec.model,
            status="unknown",
            breaker_state=decision.state,
        )

    windows = tuple(
        WindowStatus(
            window=state.window,
            limit=state.limit,
            remaining=state.remaining,
            resets_at=state.resets_at,
        )
        for state in states
    )

    exhausted = [state for state in states if state.exhausted]
    if exhausted:
        earliest = min(exhausted, key=lambda state: state.resets_at)
        return CandidateStatus(
            provider=spec.provider,
            model=spec.model,
            status="rate_limited",
            breaker_state=decision.state,
            resets_at=earliest.resets_at,
            windows=windows,
        )

    return CandidateStatus(
        provider=spec.provider,
        model=spec.model,
        status="available",
        breaker_state=decision.state,
        windows=windows,
    )


def _summarize(
    candidates: tuple[CandidateStatus, ...],
) -> tuple[SlotStatus, datetime | None]:
    """A slot's status is the best of its candidates; its ``resets_at`` is the
    earliest across *all* of them, not just the ones sharing the winning status
    — the slot becomes servable again the moment any one candidate does, and
    that is the honest answer to "when might this come back".
    """
    if not candidates:
        return "unknown", None

    status = min((candidate.status for candidate in candidates), key=lambda s: _STATUS_RANK[s])
    if status == "available":
        return status, None

    resets = [candidate.resets_at for candidate in candidates if candidate.resets_at is not None]
    return status, min(resets) if resets else None
