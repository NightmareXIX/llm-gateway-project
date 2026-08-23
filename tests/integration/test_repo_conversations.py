"""``app/db/repo/conversations.py`` against a real Postgres.

The ownership tests are the ones that matter most here. "Every read is
ownership-scoped in the query itself" is a rule that holds until somebody adds a
convenience function that forgets, so every entry point in the module is
exercised against a second user, and the assertion is always the same: the
non-owner learns nothing and changes nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repo import conversations as repo

pytestmark = pytest.mark.integration


async def test_create_returns_a_populated_row(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    conversation = await repo.create(db_session, user_id=user.id, title="Quarterly numbers")

    assert conversation.user_id == user.id
    assert conversation.title == "Quarterly numbers"
    assert conversation.preferred_slot == "auto"
    # Server defaults come back from the flush, not from a later refresh.
    assert conversation.created_at is not None
    assert conversation.updated_at is not None


async def test_create_defaults_to_an_untitled_thread(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """No placeholder title: later it would be indistinguishable from a real one."""
    user = await user_factory()
    conversation = await repo.create(db_session, user_id=user.id)
    assert conversation.title is None


async def test_get_owned_returns_the_row_for_its_owner(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    created = await repo.create(db_session, user_id=user.id)

    found = await repo.get_owned(db_session, conversation_id=created.id, user_id=user.id)
    assert found is not None
    assert found.id == created.id


async def test_get_owned_hides_another_users_conversation(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """The 404-not-403 rule starts here: the repo cannot tell the difference."""
    owner = await user_factory()
    intruder = await user_factory()
    created = await repo.create(db_session, user_id=owner.id)

    assert await repo.get_owned(db_session, conversation_id=created.id, user_id=intruder.id) is None


async def test_get_owned_returns_none_for_an_unknown_id(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    assert await repo.get_owned(db_session, conversation_id=uuid4(), user_id=user.id) is None


async def test_get_owned_is_scoped_even_when_the_row_is_already_in_the_session(
    db_session: AsyncSession,
    user_factory: Callable[..., Any],
    conversation_factory: Callable[..., Any],
) -> None:
    """Why this is a ``select``, not ``session.get``.

    ``session.get`` is served from the identity map when the row is already
    loaded, so the ownership predicate would never reach the database — the
    intruder would get the object straight out of memory.
    """
    owner = await user_factory()
    intruder = await user_factory()
    conversation = await conversation_factory(user=owner)
    assert conversation in db_session  # loaded and tracked by this session

    assert (
        await repo.get_owned(db_session, conversation_id=conversation.id, user_id=intruder.id)
        is None
    )


async def test_list_for_user_orders_by_recent_activity(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """``updated_at``, not ``created_at`` — a thread replied to today belongs on top."""
    user = await user_factory()
    first = await repo.create(db_session, user_id=user.id, title="oldest")
    second = await repo.create(db_session, user_id=user.id, title="newest")

    await repo.touch(db_session, conversation_id=first.id, user_id=user.id)
    await db_session.flush()
    # Detach, so the assertion reads rows the database returned rather than the
    # instances already sitting in this session's identity map.
    db_session.expunge_all()

    listed = await repo.list_for_user(db_session, user_id=user.id)
    assert [c.id for c in listed] == [first.id, second.id]


async def test_list_for_user_excludes_other_users(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    owner = await user_factory()
    other = await user_factory()
    mine = await repo.create(db_session, user_id=owner.id)
    await repo.create(db_session, user_id=other.id)

    listed = await repo.list_for_user(db_session, user_id=owner.id)
    assert [c.id for c in listed] == [mine.id]


async def test_list_for_user_paginates(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    for _ in range(3):
        await repo.create(db_session, user_id=user.id)

    page = await repo.list_for_user(db_session, user_id=user.id, limit=2)
    assert len(page) == 2
    assert len(await repo.list_for_user(db_session, user_id=user.id, limit=2, offset=2)) == 1


async def test_list_for_user_caps_an_absurd_limit(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    await repo.create(db_session, user_id=user.id)
    # The cap is about the SQL that gets emitted, not the row count here.
    assert len(await repo.list_for_user(db_session, user_id=user.id, limit=10_000)) == 1


async def test_touch_moves_updated_at(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    conversation = await repo.create(db_session, user_id=user.id)
    before = conversation.updated_at

    assert await repo.touch(db_session, conversation_id=conversation.id, user_id=user.id) is True
    await db_session.refresh(conversation)
    assert conversation.updated_at >= before


async def test_touch_rejects_another_users_conversation(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    owner = await user_factory()
    intruder = await user_factory()
    conversation = await repo.create(db_session, user_id=owner.id)

    assert (
        await repo.touch(db_session, conversation_id=conversation.id, user_id=intruder.id) is False
    )


async def test_touch_updates_preferred_slot_when_given_one(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """D33: the activity bump and the preference update are one UPDATE."""
    user = await user_factory()
    conversation = await repo.create(db_session, user_id=user.id, preferred_slot="auto")

    assert await repo.touch(
        db_session, conversation_id=conversation.id, user_id=user.id, preferred_slot="fast"
    )
    await db_session.refresh(conversation)
    assert conversation.preferred_slot == "fast"


async def test_touch_leaves_preferred_slot_alone_when_none_is_given(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """The Phase 1 caller — and any future caller that only wants the activity
    bump — has no opinion on the slot, and the column must not move under it."""
    user = await user_factory()
    conversation = await repo.create(db_session, user_id=user.id, preferred_slot="fast")

    assert await repo.touch(db_session, conversation_id=conversation.id, user_id=user.id)
    await db_session.refresh(conversation)
    assert conversation.preferred_slot == "fast"


async def test_touch_rejects_another_users_conversation_and_writes_nothing(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    owner = await user_factory()
    intruder = await user_factory()
    conversation = await repo.create(db_session, user_id=owner.id, preferred_slot="auto")

    assert (
        await repo.touch(
            db_session,
            conversation_id=conversation.id,
            user_id=intruder.id,
            preferred_slot="fast",
        )
        is False
    )
    await db_session.refresh(conversation)
    assert conversation.preferred_slot == "auto"


async def test_rename_sets_and_clears_the_title(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    conversation = await repo.create(db_session, user_id=user.id, title="draft")

    assert await repo.rename(
        db_session, conversation_id=conversation.id, user_id=user.id, title="Q3 review"
    )
    await db_session.refresh(conversation)
    assert conversation.title == "Q3 review"

    assert await repo.rename(
        db_session, conversation_id=conversation.id, user_id=user.id, title=None
    )
    await db_session.refresh(conversation)
    assert conversation.title is None


async def test_rename_rejects_another_users_conversation(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    owner = await user_factory()
    intruder = await user_factory()
    conversation = await repo.create(db_session, user_id=owner.id, title="mine")

    assert not await repo.rename(
        db_session, conversation_id=conversation.id, user_id=intruder.id, title="not yours"
    )
    await db_session.refresh(conversation)
    assert conversation.title == "mine"


async def test_delete_removes_the_conversation(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    conversation = await repo.create(db_session, user_id=user.id)

    assert await repo.delete(db_session, conversation_id=conversation.id, user_id=user.id) is True
    assert (
        await repo.get_owned(db_session, conversation_id=conversation.id, user_id=user.id) is None
    )


async def test_delete_rejects_another_users_conversation(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    owner = await user_factory()
    intruder = await user_factory()
    conversation = await repo.create(db_session, user_id=owner.id)

    assert (
        await repo.delete(db_session, conversation_id=conversation.id, user_id=intruder.id) is False
    )
    assert await repo.get_owned(db_session, conversation_id=conversation.id, user_id=owner.id)


async def test_delete_is_not_idempotent_about_its_return_value(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """Unlike ``api_keys.revoke``: a second delete found nothing to delete.

    The endpoint turns that into a 404, which is the honest answer for a
    conversation that is already gone.
    """
    user = await user_factory()
    conversation = await repo.create(db_session, user_id=user.id)

    assert await repo.delete(db_session, conversation_id=conversation.id, user_id=user.id) is True
    assert await repo.delete(db_session, conversation_id=conversation.id, user_id=user.id) is False
