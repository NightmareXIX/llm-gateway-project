"""Wire models for the auth surface."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.auth.principal import AuthMethod


class MeResponse(BaseModel):
    """Who the caller is, as the gateway sees them.

    The frontend's session bootstrap: one call that proves the token works, tells
    the UI whether to show an unverified-email banner, and yields the ``tier``
    that decides what to render.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: UUID
    email: str
    email_verified: bool
    tier: str

    auth_method: AuthMethod
    """Echoed back so an integration can confirm which credential was used —
    the most common cause of "why am I seeing someone else's data?" is a stale
    header the caller forgot was set."""

    api_key_id: UUID | None = None
