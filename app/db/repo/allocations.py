"""Reads against ``user_quota_allocations`` — D39's personal daily cap.

Read-only in Phase 6 Step 1, and read-only for the whole phase per
``phase6.md``: rows are written by hand or a seed script until an admin
surface exists (Phase 7's ``api/admin.py``). This module holds the one query
that matters until then.

Not ownership-scoped the way ``conversations``/``files`` are — there is no
"someone else's cap" a caller could read by mistake, because the caller
always supplies its own ``user_id`` from the resolved principal, never one
taken from a request body.

Repositories take a session and never commit.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UserQuotaAllocation


async def list_for_user(session: AsyncSession, user_id: UUID) -> Sequence[UserQuotaAllocation]:
    """Every personal-cap override this user holds, in one query.

    Not in the step's original one-function plan — added so
    ``keys_resolution.resolver.UserCredentials`` can batch-load every
    allocation row in the same session as its provider-key load (D38), rather
    than issuing a second round trip per candidate the way a bare
    :func:`get_cap` call per attempt would. Mirrors
    ``provider_keys_repo.list_active_for_user``'s reasoning exactly: a
    failover chain crossing several providers must cost one query, not one per
    candidate.
    """
    result = await session.execute(
        select(UserQuotaAllocation).where(UserQuotaAllocation.user_id == user_id)
    )
    return result.scalars().all()


async def get_cap(session: AsyncSession, *, user_id: UUID, provider: str, model: str) -> int | None:
    """This user's personal daily cap on the shared pool for one model.

    ``None`` means no override row exists — the caller falls back to
    ``config/limits.yaml``'s ``shared_pool_daily_cap`` tier default, and if
    that is also unset, there is no personal cap at all (D39).
    """
    result = await session.execute(
        select(UserQuotaAllocation.daily_cap).where(
            UserQuotaAllocation.user_id == user_id,
            UserQuotaAllocation.provider == provider,
            UserQuotaAllocation.model == model,
        )
    )
    return result.scalar_one_or_none()
