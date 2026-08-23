"""Reads and writes against ``conversations``.

**Ownership is scoped in the SQL, always.** Every function here takes a
``user_id`` and puts it in the WHERE clause. There is no fetch-then-check variant
and no unscoped ``get`` for a caller to reach for by mistake — the only way to
load a conversation from this module is to say whose it is. A miss is
indistinguishable from a conversation that never existed, which is why the
endpoints answer 404 rather than 403: a 403 confirms the id is real.

Mutations return ``bool`` rather than raising. "Was this yours to change" is a
question the repository can answer and the HTTP layer should decide about; a
repository that raised :class:`NotFound` would be making a status-code choice on
behalf of every future caller.

Repositories take a session and never commit. The caller owns the transaction
boundary, because only the caller knows what one unit of work is.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy import delete as sql_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Conversation

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
"""Ceiling on ``list_for_user``. The sidebar shows a page; a caller asking for
ten thousand rows is a bug or an abuse, and either way the answer is the same."""


async def create(
    session: AsyncSession,
    *,
    user_id: UUID,
    title: str | None = None,
    preferred_slot: str = "auto",
) -> Conversation:
    """Start a new thread.

    ``title`` is nullable on purpose — Phase 1 has no title generation, and the
    UI shows the first user message until something better exists. A placeholder
    string written now would be indistinguishable later from a title the user
    actually chose.
    """
    conversation = Conversation(
        user_id=user_id,
        title=title,
        preferred_slot=preferred_slot,
    )
    session.add(conversation)
    # Assigns the server defaults (created_at, updated_at) before we return.
    # SQLAlchemy 2.0 fetches them via RETURNING on Postgres, so this is one round
    # trip rather than an INSERT followed by a SELECT.
    await session.flush()
    return conversation


async def get_owned(
    session: AsyncSession, *, conversation_id: UUID, user_id: UUID
) -> Conversation | None:
    """Load a conversation, or ``None`` when it is not this user's.

    Deliberately not ``session.get(Conversation, id)`` — that would fetch by
    primary key and leave the ownership test to the caller, which is the exact
    fetch-then-check pattern this codebase forbids. It would also be served from
    the identity map, so a row already loaded in this session would come back
    without the WHERE clause ever running.
    """
    result = await session.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def list_for_user(
    session: AsyncSession,
    *,
    user_id: UUID,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> Sequence[Conversation]:
    """The sidebar query: most recently active first.

    Ordered by ``updated_at DESC`` to match ``ix_conversations_user_id_updated_at``
    exactly, so this stays an index scan as the table grows. ``created_at`` would
    be the wrong key — a thread someone replied to this morning belongs at the
    top regardless of when it started.
    """
    result = await session.execute(
        select(Conversation)
        .where(Conversation.user_id == user_id)
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(max(1, min(limit, MAX_PAGE_SIZE)))
        .offset(max(0, offset))
    )
    return result.scalars().all()


async def touch(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    user_id: UUID,
    preferred_slot: str | None = None,
) -> bool:
    """Mark the conversation as active now. Returns False when it is not theirs.

    ``updated_at`` is set explicitly rather than left to the column's
    ``onupdate=func.now()`` hook. A bare UPDATE with no other changed value is
    the one case where relying on the hook is ambiguous, and an explicit
    assignment makes the statement say what it does.

    ``clock_timestamp()``, not ``now()``. Postgres' ``now()`` is the *transaction*
    timestamp: it returns the moment the transaction began, frozen, however many
    statements later it is called. This function exists to record when a thread
    was last active, and the moment its transaction happened to start is a
    different — and wrong — answer to that question.

    Production timing hides the difference, because a create and a later reply
    arrive as separate requests in separate transactions. Inside a single
    transaction it stops hiding: ``create`` and ``touch`` stamp byte-identical
    times, ``list_for_user``'s ``updated_at DESC`` tie-breaks on a random UUID,
    and the sidebar order becomes a coin flip.

    ``preferred_slot`` (D33) rides the same UPDATE rather than a second round
    trip — the activity bump and the preference update are one unit of work
    every turn already pays for. It is a **display preference**, not a routing
    input: the caller passes whatever slot the request named (even ``"auto"``),
    and this function does not compare it against anything or decide whether it
    "changed" — the WHERE clause already limits the statement to one row, so
    writing the same value back costs nothing beyond the UPDATE that was
    happening anyway. Left ``None``, the column is untouched — the caller from
    Phase 1's create path, and any future caller that only wants the activity
    bump, do not have an opinion on the slot.
    """
    values: dict[str, Any] = {"updated_at": func.clock_timestamp()}
    if preferred_slot is not None:
        values["preferred_slot"] = preferred_slot
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
            .values(**values)
        ),
    )
    return result.rowcount > 0


async def set_pinned(
    session: AsyncSession, *, conversation_id: UUID, user_id: UUID, model: str
) -> bool:
    """D3/D32's write side: lock a conversation to one model. Ownership-scoped
    in the SQL like every sibling. Returns ``False`` when it is not theirs, or
    when it is already pinned to a *different* model.

    ``model`` is ``"{provider}/{model}"`` — :func:`~app.memory.canonical.pin_target`'s
    own return shape, and what ``selection._resolve_pin`` expects back out. A
    slot name here would be wrong: a slot's primary candidate moves with a
    ``providers.yaml`` edit, and a pin that silently followed it is the one
    thing pinning exists to prevent.

    Idempotent when re-pinning to the same model — a retried request, or a
    caller that recomputes the trigger on every turn once a conversation is
    already pinned, must not fail on that account. A re-pin to a *different*
    model is refused by the WHERE clause instead of raising: ``pinned_model IS
    NULL OR pinned_model = :model`` means an UPDATE that would *move* an
    existing pin matches zero rows. A pin exists precisely because that
    conversation's tool-call history cannot safely follow a different model's
    schema — an UPDATE that relocated one anyway would defeat the feature it
    implements, so "no rows matched" is the correct outcome, not a bug to work
    around with a second query.
    """
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
                (Conversation.pinned_model.is_(None)) | (Conversation.pinned_model == model),
            )
            .values(pinned_model=model, updated_at=func.clock_timestamp())
        ),
    )
    return result.rowcount > 0


async def rename(
    session: AsyncSession, *, conversation_id: UUID, user_id: UUID, title: str | None
) -> bool:
    """Set the thread title. ``None`` clears it back to the derived display name.

    ``clock_timestamp()`` for the same reason as :func:`touch` — this is the
    moment of the rename, not the moment its transaction began.
    """
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
            .values(title=title, updated_at=func.clock_timestamp())
        ),
    )
    return result.rowcount > 0


async def delete(session: AsyncSession, *, conversation_id: UUID, user_id: UUID) -> bool:
    """Remove a conversation and its messages. Returns False when it is not theirs.

    A hard delete, unlike ``api_keys.revoke``'s soft one, and the asymmetry is
    deliberate. A revoked key must survive because ``requests.api_key_id``
    attributes historical usage to it; a deleted conversation must not, because
    the user asked for their messages to be gone and a hidden row that still
    holds them is not a deletion.

    The two foreign keys pointing here are configured for exactly that split:
    ``messages.conversation_id`` cascades, so the content goes; ``requests
    .conversation_id`` is ``ON DELETE SET NULL``, so the usage and cost history
    survives with the message text it referred to already removed.
    """
    result = cast(
        CursorResult[Any],
        await session.execute(
            sql_delete(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
            )
        ),
    )
    return result.rowcount > 0
