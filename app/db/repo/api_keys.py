"""Reads and writes against ``api_keys`` — the gateway's own ``gw_live_`` keys.

Two things this module holds the line on.

**Ownership is scoped in the SQL, never checked after the fetch.** ``revoke`` and
``list_for_user`` both carry ``user_id`` in the WHERE clause. A caller asking
about someone else's key gets nothing back, and the endpoint turns that into a
404 — a 403 would confirm the key exists.

**The plaintext never arrives here.** Callers hash first (``app/auth/api_keys.py``)
and pass the digest. Nothing in this module can log a live credential because
nothing in this module has ever seen one.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.db.models import ApiKey, User

LAST_USED_THROTTLE_S = 60.0
"""How stale ``last_used_at`` is allowed to get before a request pays for a write.

Updating it on every call turns a read-only request into a write and puts a row
lock on the hot path of every integration. Minute-granularity is enough for what
the column is actually for: "is this key still in use, or can it be revoked?"
"""


async def create(
    session: AsyncSession,
    *,
    user_id: UUID,
    key_hash: str,
    key_prefix: str,
    last_4: str,
    nickname: str | None = None,
) -> ApiKey:
    """Persist a newly minted key. The caller still holds the only plaintext copy."""
    api_key = ApiKey(
        user_id=user_id,
        key_hash=key_hash,
        key_prefix=key_prefix,
        last_4=last_4,
        nickname=nickname,
    )
    session.add(api_key)
    await session.flush()  # assigns defaults (created_at, is_active) before we return
    return api_key


async def list_for_user(session: AsyncSession, user_id: UUID) -> Sequence[ApiKey]:
    """Every key this user has, revoked ones included.

    Revoked keys are kept and shown: "I revoked that one last week" is the answer
    to half the questions this list gets opened to answer.
    """
    result = await session.execute(
        select(ApiKey).where(ApiKey.user_id == user_id).order_by(ApiKey.created_at.desc())
    )
    return result.scalars().all()


async def get_active_with_user(session: AsyncSession, key_hash: str) -> tuple[ApiKey, User] | None:
    """Resolve a key digest to its key row and owner, or ``None``.

    One join rather than two round-trips: the auth path needs the key (for
    ``api_key_id``) and the user (for ``tier``) on every single request, and this
    sits in front of every endpoint in the gateway.

    Filters on ``is_active`` in the query, so a revoked key is indistinguishable
    from one that never existed.
    """
    result = await session.execute(
        select(ApiKey, User)
        .join(User, User.id == ApiKey.user_id)
        .where(ApiKey.key_hash == key_hash, ApiKey.is_active.is_(True))
    )
    row = result.first()
    if row is None:
        return None
    return row[0], row[1]


async def revoke(session: AsyncSession, *, key_id: UUID, user_id: UUID) -> bool:
    """Deactivate a key. Returns False when it is not this user's to revoke.

    Soft delete: ``requests.api_key_id`` points here, and the usage dashboard
    should still be able to say which integration made a call after the key that
    made it was rotated out.

    Idempotent — revoking an already-revoked key still returns True, because the
    caller's intent ("this key must not work") is satisfied either way.
    """
    # `execute` is typed as returning Result; an UPDATE always yields a
    # CursorResult, which is the one that knows how many rows it touched.
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(ApiKey)
            .where(ApiKey.id == key_id, ApiKey.user_id == user_id)
            .values(is_active=False)
        ),
    )
    return result.rowcount > 0


async def touch_last_used(session: AsyncSession, *, key_id: UUID, clock: Clock) -> None:
    """Record that a key was used, at most once per ``LAST_USED_THROTTLE_S``.

    The throttle is in the WHERE clause rather than in Python: two concurrent
    requests would both read the same stale timestamp and both decide to write.
    Postgres evaluating the predicate makes the check and the write one operation.
    """
    now = clock.now()
    cutoff = now - timedelta(seconds=LAST_USED_THROTTLE_S)
    await session.execute(
        update(ApiKey)
        .where(
            ApiKey.id == key_id,
            (ApiKey.last_used_at.is_(None)) | (ApiKey.last_used_at < cutoff),
        )
        .values(last_used_at=now)
    )
