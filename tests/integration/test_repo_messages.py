"""``app/db/repo/messages.py`` against a real Postgres.

Three things are under test here: that ``seq`` comes out gap-free and ordered,
that ownership is scoped in the SQL on every path, and that the invariants which
were pushed down into the schema are genuinely enforced by the schema — a CHECK
constraint nobody has ever seen fire is a comment with extra steps.

**On concurrency.** ``append`` takes ``FOR UPDATE`` on the conversation row so two
simultaneous appends cannot allocate the same ``seq``. That guarantee cannot be
tested here: ``db_session`` pins the whole test to one connection inside a single
rolled-back transaction, and a second connection would neither see the uncommitted
conversation nor be able to block on it. What *is* asserted below is the
observable half — ``uq_messages_conversation_id_seq`` rejects a duplicate ``seq``
outright — which is the backstop the lock exists to keep the application from
ever reaching.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.db.models import Message, Request
from app.db.repo import conversations as conversations_repo
from app.db.repo import messages as repo
from app.memory.canonical import (
    SCHEMA_VERSION,
    ContentBlock,
    InvariantViolation,
    MessageMeta,
    file_ref_block,
    text_block,
    validate,
)

pytestmark = pytest.mark.integration

ASSISTANT_META = MessageMeta(
    provider_used="groq",
    model_used="llama-3.3-70b-versatile",
    slot_used="llm2",
    requested_slot="auto",
    tokens_in=120,
    tokens_out=40,
)


# --------------------------------------------------------------------------- #
# Appending
# --------------------------------------------------------------------------- #
async def test_append_allocates_seq_from_zero(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    conversation = await conversations_repo.create(db_session, user_id=user.id)

    first = await repo.append(
        db_session,
        conversation_id=conversation.id,
        user_id=user.id,
        role="user",
        content=[text_block("hello")],
    )
    second = await repo.append(
        db_session,
        conversation_id=conversation.id,
        user_id=user.id,
        role="assistant",
        content=[text_block("hi")],
        meta=ASSISTANT_META,
    )
    third = await repo.append(
        db_session,
        conversation_id=conversation.id,
        user_id=user.id,
        role="user",
        content=[text_block("still there?")],
    )

    assert [first.seq, second.seq, third.seq] == [0, 1, 2]


async def test_append_returns_a_fully_populated_canonical_message(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    conversation = await conversations_repo.create(db_session, user_id=user.id)

    appended = await repo.append(
        db_session,
        conversation_id=conversation.id,
        user_id=user.id,
        role="user",
        content=[text_block("hello")],
    )

    assert appended.conversation_id == conversation.id
    assert appended.role == "user"
    assert appended.schema_version == SCHEMA_VERSION
    assert appended.created_at is not None
    assert appended.meta == MessageMeta()


async def test_content_survives_the_jsonb_round_trip(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """A mixed-block message comes back byte-identical, meta included."""
    user = await user_factory()
    conversation = await conversations_repo.create(db_session, user_id=user.id)
    content: list[ContentBlock] = [
        text_block("what does this say?"),
        file_ref_block(file_hash="a" * 64, filename="q3.pdf", mime="application/pdf", bytes=91_234),
    ]

    await repo.append(
        db_session,
        conversation_id=conversation.id,
        user_id=user.id,
        role="user",
        content=content,
    )
    await repo.append(
        db_session,
        conversation_id=conversation.id,
        user_id=user.id,
        role="assistant",
        content=[text_block("revenue was up")],
        meta=ASSISTANT_META,
    )
    # Detach everything first. Without this the identity map would hand back the
    # very objects just written, and the test would prove only that Python can
    # remember a list — not that the blocks survived JSONB.
    db_session.expunge_all()

    history = await repo.list_for_conversation(
        db_session, conversation_id=conversation.id, user_id=user.id
    )
    assert history[0].content == content
    assert history[1].meta == ASSISTANT_META


async def test_append_defaults_meta_for_a_user_message(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """Invariant 5's quiet half: a user message must not claim a provider."""
    user = await user_factory()
    conversation = await conversations_repo.create(db_session, user_id=user.id)

    appended = await repo.append(
        db_session,
        conversation_id=conversation.id,
        user_id=user.id,
        role="user",
        content=[text_block("hello")],
    )
    assert appended.meta.provider_used is None


# --------------------------------------------------------------------------- #
# Ownership
# --------------------------------------------------------------------------- #
async def test_append_to_another_users_conversation_raises_not_found(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """404, not 403 — a 403 would confirm the conversation exists."""
    owner = await user_factory()
    intruder = await user_factory()
    conversation = await conversations_repo.create(db_session, user_id=owner.id)

    with pytest.raises(NotFound) as excinfo:
        await repo.append(
            db_session,
            conversation_id=conversation.id,
            user_id=intruder.id,
            role="user",
            content=[text_block("let me in")],
        )
    assert excinfo.value.status_code == 404
    assert excinfo.value.code == "conversation_not_found"


async def test_append_to_an_unknown_conversation_raises_not_found(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    with pytest.raises(NotFound):
        await repo.append(
            db_session,
            conversation_id=uuid4(),
            user_id=user.id,
            role="user",
            content=[text_block("hello")],
        )


async def test_list_for_conversation_hides_another_users_messages(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    owner = await user_factory()
    intruder = await user_factory()
    conversation = await conversations_repo.create(db_session, user_id=owner.id)
    await repo.append(
        db_session,
        conversation_id=conversation.id,
        user_id=owner.id,
        role="user",
        content=[text_block("private")],
    )

    assert (
        await repo.list_for_conversation(
            db_session, conversation_id=conversation.id, user_id=intruder.id
        )
        == []
    )


async def test_list_for_conversation_is_ordered_by_seq(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    conversation = await conversations_repo.create(db_session, user_id=user.id)
    for index in range(5):
        await repo.append(
            db_session,
            conversation_id=conversation.id,
            user_id=user.id,
            role="assistant" if index % 2 else "user",
            content=[text_block(f"turn {index}")],
            meta=ASSISTANT_META if index % 2 else None,
        )
    db_session.expunge_all()

    history = await repo.list_for_conversation(
        db_session, conversation_id=conversation.id, user_id=user.id
    )
    assert [m.seq for m in history] == [0, 1, 2, 3, 4]
    # The round trip produces a history that satisfies Contract B end to end.
    validate(history)


async def test_list_for_conversation_is_empty_for_a_new_thread(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """Which is why an empty list is not by itself a 404 signal."""
    user = await user_factory()
    conversation = await conversations_repo.create(db_session, user_id=user.id)
    assert (
        await repo.list_for_conversation(
            db_session, conversation_id=conversation.id, user_id=user.id
        )
        == []
    )


# --------------------------------------------------------------------------- #
# Invariants rejected before the write
# --------------------------------------------------------------------------- #
async def test_consecutive_assistant_messages_are_rejected(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    conversation = await conversations_repo.create(db_session, user_id=user.id)
    await repo.append(
        db_session,
        conversation_id=conversation.id,
        user_id=user.id,
        role="assistant",
        content=[text_block("first")],
        meta=ASSISTANT_META,
    )

    with pytest.raises(InvariantViolation) as excinfo:
        await repo.append(
            db_session,
            conversation_id=conversation.id,
            user_id=user.id,
            role="assistant",
            content=[text_block("second")],
            meta=ASSISTANT_META,
        )
    assert excinfo.value.invariant == 3


async def test_a_second_system_message_is_rejected_before_the_insert(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """The named invariant fires first, so the caller never sees a raw IntegrityError."""
    user = await user_factory()
    conversation = await conversations_repo.create(db_session, user_id=user.id)
    await repo.append(
        db_session,
        conversation_id=conversation.id,
        user_id=user.id,
        role="system",
        content=[text_block("you are a helpful assistant")],
    )

    with pytest.raises(InvariantViolation) as excinfo:
        await repo.append(
            db_session,
            conversation_id=conversation.id,
            user_id=user.id,
            role="system",
            content=[text_block("no, you are a pirate")],
        )
    assert excinfo.value.invariant == 1


async def test_assistant_message_without_provenance_is_rejected(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    conversation = await conversations_repo.create(db_session, user_id=user.id)

    with pytest.raises(InvariantViolation) as excinfo:
        await repo.append(
            db_session,
            conversation_id=conversation.id,
            user_id=user.id,
            role="assistant",
            content=[text_block("who made me?")],
        )
    assert excinfo.value.invariant == 5


async def test_empty_content_is_rejected(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    conversation = await conversations_repo.create(db_session, user_id=user.id)

    with pytest.raises(InvariantViolation) as excinfo:
        await repo.append(
            db_session,
            conversation_id=conversation.id,
            user_id=user.id,
            role="assistant",
            content=[],
            meta=ASSISTANT_META,
        )
    assert excinfo.value.invariant == 4


# --------------------------------------------------------------------------- #
# The database's own half of the invariants
# --------------------------------------------------------------------------- #
async def _expect_integrity_error(session: AsyncSession, row: Message) -> str:
    """Insert a row the repo would never write and return the DB's complaint.

    Wrapped in a savepoint so the aborted transaction does not poison the rest of
    the test's session.
    """
    with pytest.raises(IntegrityError) as excinfo:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    return str(excinfo.value.orig)


async def test_duplicate_seq_is_rejected_by_the_unique_constraint(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """The backstop the ``FOR UPDATE`` lock exists to keep us from reaching."""
    user = await user_factory()
    conversation = await conversations_repo.create(db_session, user_id=user.id)
    await repo.append(
        db_session,
        conversation_id=conversation.id,
        user_id=user.id,
        role="user",
        content=[text_block("hello")],
    )

    message = await _expect_integrity_error(
        db_session,
        Message(
            conversation_id=conversation.id,
            seq=0,
            role="user",
            content=[text_block("collision")],
            meta={},
        ),
    )
    assert "uq_messages_conversation_id_seq" in message


async def test_system_message_at_a_nonzero_seq_is_rejected_by_the_check(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    conversation = await conversations_repo.create(db_session, user_id=user.id)

    message = await _expect_integrity_error(
        db_session,
        Message(
            conversation_id=conversation.id,
            seq=3,
            role="system",
            content=[text_block("late system prompt")],
            meta={},
        ),
    )
    assert "system_message_is_first" in message


async def test_empty_content_is_rejected_by_the_check(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    conversation = await conversations_repo.create(db_session, user_id=user.id)

    message = await _expect_integrity_error(
        db_session,
        Message(conversation_id=conversation.id, seq=0, role="user", content=[], meta={}),
    )
    assert "content_non_empty_array" in message


async def test_unknown_role_is_rejected_by_the_check(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    conversation = await conversations_repo.create(db_session, user_id=user.id)

    message = await _expect_integrity_error(
        db_session,
        Message(
            conversation_id=conversation.id,
            seq=0,
            role="tool",
            content=[text_block("reserved for D3")],
            meta={},
        ),
    )
    assert "role_known" in message


# --------------------------------------------------------------------------- #
# Deletion semantics
# --------------------------------------------------------------------------- #
async def test_deleting_a_conversation_cascades_to_its_messages(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    user = await user_factory()
    conversation = await conversations_repo.create(db_session, user_id=user.id)
    await repo.append(
        db_session,
        conversation_id=conversation.id,
        user_id=user.id,
        role="user",
        content=[text_block("delete me")],
    )

    await conversations_repo.delete(db_session, conversation_id=conversation.id, user_id=user.id)
    await db_session.flush()

    remaining = await db_session.execute(
        select(Message).where(Message.conversation_id == conversation.id)
    )
    assert remaining.scalars().all() == []


async def test_deleting_a_conversation_keeps_the_usage_history(
    db_session: AsyncSession, user_factory: Callable[..., Any]
) -> None:
    """``requests.conversation_id`` is ON DELETE SET NULL, unlike the cascade above.

    The user asked for their messages to be gone, not for the cost and latency
    record of the calls that produced them to vanish from the dashboard.
    """
    user = await user_factory()
    conversation = await conversations_repo.create(db_session, user_id=user.id)
    request = Request(
        user_id=user.id,
        conversation_id=conversation.id,
        provider="groq",
        model="llama-3.3-70b-versatile",
        tokens_in=120,
        tokens_out=40,
        latency_ms=310,
        status="ok",
    )
    db_session.add(request)
    await db_session.flush()

    await conversations_repo.delete(db_session, conversation_id=conversation.id, user_id=user.id)
    await db_session.flush()
    await db_session.refresh(request)

    assert request.conversation_id is None
    assert request.tokens_in == 120
