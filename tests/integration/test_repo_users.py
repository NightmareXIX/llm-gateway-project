"""``app/db/repo/users.py`` against a real Postgres.

This repo is one statement, and every interesting thing about it lives in the
parts of that statement SQLite could not run: ``INSERT ... ON CONFLICT DO UPDATE``
with a deliberately *narrow* update set, and ``populate_existing`` to stop the
identity map winning over what was just written.

Three claims are worth the round trip.

**``tier`` survives.** It is ours, not Supabase's. It is absent from the ``set_=``
clause, and nothing except this test says so — if someone "tidies up" that clause
into a full update, every paying user silently drops back to free on their next
request.

**The returned object is the new one.** Without ``populate_existing`` the caller
gets whatever the session already had loaded. One session per request hides it in
production and it is a real staleness bug regardless.

**An email collision is a 409, not a 500.** It happens when a Supabase account is
deleted and recreated while our mirror still holds the old row, and the difference
between the two answers is whether the user is told anything useful.

[test_auth_dependency.py](tests/integration/test_auth_dependency.py) covers the
same code from the endpoint side. Here it is driven directly, so the cases a JWT
cannot easily produce are reachable.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict
from app.db.models import User
from app.db.repo import users as repo

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# normalize_email
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("User@Example.com", "user@example.com"),
        ("  user@example.com  ", "user@example.com"),
        ("\tUSER@EXAMPLE.COM\n", "user@example.com"),
        ("user@example.com", "user@example.com"),
    ],
)
def test_normalization_is_what_makes_the_unique_index_real(raw: str, expected: str) -> None:
    """A unique index on ``email`` only means something if every writer agrees on
    the form. Two rows for ``Bob@x.com`` and ``bob@x.com`` are two accounts."""
    assert repo.normalize_email(raw) == expected


# --------------------------------------------------------------------------- #
# upsert_from_claims
# --------------------------------------------------------------------------- #
async def test_a_first_sighting_creates_the_mirror(db_session: AsyncSession) -> None:
    user_id = uuid4()

    user = await repo.upsert_from_claims(
        db_session, user_id=user_id, email="New@Example.com", email_verified=True
    )

    assert user.id == user_id
    assert user.email == "new@example.com"
    assert user.email_verified is True
    assert user.tier == "free"
    assert user.created_at is not None


async def test_a_second_sighting_updates_rather_than_duplicating(
    db_session: AsyncSession,
) -> None:
    """One statement, not a SELECT then a conditional INSERT — which is a race the
    first time a new user's two requests arrive together."""
    user_id = uuid4()
    await repo.upsert_from_claims(
        db_session, user_id=user_id, email="a@example.com", email_verified=False
    )
    await repo.upsert_from_claims(
        db_session, user_id=user_id, email="b@example.com", email_verified=True
    )

    stored = await repo.get_by_id(db_session, user_id)
    assert stored is not None
    assert stored.email == "b@example.com"
    assert stored.email_verified is True


async def test_a_promotion_to_plus_survives_the_next_login(db_session: AsyncSession) -> None:
    """``tier`` is deliberately absent from the update set. Supabase does not know
    about it and must not be able to overwrite it."""
    user_id = uuid4()
    await repo.upsert_from_claims(
        db_session, user_id=user_id, email="paid@example.com", email_verified=True
    )

    upgraded = await repo.get_by_id(db_session, user_id)
    assert upgraded is not None
    upgraded.tier = "plus"
    await db_session.flush()

    refreshed = await repo.upsert_from_claims(
        db_session, user_id=user_id, email="paid@example.com", email_verified=True
    )

    assert refreshed.tier == "plus"


async def test_the_returned_row_is_the_one_just_written_not_the_cached_one(
    db_session: AsyncSession,
) -> None:
    """``populate_existing``. Load the row first so the identity map holds a stale
    copy, then upsert — the caller must get the new values back, not the old."""
    user_id = uuid4()
    await repo.upsert_from_claims(
        db_session, user_id=user_id, email="stale@example.com", email_verified=False
    )
    cached = await db_session.get(User, user_id)
    assert cached is not None and cached.email_verified is False

    returned = await repo.upsert_from_claims(
        db_session, user_id=user_id, email="fresh@example.com", email_verified=True
    )

    assert returned.email == "fresh@example.com"
    assert returned.email_verified is True


async def test_the_stored_email_is_normalized_not_just_the_compared_one(
    db_session: AsyncSession,
) -> None:
    user_id = uuid4()

    await repo.upsert_from_claims(
        db_session, user_id=user_id, email="  MiXeD@Example.COM ", email_verified=True
    )

    stored = await repo.get_by_id(db_session, user_id)
    assert stored is not None
    assert stored.email == "mixed@example.com"


async def test_an_email_belonging_to_someone_else_is_a_conflict_not_a_500(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """A Supabase account deleted and recreated: same address, new ``sub``. The
    user deserves a sentence they can act on, not a generic failure."""
    await user_factory(email="taken@example.com")

    with pytest.raises(Conflict) as excinfo:
        await repo.upsert_from_claims(
            db_session, user_id=uuid4(), email="taken@example.com", email_verified=True
        )

    assert excinfo.value.code == "email_taken"
    # The provider-facing detail stays out of it; this message is for a person.
    assert "already registered" in str(excinfo.value)


async def test_the_conflict_is_raised_on_the_normalized_form_too(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """Otherwise the collision is only caught when the casing happens to match."""
    await user_factory(email="taken@example.com")

    with pytest.raises(Conflict):
        await repo.upsert_from_claims(
            db_session, user_id=uuid4(), email="TAKEN@Example.com", email_verified=True
        )


# --------------------------------------------------------------------------- #
# get_by_id
# --------------------------------------------------------------------------- #
async def test_get_by_id_returns_the_row(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory(email="known@example.com")

    found = await repo.get_by_id(db_session, user.id)
    assert found is not None
    assert found.email == "known@example.com"


async def test_get_by_id_is_none_for_an_unknown_id(db_session: AsyncSession) -> None:
    """``/v1/me`` turns this into a 404. It means the account was deleted between
    authentication and the read, which is rare and still has to not be a 500."""
    assert await repo.get_by_id(db_session, uuid4()) is None
