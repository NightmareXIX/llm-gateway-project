"""``app/db/repo/api_keys.py`` against a real Postgres.

Three behaviours here are only observable from the database side, which is why
they get a repo suite rather than being left to
[test_keys_endpoints.py](tests/integration/test_keys_endpoints.py).

**A revoked key is indistinguishable from one that never existed.**
``get_active_with_user`` filters on ``is_active`` inside the query, so the auth
path cannot accidentally learn that a digest is real but disabled — which is the
same reason revoking someone else's key is a 404 and not a 403.

**Revocation is a soft delete, and is idempotent.** ``requests.api_key_id`` points
here, so a hard delete would erase attribution every time somebody rotates a key.
And "this key must not work" is satisfied whether or not it already was.

**The ``last_used_at`` throttle lives in the WHERE clause.** In Python, two
concurrent requests read the same stale timestamp and both decide to write.
Letting Postgres evaluate the predicate makes the check and the write one
operation — and the only way to test it without sleeping for a minute is a
``FixedClock`` that a test advances, which is exactly what that seam exists for.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FixedClock
from app.core.crypto import sha256_hex
from app.db.repo import api_keys as repo

pytestmark = pytest.mark.integration


def _digest(label: str) -> str:
    """A stand-in for a hashed key. The plaintext never reaches this module."""
    return sha256_hex(f"gw_live_{label}")


# --------------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------------- #
async def test_create_returns_a_row_with_its_defaults_already_set(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()

    api_key = await repo.create(
        db_session,
        user_id=user.id,
        key_hash=_digest("one"),
        key_prefix="gw_live_",
        last_4="a91c",
        nickname="CI runner",
    )

    assert api_key.id is not None
    assert api_key.user_id == user.id
    assert api_key.nickname == "CI runner"
    assert api_key.is_active is True
    assert api_key.last_used_at is None
    assert api_key.created_at is not None


async def test_a_key_needs_no_nickname(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()

    api_key = await repo.create(
        db_session, user_id=user.id, key_hash=_digest("two"), key_prefix="gw_live_", last_4="0000"
    )

    assert api_key.nickname is None


# --------------------------------------------------------------------------- #
# list_for_user
# --------------------------------------------------------------------------- #
async def test_the_listing_is_newest_first_and_keeps_revoked_keys(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """Half the questions this list gets opened to answer are answered by seeing
    the key that was revoked last week still sitting there."""
    user = await user_factory()
    older = await repo.create(
        db_session, user_id=user.id, key_hash=_digest("older"), key_prefix="gw_live_", last_4="1111"
    )
    newer = await repo.create(
        db_session, user_id=user.id, key_hash=_digest("newer"), key_prefix="gw_live_", last_4="2222"
    )
    await repo.revoke(db_session, key_id=older.id, user_id=user.id)

    rows = await repo.list_for_user(db_session, user.id)

    assert {row.id for row in rows} == {older.id, newer.id}
    assert [row.created_at for row in rows] == sorted(
        (row.created_at for row in rows), reverse=True
    )


async def test_the_listing_shows_nobody_elses_keys(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    mine = await user_factory()
    theirs = await user_factory()
    await repo.create(
        db_session, user_id=mine.id, key_hash=_digest("mine"), key_prefix="gw_live_", last_4="3333"
    )
    await repo.create(
        db_session,
        user_id=theirs.id,
        key_hash=_digest("theirs"),
        key_prefix="gw_live_",
        last_4="4444",
    )

    rows = await repo.list_for_user(db_session, mine.id)

    assert [row.last_4 for row in rows] == ["3333"]


async def test_a_user_with_no_keys_lists_empty(db_session: AsyncSession) -> None:
    assert await repo.list_for_user(db_session, uuid4()) == []


# --------------------------------------------------------------------------- #
# get_active_with_user
# --------------------------------------------------------------------------- #
async def test_a_live_key_resolves_to_its_row_and_its_owner(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """One join, not two round-trips: this sits in front of every endpoint, and it
    needs ``api_key_id`` for attribution and ``tier`` for quota on every call."""
    user = await user_factory(tier="plus")
    digest = _digest("live")
    created = await repo.create(
        db_session, user_id=user.id, key_hash=digest, key_prefix="gw_live_", last_4="5555"
    )

    found = await repo.get_active_with_user(db_session, digest)

    assert found is not None
    api_key, owner = found
    assert api_key.id == created.id
    assert owner.id == user.id
    assert owner.tier == "plus"


async def test_a_revoked_key_and_an_unknown_one_look_identical(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """Both ``None``. Anything else lets an attacker enumerate which digests were
    ever real."""
    user = await user_factory()
    digest = _digest("revoked")
    created = await repo.create(
        db_session, user_id=user.id, key_hash=digest, key_prefix="gw_live_", last_4="6666"
    )
    await repo.revoke(db_session, key_id=created.id, user_id=user.id)

    assert await repo.get_active_with_user(db_session, digest) is None
    assert await repo.get_active_with_user(db_session, _digest("never-existed")) is None


# --------------------------------------------------------------------------- #
# revoke
# --------------------------------------------------------------------------- #
async def test_revoking_deactivates_rather_than_deletes(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """``requests.api_key_id`` points here. A hard delete would take the usage
    history with it every time somebody rotates a key."""
    user = await user_factory()
    created = await repo.create(
        db_session, user_id=user.id, key_hash=_digest("soft"), key_prefix="gw_live_", last_4="7777"
    )

    assert await repo.revoke(db_session, key_id=created.id, user_id=user.id) is True

    rows = await repo.list_for_user(db_session, user.id)
    assert len(rows) == 1
    assert rows[0].is_active is False


async def test_revoking_twice_still_succeeds(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """The caller's intent — "this key must not work" — is satisfied either way,
    and a retried request should not read as a failure."""
    user = await user_factory()
    created = await repo.create(
        db_session, user_id=user.id, key_hash=_digest("twice"), key_prefix="gw_live_", last_4="8888"
    )
    await repo.revoke(db_session, key_id=created.id, user_id=user.id)

    assert await repo.revoke(db_session, key_id=created.id, user_id=user.id) is True


async def test_revoking_someone_elses_key_changes_nothing(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """``False``, and — the part worth asserting — the key still works."""
    owner = await user_factory()
    stranger = await user_factory()
    digest = _digest("not-yours")
    created = await repo.create(
        db_session, user_id=owner.id, key_hash=digest, key_prefix="gw_live_", last_4="9999"
    )

    assert await repo.revoke(db_session, key_id=created.id, user_id=stranger.id) is False
    assert await repo.get_active_with_user(db_session, digest) is not None


async def test_revoking_an_unknown_key_is_false(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()

    assert await repo.revoke(db_session, key_id=uuid4(), user_id=user.id) is False


# --------------------------------------------------------------------------- #
# touch_last_used — the throttle
# --------------------------------------------------------------------------- #
async def test_a_first_use_is_recorded(
    db_session: AsyncSession, user_factory: Callable[..., Any], frozen_clock: FixedClock
) -> None:
    user = await user_factory()
    created = await repo.create(
        db_session, user_id=user.id, key_hash=_digest("touch"), key_prefix="gw_live_", last_4="aaaa"
    )
    assert created.last_used_at is None

    await repo.touch_last_used(db_session, key_id=created.id, clock=frozen_clock)
    await db_session.refresh(created)

    assert created.last_used_at == frozen_clock.now()


async def test_a_second_use_inside_the_window_does_not_pay_for_a_write(
    db_session: AsyncSession, user_factory: Callable[..., Any], frozen_clock: FixedClock
) -> None:
    """The throttle. Without it every read-only request becomes a write and takes
    a row lock on the hot path of every integration."""
    user = await user_factory()
    created = await repo.create(
        db_session,
        user_id=user.id,
        key_hash=_digest("throttled"),
        key_prefix="gw_live_",
        last_4="bbbb",
    )
    await repo.touch_last_used(db_session, key_id=created.id, clock=frozen_clock)
    await db_session.refresh(created)
    first = created.last_used_at

    frozen_clock.advance(repo.LAST_USED_THROTTLE_S - 1)
    await repo.touch_last_used(db_session, key_id=created.id, clock=frozen_clock)
    await db_session.refresh(created)

    assert created.last_used_at == first


async def test_a_use_past_the_window_moves_the_timestamp(
    db_session: AsyncSession, user_factory: Callable[..., Any], frozen_clock: FixedClock
) -> None:
    """Minute-granularity is enough for what the column is for: "is this key still
    in use, or can it be revoked?\""""
    user = await user_factory()
    created = await repo.create(
        db_session, user_id=user.id, key_hash=_digest("stale"), key_prefix="gw_live_", last_4="cccc"
    )
    await repo.touch_last_used(db_session, key_id=created.id, clock=frozen_clock)
    await db_session.refresh(created)
    first = created.last_used_at

    later = frozen_clock.advance(repo.LAST_USED_THROTTLE_S + 1)
    await repo.touch_last_used(db_session, key_id=created.id, clock=frozen_clock)
    await db_session.refresh(created)

    assert created.last_used_at == later
    assert first is not None and created.last_used_at > first


async def test_touching_an_unknown_key_is_a_no_op(
    db_session: AsyncSession, frozen_clock: FixedClock
) -> None:
    """A key revoked between authentication and this call is not an error worth
    failing an otherwise-successful request over."""
    await repo.touch_last_used(db_session, key_id=uuid4(), clock=frozen_clock)
