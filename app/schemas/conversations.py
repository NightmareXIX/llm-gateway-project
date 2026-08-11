"""Wire models for ``/v1/conversations`` — the history the UI reads.

**Messages go out in the canonical shape.** ``content`` is the stored list of
content blocks and ``meta`` is the stored :class:`~app.memory.canonical.MessageMeta`,
not a flattened string and not a reshaped summary. Two reasons: the frontend's
``ModelIndicator`` renders ``meta`` directly, which is what makes the D1/D2
disclosure free at render time (§2.2.2); and a ``file_ref`` or an
``omission_marker`` has to survive the trip, because "4 earlier messages omitted"
is a thing the transcript must be able to show.

Typed as loose JSON objects rather than as a discriminated union of block models.
The blocks already have exactly one definition — ``app/memory/canonical.py``, which
validates every value on the way out of the database — and a second, structurally
identical definition here would be a shape free to drift from the stored one, for
the sole benefit of a slightly prettier OpenAPI schema.

Request/response models only; nothing here imports from the rest of the app. The
mapping from ``CanonicalMessage`` lives in ``app/api/v1/conversations.py``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MessageOut(BaseModel):
    """One stored message, canonical blocks and all."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    seq: int
    """The ordering key. Not ``created_at`` — two messages written in the same
    millisecond are indistinguishable by timestamp."""

    role: str
    content: list[dict[str, Any]]
    meta: dict[str, Any]
    """Provenance: ``provider_used``, ``model_used``, ``slot_used``,
    ``substituted``, ``attempts``, tokens. Populated on assistant messages,
    empty of provider fields on user ones (invariant 5)."""

    created_at: datetime


class ConversationOut(BaseModel):
    """A thread, without its messages. The sidebar's row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    title: str | None
    """``None`` until someone renames it. Phase 1 has no title generation, and a
    placeholder written now would be indistinguishable later from a title the
    user actually chose — the UI shows the first message instead."""

    preferred_slot: str
    pinned_model: str | None
    """D3 seam: set by a conversation's first tool call, honoured by the router
    thereafter. Always ``None`` in v1."""

    created_at: datetime
    updated_at: datetime
    """Bumped when a turn is taken. The sidebar orders on this."""


class ConversationDetail(ConversationOut):
    """A thread with its full history, oldest first.

    Unpaginated, like the repository read behind it: this is a conversation, and
    the client needs all of it to render the transcript.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    messages: list[MessageOut]


class ConversationRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str | None = Field(default=None, max_length=200)
    """``None`` clears the title back to the UI's derived display name."""
