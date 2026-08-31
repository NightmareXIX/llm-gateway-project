"""``GET /v1/admin/*`` — the usage dashboard's data, self-scoped (D44).

**"Admin" here means this account's own operational view, not "everyone's".**
``Principal`` is frozen at four fields and ``users`` has no role column
(``development-plan.md``/``project-overview.md`` both sketch this as an
"admin dashboard" against ``/admin/*``, but there is no operator identity in
this system to gate one with). Every route below scopes to the calling
principal's own ``user_id``, in the SQL itself, exactly like ``conversations``
and ``files`` — no role, no allowlist, no ``is_admin``. The module keeps its
designated name because §3's repo tree named it and renaming a slot is churn.

Three routes:

* ``GET /usage`` — the dashboard's four aggregates (D45) plus simulated cost
  (D46), one window at a time.
* ``GET /quota`` — per-candidate live status under the caller's own resolved
  scope. This is **not a per-user database read** — quota utilization is a
  property of a *pool*, and which pool the caller draws from is exactly what
  Phase 6's resolver answers. Rather than re-deriving that computation, this
  route calls ``api.v1.models.list_models`` directly with this request's own
  dependencies, so the two pages are answering the same question through the
  same code and cannot quietly disagree.
* ``GET /requests`` — the existing ``list_for_user``, mapped to a wire model.
  The "show me my last few calls" table underneath the charts; not the
  dashboard's own aggregate query, which is ``/usage`` above.

Breaker state is not duplicated into this module either: it is already
visible per-candidate through ``/v1/models`` (and through ``/quota`` above,
which shares that endpoint's computation), so there is nothing for a second
copy here to say.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Query

from app.api.v1 import models as models_routes
from app.api.v1.chat import CredentialsDep
from app.auth.dependency import PrincipalDep
from app.core.clock import SYSTEM_CLOCK
from app.db.models import Request
from app.db.repo import requests as requests_repo
from app.deps import BreakerDep, QuotaDep, RegistryDep, SessionDep
from app.schemas.admin import (
    OutcomeSummaryOut,
    PoolSplitOut,
    ProviderSliceOut,
    RequestOut,
    RequestsOut,
    UsageOverview,
    VolumePointOut,
    Window,
)
from app.schemas.errors import AUTHENTICATED_ERROR_RESPONSES
from app.schemas.models import ModelsResponse
from app.usage.pricing import default_currency, simulated_cost

router = APIRouter(prefix="/v1/admin", tags=["admin"], responses=AUTHENTICATED_ERROR_RESPONSES)

DEFAULT_REQUESTS_LIMIT = 50
MAX_REQUESTS_LIMIT = 200


@router.get("/usage", response_model=UsageOverview)
async def usage_overview(
    principal: PrincipalDep,
    session: SessionDep,
    window: Window = Query(default="24h"),
) -> UsageOverview:
    """One window's worth of the caller's own traffic, costed at read time.

    Four repo calls, all scoped to ``principal.user_id`` in their own SQL, all
    against the same floored ``since`` (``requests_repo.window_span``) — a
    summary computed over a different span than the chart above it would be a
    dashboard that contradicts itself. Costing is the only logic added here:
    ``provider_distribution``'s rows carry token counts, not money (D45), and
    turning them into money is this step's whole job.
    """
    now = SYSTEM_CLOCK.now()
    since, until = requests_repo.window_span(window, now)

    outcomes = await requests_repo.outcome_summary(session, user_id=principal.user_id, since=since)
    volume = await requests_repo.volume_series(
        session, user_id=principal.user_id, window=window, now=now
    )
    providers = await requests_repo.provider_distribution(
        session, user_id=principal.user_id, since=since
    )
    pool = await requests_repo.pool_split(session, user_id=principal.user_id, since=since)

    provider_slices: list[ProviderSliceOut] = []
    total_cost = Decimal("0")
    priced_tokens = 0
    unpriced_requests = 0
    any_priced = False

    for slice_ in providers:
        cost = (
            simulated_cost(
                slice_.provider,
                slice_.model,
                tokens_in=slice_.tokens_in,
                tokens_out=slice_.tokens_out,
            )
            if slice_.model is not None
            else None
        )
        if cost is None:
            unpriced_requests += slice_.requests
        else:
            any_priced = True
            total_cost += cost
            priced_tokens += slice_.tokens_in + slice_.tokens_out
        provider_slices.append(
            ProviderSliceOut(
                provider=slice_.provider,
                model=slice_.model,
                requests=slice_.requests,
                tokens_in=slice_.tokens_in,
                tokens_out=slice_.tokens_out,
                simulated_cost=cost,
            )
        )

    # A single blended rate over this window's own priced traffic — the only
    # thing `pool_split`'s token counts (no per-model breakdown) can honestly
    # be multiplied against. See `PoolSplitOut`'s docstring.
    blended_rate = (total_cost / priced_tokens) if any_priced and priced_tokens > 0 else None
    pool_split_out = PoolSplitOut(
        shared_requests=pool.shared_requests,
        shared_tokens_in=pool.shared_tokens_in,
        shared_tokens_out=pool.shared_tokens_out,
        shared_cost=(
            blended_rate * (pool.shared_tokens_in + pool.shared_tokens_out)
            if blended_rate is not None
            else None
        ),
        private_requests=pool.private_requests,
        private_tokens_in=pool.private_tokens_in,
        private_tokens_out=pool.private_tokens_out,
        private_cost=(
            blended_rate * (pool.private_tokens_in + pool.private_tokens_out)
            if blended_rate is not None
            else None
        ),
    )

    return UsageOverview(
        window=window,
        since=since,
        until=until,
        outcomes=OutcomeSummaryOut(
            total=outcomes.total,
            ok=outcomes.ok,
            errors=outcomes.errors,
            cache_hits=outcomes.cache_hits,
            replays=outcomes.replays,
            substituted=outcomes.substituted,
            multi_attempt=outcomes.multi_attempt,
            tokens_in=outcomes.tokens_in,
            tokens_out=outcomes.tokens_out,
            wasted_tokens_out=outcomes.wasted_tokens_out,
        ),
        volume=tuple(
            VolumePointOut(
                bucket_start=point.bucket_start,
                total=point.total,
                errors=point.errors,
                cache_hits=point.cache_hits,
            )
            for point in volume
        ),
        providers=tuple(provider_slices),
        pool_split=pool_split_out,
        total_cost=total_cost if any_priced else None,
        currency=default_currency() if any_priced else None,
        unpriced_requests=unpriced_requests,
    )


@router.get("/quota", response_model=ModelsResponse)
async def quota_overview(
    registry: RegistryDep,
    breaker: BreakerDep,
    quota: QuotaDep,
    credentials: CredentialsDep,
) -> ModelsResponse:
    """Live per-candidate status under the caller's own resolved scope.

    Authenticated through ``CredentialsDep``'s own dependency chain, the same
    way ``list_models`` is (see that function's docstring) — no separate
    ``PrincipalDep`` needed here either. Delegates to ``list_models`` itself
    rather than re-deriving its computation: a shared-pool user sees the
    shared pool's remainder (already visible on ``/v1/models``), a private-key
    user sees their own, and calling the same function is what guarantees the
    two pages report identical numbers rather than merely similar ones.
    """
    return await models_routes.list_models(registry, breaker, quota, credentials)


def _request_out(row: Request) -> RequestOut:
    return RequestOut(
        id=row.id,
        created_at=row.created_at,
        requested_slot=row.requested_slot,
        served_slot=row.served_slot,
        provider=row.provider,
        model=row.model,
        status=row.status,
        tokens_in=row.tokens_in,
        tokens_out=row.tokens_out,
        latency_ms=row.latency_ms,
        ttft_ms=row.ttft_ms,
        cache_hit=row.cache_hit,
        substituted=row.substituted,
        error_code=row.error_code,
        quota_scope=row.quota_scope,
    )


@router.get("/requests", response_model=RequestsOut)
async def recent_requests(
    principal: PrincipalDep,
    session: SessionDep,
    limit: int = Query(default=DEFAULT_REQUESTS_LIMIT, ge=1, le=MAX_REQUESTS_LIMIT),
) -> RequestsOut:
    """The caller's most recent calls, most recent first.

    ``requests_repo.list_for_user`` already exists for exactly this — its own
    docstring says the dashboard's aggregates are a different query — so this
    route is a limit-bounded read plus a mapping to a wire model."""
    rows = await requests_repo.list_for_user(session, user_id=principal.user_id, limit=limit)
    return RequestsOut(data=tuple(_request_out(row) for row in rows))
