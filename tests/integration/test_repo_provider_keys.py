"""``app/db/repo/provider_keys.py`` against a real Postgres.

Three things here are only observable from the database side.

**The partial unique index is §9.5's granularity, enforced.** One live key per
``(user, provider)`` — a second active row for the same pair must be refused
by the schema, not merely by application discipline, and a soft-deleted row
must never count against that limit.

**``upsert`` replaces, it never accumulates.** Adding a second key for a
provider the user already has one for deactivates the first inside the same
transaction — the partial index makes that mandatory, not a style choice.

**Ownership is scoped in the SQL, always** — the same rule ``conversations``
and ``files`` already follow, extended here to a table that also gates who
gets billed for a request.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FixedClock
from app.db.models import ProviderKey
from app.db.repo import provider_keys as repo

pytestmark = pytest.mark.integration


async def _add(
    session: AsyncSession, *, user: Any, provider: str = "gemini", **overrides: Any
) -> ProviderKey:
    fields: dict[str, Any] = {
        "user_id": user.id,
        "provider": provider,
        "encrypted_key": "gAAAA-not-real-ciphertext",
        "last_4": "a91c",
        "nickname": None,
        "validation_status": "valid",
        "last_validated_at": None,
    }
    fields.update(overrides)
    return await repo.upsert(session, **fields)


# --------------------------------------------------------------------------- #
# upsert
# --------------------------------------------------------------------------- #
async def test_upsert_stores_a_row_with_the_given_fields(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()

    key = await _add(db_session, user=user, nickname="my key")

    assert key.owner_type == "user"
    assert key.owner_id == user.id
    assert key.provider == "gemini"
    assert key.last_4 == "a91c"
    assert key.nickname == "my key"
    assert key.validation_status == "valid"
    assert key.is_active is True


async def test_upsert_replaces_an_existing_active_key_for_the_same_provider(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """The partial unique index makes this mandatory: two active rows for the
    same ``(owner_id, provider)`` cannot coexist."""
    user = await user_factory()
    first = await _add(db_session, user=user, last_4="1111")

    second = await _add(db_session, user=user, last_4="2222")
    await db_session.flush()
    await db_session.refresh(first)

    assert first.is_active is False
    assert second.is_active is True
    assert second.last_4 == "2222"

    active = await repo.get_active(db_session, user_id=user.id, provider="gemini")
    assert active is not None
    assert active.id == second.id


async def test_upsert_does_not_disturb_a_different_providers_key(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    gemini_key = await _add(db_session, user=user, provider="gemini")

    await _add(db_session, user=user, provider="groq")
    await db_session.flush()
    await db_session.refresh(gemini_key)

    assert gemini_key.is_active is True


async def test_upsert_does_not_disturb_another_users_key_for_the_same_provider(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    owner = await user_factory()
    other = await user_factory()
    owners_key = await _add(db_session, user=owner)

    await _add(db_session, user=other)
    await db_session.flush()
    await db_session.refresh(owners_key)

    assert owners_key.is_active is True


# --------------------------------------------------------------------------- #
# the partial unique index, hit directly
# --------------------------------------------------------------------------- #
async def test_two_active_rows_for_the_same_owner_and_provider_are_refused(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """The schema's own backstop — a bug in ``upsert`` that skipped the
    deactivate step would hit this, not silently store two live keys."""
    user = await user_factory()
    await _add(db_session, user=user)

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                ProviderKey(
                    owner_type="user",
                    owner_id=user.id,
                    provider="gemini",
                    encrypted_key="gAAAA-second",
                    last_4="9999",
                    validation_status="valid",
                    is_active=True,
                )
            )
            await db_session.flush()


async def test_a_revoked_row_does_not_block_re_adding(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """Partial: the index only covers ``is_active`` rows, so a soft-deleted one
    is invisible to it."""
    user = await user_factory()
    original = await _add(db_session, user=user)
    await repo.deactivate(db_session, user_id=user.id, provider="gemini")
    await db_session.flush()

    async with db_session.begin_nested():
        db_session.add(
            ProviderKey(
                owner_type="user",
                owner_id=user.id,
                provider="gemini",
                encrypted_key="gAAAA-again",
                last_4="0000",
                validation_status="valid",
                is_active=True,
            )
        )
        await db_session.flush()

    await db_session.refresh(original)
    assert original.is_active is False


# --------------------------------------------------------------------------- #
# the owner_type / owner_id CHECK
# --------------------------------------------------------------------------- #
async def test_a_user_owned_row_needs_an_owner_id(db_session: AsyncSession) -> None:
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                ProviderKey(
                    owner_type="user",
                    owner_id=None,
                    provider="gemini",
                    encrypted_key="gAAAA-orphan",
                    last_4="0000",
                    validation_status="unverified",
                )
            )
            await db_session.flush()


async def test_a_system_owned_row_must_have_no_owner_id(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                ProviderKey(
                    owner_type="system",
                    owner_id=user.id,
                    provider="gemini",
                    encrypted_key="gAAAA-mislabeled",
                    last_4="0000",
                    validation_status="unverified",
                )
            )
            await db_session.flush()


async def test_an_unknown_validation_status_is_refused(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                ProviderKey(
                    owner_type="user",
                    owner_id=user.id,
                    provider="gemini",
                    encrypted_key="gAAAA-badstatus",
                    last_4="0000",
                    validation_status="pending",
                )
            )
            await db_session.flush()


# --------------------------------------------------------------------------- #
# list_for_user / list_active_for_user / get_active — ownership scoping
# --------------------------------------------------------------------------- #
async def test_list_for_user_keeps_a_deactivated_row_and_hides_other_users(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    owner = await user_factory()
    other = await user_factory()
    await _add(db_session, user=owner, provider="gemini")
    await repo.deactivate(db_session, user_id=owner.id, provider="gemini")
    await _add(db_session, user=other, provider="gemini")

    rows = await repo.list_for_user(db_session, owner.id)

    assert len(rows) == 1
    assert rows[0].is_active is False


async def test_list_for_user_orders_by_provider(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    await _add(db_session, user=user, provider="openrouter")
    await _add(db_session, user=user, provider="gemini")
    await _add(db_session, user=user, provider="groq")

    rows = await repo.list_for_user(db_session, user.id)

    assert [row.provider for row in rows] == ["gemini", "groq", "openrouter"]


async def test_list_active_for_user_excludes_a_deactivated_row(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """The resolver's one query (D38) — only rows it would actually use."""
    user = await user_factory()
    await _add(db_session, user=user, provider="gemini")
    await repo.deactivate(db_session, user_id=user.id, provider="gemini")
    await _add(db_session, user=user, provider="groq")

    rows = await repo.list_active_for_user(db_session, user.id)

    assert [row.provider for row in rows] == ["groq"]


async def test_list_active_for_user_excludes_another_users_rows(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    mine = await user_factory()
    theirs = await user_factory()
    await _add(db_session, user=mine, provider="gemini")
    await _add(db_session, user=theirs, provider="groq")

    rows = await repo.list_active_for_user(db_session, mine.id)

    assert [row.provider for row in rows] == ["gemini"]


async def test_get_active_returns_none_for_a_provider_never_added(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    assert await repo.get_active(db_session, user_id=user.id, provider="gemini") is None


async def test_get_active_hides_another_users_key(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    owner = await user_factory()
    intruder = await user_factory()
    await _add(db_session, user=owner)

    assert await repo.get_active(db_session, user_id=intruder.id, provider="gemini") is None


# --------------------------------------------------------------------------- #
# deactivate
# --------------------------------------------------------------------------- #
async def test_deactivate_soft_deletes_rather_than_removes_the_row(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    await _add(db_session, user=user)

    assert await repo.deactivate(db_session, user_id=user.id, provider="gemini") is True

    rows = await repo.list_for_user(db_session, user.id)
    assert len(rows) == 1
    assert rows[0].is_active is False


async def test_deactivating_twice_still_succeeds(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """Same idempotence ``api_keys.revoke`` has, for the same reason: "this
    provider must not use my key" is satisfied whether or not it already was."""
    user = await user_factory()
    await _add(db_session, user=user)
    await repo.deactivate(db_session, user_id=user.id, provider="gemini")

    assert await repo.deactivate(db_session, user_id=user.id, provider="gemini") is True


async def test_deactivating_a_provider_never_added_is_false(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    assert await repo.deactivate(db_session, user_id=user.id, provider="gemini") is False


async def test_deactivating_someone_elses_key_changes_nothing(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    owner = await user_factory()
    intruder = await user_factory()
    await _add(db_session, user=owner)

    assert await repo.deactivate(db_session, user_id=intruder.id, provider="gemini") is False
    assert await repo.get_active(db_session, user_id=owner.id, provider="gemini") is not None


# --------------------------------------------------------------------------- #
# touch_last_used — the throttle
# --------------------------------------------------------------------------- #
async def test_a_first_use_is_recorded(
    db_session: AsyncSession, user_factory: Callable[..., Any], frozen_clock: FixedClock
) -> None:
    user = await user_factory()
    key = await _add(db_session, user=user)
    assert key.last_used_at is None

    await repo.touch_last_used(db_session, key_id=key.id, clock=frozen_clock)
    await db_session.refresh(key)

    assert key.last_used_at == frozen_clock.now()


async def test_a_second_use_inside_the_window_does_not_pay_for_a_write(
    db_session: AsyncSession, user_factory: Callable[..., Any], frozen_clock: FixedClock
) -> None:
    user = await user_factory()
    key = await _add(db_session, user=user)
    await repo.touch_last_used(db_session, key_id=key.id, clock=frozen_clock)
    await db_session.refresh(key)
    first = key.last_used_at

    frozen_clock.advance(repo.LAST_USED_THROTTLE_S - 1)
    await repo.touch_last_used(db_session, key_id=key.id, clock=frozen_clock)
    await db_session.refresh(key)

    assert key.last_used_at == first


async def test_touching_an_unknown_key_is_a_no_op(
    db_session: AsyncSession, frozen_clock: FixedClock
) -> None:
    await repo.touch_last_used(db_session, key_id=uuid4(), clock=frozen_clock)


# --------------------------------------------------------------------------- #
# mark_invalid — D40's disclosure write
# --------------------------------------------------------------------------- #
async def test_mark_invalid_flips_the_validation_status(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    key = await _add(db_session, user=user, validation_status="valid")

    await repo.mark_invalid(db_session, key_id=key.id)
    await db_session.refresh(key)

    assert key.validation_status == "invalid"


async def test_mark_invalid_does_not_touch_last_validated_at(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """D40: this fires from a private key's ``AuthFailed`` mid-request, not from
    the user re-checking it — the two are different events on the settings UI."""
    validated_at = datetime(2026, 8, 20, tzinfo=UTC)
    user = await user_factory()
    key = await _add(
        db_session, user=user, validation_status="valid", last_validated_at=validated_at
    )

    await repo.mark_invalid(db_session, key_id=key.id)
    await db_session.refresh(key)

    assert key.last_validated_at == validated_at


# --------------------------------------------------------------------------- #
# record_validation_result — the "check again" button's write
# --------------------------------------------------------------------------- #
async def test_record_validation_result_records_a_success(
    db_session: AsyncSession, user_factory: Callable[..., Any], frozen_clock: FixedClock
) -> None:
    user = await user_factory()
    key = await _add(db_session, user=user, validation_status="unverified", last_validated_at=None)

    await repo.record_validation_result(
        db_session, key_id=key.id, valid=True, validated_at=frozen_clock.now()
    )
    await db_session.refresh(key)

    assert key.validation_status == "valid"
    assert key.last_validated_at == frozen_clock.now()


async def test_record_validation_result_records_a_failure(
    db_session: AsyncSession, user_factory: Callable[..., Any], frozen_clock: FixedClock
) -> None:
    """Unlike ``mark_invalid``, this one moves ``last_validated_at`` too — the
    user asked "is this still good" and got an answer, even a bad one."""
    user = await user_factory()
    key = await _add(db_session, user=user, validation_status="valid", last_validated_at=None)

    await repo.record_validation_result(
        db_session, key_id=key.id, valid=False, validated_at=frozen_clock.now()
    )
    await db_session.refresh(key)

    assert key.validation_status == "invalid"
    assert key.last_validated_at == frozen_clock.now()
