"""``app/db/repo/allocations.py`` against a real Postgres.

One function, ``get_cap``, and one thing worth proving about it: it is a pure
override lookup, keyed on the exact ``(user_id, provider, model)`` triple, with
no fallback logic of its own — the tier default in ``config/limits.yaml`` is a
concern of whatever calls this, not of the query. ``None`` is the only signal
this module ever returns for "no override", and it means exactly that.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UserQuotaAllocation
from app.db.repo import allocations as repo

pytestmark = pytest.mark.integration


async def _grant(
    session: AsyncSession, *, user: Any, provider: str, model: str, daily_cap: int
) -> None:
    session.add(
        UserQuotaAllocation(user_id=user.id, provider=provider, model=model, daily_cap=daily_cap)
    )
    await session.flush()


async def test_get_cap_returns_the_stored_override(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    await _grant(db_session, user=user, provider="gemini", model="gemini-3.6-flash", daily_cap=50)

    cap = await repo.get_cap(
        db_session, user_id=user.id, provider="gemini", model="gemini-3.6-flash"
    )

    assert cap == 50


async def test_get_cap_is_none_with_no_row(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """No override, no configured tier default: D39's "there is no personal
    cap at all" case."""
    user = await user_factory()
    assert (
        await repo.get_cap(db_session, user_id=user.id, provider="gemini", model="gemini-3.6-flash")
        is None
    )


async def test_get_cap_is_scoped_to_the_exact_model(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """A cap on one model of a provider must not leak onto a sibling model —
    the shared pool's remaining budget differs per model, not just per provider."""
    user = await user_factory()
    await _grant(db_session, user=user, provider="gemini", model="gemini-3.6-flash", daily_cap=50)

    assert (
        await repo.get_cap(db_session, user_id=user.id, provider="gemini", model="gemini-3.6-pro")
        is None
    )


async def test_get_cap_does_not_read_another_users_allocation(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    owner = await user_factory()
    other = await user_factory()
    await _grant(db_session, user=owner, provider="gemini", model="gemini-3.6-flash", daily_cap=50)

    assert (
        await repo.get_cap(
            db_session, user_id=other.id, provider="gemini", model="gemini-3.6-flash"
        )
        is None
    )


async def test_get_cap_for_an_unknown_user_id_is_none(db_session: AsyncSession) -> None:
    assert (
        await repo.get_cap(db_session, user_id=uuid4(), provider="gemini", model="gemini-3.6-flash")
        is None
    )


async def test_a_second_row_for_the_same_triple_is_refused(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """The unique constraint: one cap per ``(user, provider, model)``, not a
    history of caps to pick the latest from."""
    user = await user_factory()
    await _grant(db_session, user=user, provider="gemini", model="gemini-3.6-flash", daily_cap=50)

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                UserQuotaAllocation(
                    user_id=user.id,
                    provider="gemini",
                    model="gemini-3.6-flash",
                    daily_cap=100,
                )
            )
            await db_session.flush()


async def test_a_non_positive_cap_is_refused(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                UserQuotaAllocation(
                    user_id=user.id, provider="gemini", model="gemini-3.6-flash", daily_cap=0
                )
            )
            await db_session.flush()
