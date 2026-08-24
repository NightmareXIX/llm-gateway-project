"""Wire models for gateway API key management, and BYOK provider keys (§9.9).

The plaintext appears in exactly two places in this file: the gateway key's
one-time creation response, and ``ProviderKeyCreateRequest.key`` — the latter
typed ``SecretStr`` so a validation error's ``repr`` and an accidental
``model_dump()`` both render ``**********`` rather than a live credential.
Everything else carries the masked form.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr


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


# --------------------------------------------------------------------------- #
# BYOK provider keys — Phase 6 Step 3
# --------------------------------------------------------------------------- #
class ProviderKeyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    """A ``providers.yaml`` key, e.g. ``"gemini"``. An unknown or disabled one
    is a 400 raised by the route, not by validation here — the set of known
    providers is a runtime fact of the registry, not a static enum."""

    key: SecretStr
    """The plaintext upstream credential. Never logged, never echoed back —
    ``ProviderKeyOut.masked`` is built from ``last_4`` alone."""

    nickname: str | None = Field(default=None, max_length=64)


class ProviderKeyOut(BaseModel):
    """A stored BYOK credential, as safe to display as it will ever be."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    masked: str
    """``"••••a91c"``, built from ``last_4``. The full key is not recoverable
    from anywhere — that is what ``encrypted_key`` being unread by this schema
    guarantees."""

    nickname: str | None
    validation_status: Literal["valid", "invalid", "unverified"]
    last_validated_at: datetime | None
    last_used_at: datetime | None
    is_active: bool
    created_at: datetime


class ProviderKeyStatus(BaseModel):
    """One row of the settings page — for *every* enabled provider, whether or
    not this caller has a key stored for it.

    A client should not have to know the provider list itself to draw an
    empty "Using shared pool" row; ``GET /v1/provider-keys`` returns one of
    these per enabled provider whether or not the caller has added a key.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    pool: Literal["shared", "private"]
    key: ProviderKeyOut | None = None
    """``None`` on the shared-pool rows. Present, and always active, on the
    private ones — a revoked key simply drops back to ``pool="shared"``
    rather than being carried here in a disabled state."""
