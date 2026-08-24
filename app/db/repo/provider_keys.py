"""Reads and writes against ``provider_keys`` — a user's own BYOK credentials.

Two things this module holds the line on, the same two ``api_keys.py`` and
``files.py`` already hold.

**Ownership is scoped in the SQL, always.** Every function takes a ``user_id``
and puts it in the WHERE clause alongside ``owner_type = 'user'``. D37 means
this module never writes or reads an ``owner_type='system'`` row — that half
of the table exists for a later phase, not this one.

**The plaintext never arrives here.** Callers encrypt first
(``app/core/crypto.py``) and pass the ciphertext. Nothing in this module can
log a live credential because nothing in this module has ever seen one.

Repositories take a session and never commit. The caller owns the transaction
boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.db.models import ProviderKey

LAST_USED_THROTTLE_S = 60.0
"""Mirrors ``api_keys.LAST_USED_THROTTLE_S`` — same column, same reasoning:
minute-granularity is enough for what the column answers ("is this key still
in use"), and writing it on every resolve would turn a read into a write lock
on the hot path of every request."""


async def list_for_user(session: AsyncSession, user_id: UUID) -> Sequence[ProviderKey]:
    """Every key this user has ever added, active and revoked alike.

    The settings page's own list — "I removed that key last week" is the
    answer to half the questions this query gets opened to answer, the same
    reasoning ``api_keys.list_for_user`` already carries.
    """
    result = await session.execute(
        select(ProviderKey)
        .where(ProviderKey.owner_type == "user", ProviderKey.owner_id == user_id)
        .order_by(ProviderKey.provider)
    )
    return result.scalars().all()


async def get_active(session: AsyncSession, *, user_id: UUID, provider: str) -> ProviderKey | None:
    """This user's live key for one provider, or ``None``.

    The partial unique index guarantees at most one row can match, so this is
    always a single-row lookup rather than "the most recent of several".
    """
    result = await session.execute(
        select(ProviderKey).where(
            ProviderKey.owner_type == "user",
            ProviderKey.owner_id == user_id,
            ProviderKey.provider == provider,
            ProviderKey.is_active.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def list_active_for_user(session: AsyncSession, user_id: UUID) -> Sequence[ProviderKey]:
    """Every live key this user holds, across every provider, in one query.

    This is the resolver's one query (D38) — ``UserCredentials`` loads this
    once per request and memoizes it, rather than issuing one lookup per
    candidate attempt.
    """
    result = await session.execute(
        select(ProviderKey).where(
            ProviderKey.owner_type == "user",
            ProviderKey.owner_id == user_id,
            ProviderKey.is_active.is_(True),
        )
    )
    return result.scalars().all()


async def upsert(
    session: AsyncSession,
    *,
    user_id: UUID,
    provider: str,
    encrypted_key: str,
    last_4: str,
    nickname: str | None,
    validation_status: str,
    last_validated_at: datetime | None,
) -> ProviderKey:
    """Add or replace this user's key for one provider.

    Deactivates any existing active row for ``(user_id, provider)`` and
    inserts a fresh one, in that order within the caller's transaction. The
    partial unique index makes the deactivate mandatory rather than optional —
    two active rows for the same ``(owner_id, provider)`` would violate it,
    and "replace" is remove-then-add by design (D36): there is no key-rotation
    flow in v1, so this is the only path that ever changes a stored key.
    """
    await deactivate(session, user_id=user_id, provider=provider)
    key = ProviderKey(
        owner_type="user",
        owner_id=user_id,
        provider=provider,
        encrypted_key=encrypted_key,
        last_4=last_4,
        nickname=nickname,
        validation_status=validation_status,
        last_validated_at=last_validated_at,
    )
    session.add(key)
    await session.flush()
    return key


async def deactivate(session: AsyncSession, *, user_id: UUID, provider: str) -> bool:
    """Soft-delete this user's key for one provider.

    Mirrors ``api_keys.revoke``'s idempotence exactly, including why: the
    WHERE clause does not filter on the row's current ``is_active`` state, so
    a second removal still matches and still returns ``True`` — the caller's
    intent ("this provider must not use my key") is satisfied either way, the
    same reasoning that makes a repeat ``DELETE`` a ``204`` rather than a
    ``404``. Returns ``False`` only when this user has never added a key for
    this provider at all — including when the provider belongs to someone
    else, since the WHERE clause never distinguishes "not yours" from "never
    existed" (the same 404-not-403 rule every ownership-scoped repo here
    follows).
    """
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(ProviderKey)
            .where(
                ProviderKey.owner_type == "user",
                ProviderKey.owner_id == user_id,
                ProviderKey.provider == provider,
            )
            .values(is_active=False)
        ),
    )
    return result.rowcount > 0


async def touch_last_used(session: AsyncSession, *, key_id: UUID, clock: Clock) -> None:
    """Record that a key was resolved, at most once per ``LAST_USED_THROTTLE_S``.

    Copied from ``api_keys.touch_last_used`` including the reasoning: the
    throttle sits in the WHERE clause rather than in Python, so two concurrent
    resolves reading the same stale timestamp cannot both decide to write —
    Postgres evaluating the predicate makes the check and the write one
    operation. Called with no ``user_id`` filter because ``key_id`` already
    names one row; the resolver holds the row it just loaded under its own
    ownership-scoped query, so there is nothing left to re-check here.
    """
    now = clock.now()
    cutoff = now - timedelta(seconds=LAST_USED_THROTTLE_S)
    await session.execute(
        update(ProviderKey)
        .where(
            ProviderKey.id == key_id,
            (ProviderKey.last_used_at.is_(None)) | (ProviderKey.last_used_at < cutoff),
        )
        .values(last_used_at=now)
    )


async def mark_invalid(session: AsyncSession, *, key_id: UUID) -> None:
    """D40's disclosure write: a private key just failed with ``AuthFailed``.

    Fire-and-forget, in its own session, never blocking the request that
    discovered it — the failing candidate has already moved on to the next
    one in the chain by the time this matters. Not called on ``RateLimited``:
    a spent key is still a working key.
    """
    await session.execute(
        update(ProviderKey).where(ProviderKey.id == key_id).values(validation_status="invalid")
    )
