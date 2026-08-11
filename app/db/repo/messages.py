"""Reads and writes against ``messages`` — the canonical history (Contract B).

Rows in this table are stored and returned as :class:`CanonicalMessage`, never as
ORM objects. That is not a style preference: the whole value of Contract B is
that exactly one shape crosses this boundary, and handing a ``Message`` row to
the render pipeline would let a caller reach for ``row.content[0]["type"]``
against unvalidated JSONB. Everything leaving here has been through
``parse_content`` / ``parse_meta`` and is known-good.

**Appending is serialized per conversation.** ``seq`` must be unique *and*
gap-free (invariant 2), and "read the max, add one, insert" is a textbook race:
two concurrent requests read the same maximum and both write it. The unique
constraint would catch the collision, but one user's message would fail for a
reason they could do nothing about. Instead the append takes a row lock on the
owning conversation, which serializes appends to that one thread and leaves every
other conversation in the system untouched.

Locking ``conversations`` rather than ``messages`` matters for the empty case: a
lock over "the rows of this conversation" locks nothing at all when there are no
rows yet, so the very first two messages of a new thread would still collide.
The parent row always exists.

Repositories take a session and never commit. The caller owns the transaction
boundary, because only the caller knows what one unit of work is.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.db.models import Conversation, Message
from app.memory.canonical import (
    SCHEMA_VERSION,
    CanonicalMessage,
    ContentBlock,
    MessageMeta,
    Role,
    parse_content,
    parse_meta,
    parse_role,
    validate_append,
    validate_message,
)


def to_canonical(row: Message) -> CanonicalMessage:
    """Map a stored row to the canonical form, validating the JSONB on the way.

    The validation is not redundant with the write path. ``content`` and ``meta``
    are schemaless as far as Postgres is concerned, so a migration, a manual fix
    in psql, or a bulk write from a future version can leave a row that no live
    code path would have produced. Catching that here means the render pipeline
    can treat its input as trustworthy.
    """
    return CanonicalMessage(
        id=row.id,
        conversation_id=row.conversation_id,
        role=parse_role(row.role),
        content=parse_content(row.content),
        meta=parse_meta(row.meta),
        created_at=row.created_at,
        seq=row.seq,
        schema_version=row.schema_version,
    )


async def append(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    user_id: UUID,
    role: Role,
    content: list[ContentBlock],
    meta: MessageMeta | None = None,
) -> CanonicalMessage:
    """Add a message to the end of a conversation the caller owns.

    Raises :class:`NotFound` when the conversation is not theirs, and
    :class:`InvariantViolation` when the message would break Contract B.

    The sequence below is four statements inside the caller's transaction, and
    the order is the point:

    1. Lock the conversation row, ownership-scoped. One statement doing two jobs
       — the ``WHERE user_id`` is the ownership check, and ``FOR UPDATE`` is what
       makes step 2 race-free.
    2. Read the tail. One row gives both the next ``seq`` and the previous role,
       which is everything the ordering invariants need; loading the whole
       history to append one message would make cost grow with thread length.
    3. Validate *before* writing, so a violation surfaces as a named invariant
       rather than as a constraint error the caller has to decode.
    4. Insert.
    """
    locked = await session.execute(
        select(Conversation.id)
        .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
        .with_for_update()
    )
    if locked.scalar_one_or_none() is None:
        # Not "you may not" — as far as this user is concerned it does not exist.
        raise NotFound("That conversation does not exist.", code="conversation_not_found")

    previous = await get_last(session, conversation_id=conversation_id)
    seq = 0 if previous is None else previous.seq + 1
    message_meta = meta if meta is not None else MessageMeta()

    validate_message(role=role, content=content, meta=message_meta, seq=seq)
    validate_append(previous, role=role, seq=seq)

    row = Message(
        conversation_id=conversation_id,
        seq=seq,
        role=role,
        # `content` is already JSONB-shaped — TypedDicts are dicts. This is the
        # payoff for choosing them over dataclasses: no serialization step, and
        # no second representation that can drift from the stored one.
        content=list(content),
        meta=message_meta.to_jsonb(),
        schema_version=SCHEMA_VERSION,
    )
    session.add(row)
    await session.flush()  # assigns id and created_at before we build the result
    return to_canonical(row)


async def get_last(session: AsyncSession, *, conversation_id: UUID) -> CanonicalMessage | None:
    """The current tail of a conversation, or ``None`` when it is empty.

    Not ownership-scoped, and the only function here that is not: its single
    caller is :func:`append`, which has already taken the lock and proved
    ownership two statements earlier. Re-joining ``conversations`` would repeat
    that check without adding a guarantee. Keep it internal to this module.
    """
    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.seq.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    return None if row is None else to_canonical(row)


async def list_for_conversation(
    session: AsyncSession, *, conversation_id: UUID, user_id: UUID
) -> list[CanonicalMessage]:
    """The full history, oldest first, for a conversation the caller owns.

    Ownership is scoped by joining ``conversations`` inside the query rather than
    by checking a separately-fetched row, so there is no window in which the
    wrong user's messages exist in memory.

    A non-owner gets ``[]`` — but so does the owner of a brand-new thread, so an
    empty list is not a 404 signal. Callers that need to distinguish the two
    resolve the conversation with ``conversations.get_owned`` first; that is the
    query whose ``None`` means "not found".

    Unpaginated on purpose. This is a *conversation*, and the render pipeline's
    fitting step (D4) needs the whole thing to decide what to drop — a page of it
    would just move the truncation decision somewhere that cannot make it well.
    """
    result = await session.execute(
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
        .order_by(Message.seq)
    )
    return [to_canonical(row) for row in result.scalars().all()]
