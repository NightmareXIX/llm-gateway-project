"""``/v1/conversations`` — list, read, rename, delete.

Trivial by design: every route is a call to an ownership-scoped repository
function plus a mapping to a wire model. That is the payoff for the rule that
ownership lives in the SQL — there is no place in this module where a check could
be forgotten, because there is no unscoped read to forget it on.

**A miss is a 404, never a 403.** The repositories scope by ``user_id`` inside
the query, so "someone else's conversation" and "no such conversation" produce the
same ``None`` and the same answer. A 403 would confirm the id names something
real, which is exactly the thing an enumeration attempt is looking for.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, status

from app.auth.dependency import PrincipalDep
from app.core.errors import NotFound
from app.core.logging import get_logger
from app.db.models import Conversation
from app.db.repo import conversations as conversations_repo
from app.db.repo import messages as messages_repo
from app.deps import SessionDep
from app.memory.canonical import CanonicalMessage
from app.schemas.conversations import (
    ConversationDetail,
    ConversationOut,
    ConversationRenameRequest,
    MessageOut,
    MessagePageOut,
)
from app.schemas.errors import AUTHENTICATED_ERROR_RESPONSES, NOT_FOUND_RESPONSE

logger = get_logger("app.api.conversations")

router = APIRouter(
    prefix="/v1/conversations",
    tags=["conversations"],
    responses=AUTHENTICATED_ERROR_RESPONSES,
)


def _to_out(conversation: Conversation) -> ConversationOut:
    return ConversationOut(
        id=conversation.id,
        title=conversation.title,
        preferred_slot=conversation.preferred_slot,
        pinned_model=conversation.pinned_model,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


def _to_message_out(message: CanonicalMessage) -> MessageOut:
    """Canonical message → wire message.

    ``content`` crosses unchanged: the blocks are ``TypedDict``s, they were
    validated on the way out of the database, and re-shaping them here would give
    the frontend a second definition of a block free to drift from the stored one.
    ``meta`` goes through ``to_jsonb`` so every key is present even when it is
    ``None`` — a reader should not have to know which app version wrote a row to
    tell "false" from "not recorded".
    """
    return MessageOut(
        id=message.id,
        seq=message.seq,
        role=message.role,
        content=[dict(block) for block in message.content],
        meta=message.meta.to_jsonb(),
        created_at=message.created_at,
    )


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    principal: PrincipalDep,
    session: SessionDep,
    limit: int = Query(
        default=conversations_repo.DEFAULT_PAGE_SIZE,
        ge=1,
        le=conversations_repo.MAX_PAGE_SIZE,
    ),
    offset: int = Query(default=0, ge=0),
) -> list[ConversationOut]:
    """The sidebar: the caller's threads, most recently active first."""
    conversations = await conversations_repo.list_for_user(
        session, user_id=principal.user_id, limit=limit, offset=offset
    )
    return [_to_out(conversation) for conversation in conversations]


@router.get("/{conversation_id}", response_model=ConversationDetail, responses=NOT_FOUND_RESPONSE)
async def read_conversation(
    conversation_id: UUID,
    principal: PrincipalDep,
    session: SessionDep,
) -> ConversationDetail:
    """One thread with its newest page of history, oldest first within the page.

    Two queries rather than a join, and both ownership-scoped. The first is what
    distinguishes "empty new thread" from "not yours" — the message read returns
    an empty page for both, so it cannot answer that on its own.

    D48: only the newest page comes back here. Older history is
    ``GET /{conversation_id}/messages``'s job — a second route rather than a
    query parameter on this one, because the two have different cache
    lifetimes on the client (this one is mutated by an optimistic turn; old
    pages are immutable).
    """
    conversation = await conversations_repo.get_owned(
        session, conversation_id=conversation_id, user_id=principal.user_id
    )
    if conversation is None:
        raise NotFound("That conversation does not exist.", code="conversation_not_found")

    page = await messages_repo.list_page_for_conversation(
        session, conversation_id=conversation_id, user_id=principal.user_id
    )
    return ConversationDetail(
        **_to_out(conversation).model_dump(),
        messages=[_to_message_out(message) for message in page.messages],
        has_more=page.has_more,
        next_before_seq=page.next_before_seq,
    )


@router.get(
    "/{conversation_id}/messages",
    response_model=MessagePageOut,
    responses=NOT_FOUND_RESPONSE,
)
async def read_message_page(
    conversation_id: UUID,
    principal: PrincipalDep,
    session: SessionDep,
    before_seq: int | None = Query(default=None, ge=0),
    limit: int = Query(
        default=messages_repo.DEFAULT_PAGE_SIZE,
        ge=1,
        le=messages_repo.MAX_PAGE_SIZE,
    ),
) -> MessagePageOut:
    """Older history than the thread route's own page — the scroll-up fetch.

    Ownership is resolved with ``get_owned`` first, exactly like the thread
    route, so a non-owner (or an unknown id) gets 404 rather than an empty page
    — the same distinction the unpaginated read needs a separate check for.
    """
    conversation = await conversations_repo.get_owned(
        session, conversation_id=conversation_id, user_id=principal.user_id
    )
    if conversation is None:
        raise NotFound("That conversation does not exist.", code="conversation_not_found")

    page = await messages_repo.list_page_for_conversation(
        session,
        conversation_id=conversation_id,
        user_id=principal.user_id,
        before_seq=before_seq,
        limit=limit,
    )
    return MessagePageOut(
        messages=[_to_message_out(message) for message in page.messages],
        has_more=page.has_more,
        next_before_seq=page.next_before_seq,
    )


@router.patch(
    "/{conversation_id}",
    response_model=ConversationOut,
    responses=NOT_FOUND_RESPONSE,
)
async def rename_conversation(
    conversation_id: UUID,
    body: ConversationRenameRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> ConversationOut:
    """Set or clear the title."""
    renamed = await conversations_repo.rename(
        session,
        conversation_id=conversation_id,
        user_id=principal.user_id,
        title=body.title,
    )
    if not renamed:
        raise NotFound("That conversation does not exist.", code="conversation_not_found")
    await session.commit()

    # Re-read rather than construct: `rename` also stamps `updated_at`, and a
    # response built from the request body would report the old one.
    conversation = await conversations_repo.get_owned(
        session, conversation_id=conversation_id, user_id=principal.user_id
    )
    if conversation is None:  # pragma: no cover — deleted between the two statements
        raise NotFound("That conversation does not exist.", code="conversation_not_found")

    logger.info("conversation.renamed", conversation_id=str(conversation_id))
    return _to_out(conversation)


@router.delete(
    "/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=NOT_FOUND_RESPONSE,
)
async def delete_conversation(
    conversation_id: UUID,
    principal: PrincipalDep,
    session: SessionDep,
) -> None:
    """Delete a thread and its messages.

    A hard delete: the user asked for their messages to be gone. The ``requests``
    rows survive with ``conversation_id`` set to NULL, so usage history is not
    rewritten by someone tidying up their sidebar.
    """
    deleted = await conversations_repo.delete(
        session, conversation_id=conversation_id, user_id=principal.user_id
    )
    if not deleted:
        raise NotFound("That conversation does not exist.", code="conversation_not_found")

    await session.commit()
    logger.info("conversation.deleted", conversation_id=str(conversation_id))
