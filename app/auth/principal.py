"""The authenticated caller — the one object the rest of the gateway sees.

D7 gives the gateway two front doors: a Supabase session JWT for humans in the
browser, and a ``gw_live_`` API key for programmatic use. They verify completely
differently and they both end here. Nothing downstream — the router, the quota
tracker, the conversation repos — knows or cares which door was used.

Four fields, deliberately. This is a frozen contract (§1.2); widening it is the
easy way to end up with half the app reading ``principal.email`` and the other
half re-fetching the user row anyway. Anything beyond identity, auth method and
tier comes from the ``users`` table via ``app/db/repo/users.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

AuthMethod = Literal["session", "api_key"]


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is making this request, and how they proved it."""

    user_id: UUID
    """The Supabase ``sub`` claim, mirrored as ``users.id``.

    **Quota and rate limiting key on this, never on ``api_key_id``** (§1.2). A
    user with three API keys is one user with one budget, not three."""

    auth_method: AuthMethod
    """``session`` for a verified Supabase JWT, ``api_key`` for a gateway key.

    Read by the key-management endpoints, which refuse to mint or list keys for
    an ``api_key`` principal — otherwise a leaked key can issue its own
    successors and survive the revocation of the original."""

    api_key_id: UUID | None
    """The key that authenticated this request, or ``None`` for a session.

    Kept on the ``requests`` row for attribution: which integration made this
    call, as distinct from which user's budget paid for it."""

    tier: str
    """``free`` | ``plus``. Drives ``user_quota_allocations`` from Phase 3.

    Text rather than a ``Literal`` on purpose — tiers are business configuration
    and adding one should not be a code change plus a migration."""

    @property
    def is_session(self) -> bool:
        """True when a human authenticated in a browser, not an integration."""
        return self.auth_method == "session"
