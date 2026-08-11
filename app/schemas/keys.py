"""Wire models for gateway API key management.

The plaintext key appears in exactly one type in this file, on exactly one
response. Everything else carries the masked form.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ApiKeyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    nickname: str | None = Field(default=None, max_length=64)
    """A label for the human who has to decide which key to revoke later."""


class ApiKeyOut(BaseModel):
    """A stored key, as safe to display as it will ever be."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    masked: str
    """``gw_live_…a91c``. The full key is not recoverable from anywhere."""

    nickname: str | None
    is_active: bool
    last_used_at: datetime | None
    created_at: datetime


class ApiKeyCreated(ApiKeyOut):
    """The creation response — the only place a usable credential is ever sent.

    Extends :class:`ApiKeyOut` so the client can render the new key into its list
    without a second request.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    """The plaintext, shown once. It is not stored and cannot be shown again."""
