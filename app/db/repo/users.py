"""Reads and writes against ``users``.

The local ``users`` row is a *mirror*, not a source of truth. Supabase owns
identity — the id here is the ``sub`` claim, and email plus verification state
are whatever the current token says. The row exists so foreign keys stay local
and so app-specific fields (``tier``) can be added without touching the auth
schema (§1.2).

Repositories take a session and never commit. The caller owns the transaction
boundary, because only the caller knows what one unit of work is.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict
from app.db.models import User


def normalize_email(email: str) -> str:
    """Lower-case and strip. The unique index is only real if writes agree."""
    return email.strip().lower()


async def get_by_id(session: AsyncSession, user_id: UUID) -> User | None:
    return await session.get(User, user_id)


async def upsert_from_claims(
    session: AsyncSession,
    *,
    user_id: UUID,
    email: str,
    email_verified: bool,
) -> User:
    """Create or refresh the local mirror of a Supabase user.

    Runs on every JWT-authenticated request. One statement — ``INSERT ... ON
    CONFLICT DO UPDATE`` — rather than a SELECT followed by a conditional INSERT,
    which is a race whenever a new user's first two requests arrive together.

    ``email`` and ``email_verified`` are overwritten every time on purpose: a user
    who confirms their address, or changes it in Supabase, is reflected on their
    next request rather than at some later sync. ``tier`` is *not* in the update
    set — it is ours, not Supabase's, and must survive.
    """
    normalized = normalize_email(email)

    statement = (
        pg_insert(User)
        .values(id=user_id, email=normalized, email_verified=email_verified)
        .on_conflict_do_update(
            index_elements=[User.id],
            set_={"email": normalized, "email_verified": email_verified},
        )
        .returning(User)
        # Without this, an identity-mapped User already loaded in this session
        # wins over the row we just wrote, and the caller gets back the values
        # from before the upsert. One session per request hides it in production;
        # it is a real staleness bug regardless.
        .execution_options(populate_existing=True)
    )

    try:
        result = await session.execute(statement)
    except IntegrityError as exc:
        # The id conflict is handled above, so reaching here means the *email*
        # unique index fired: this address already belongs to a different user
        # id. In practice that is a Supabase account deleted and recreated while
        # our mirror still holds the old row. Surfacing it as a 409 with an
        # actionable code beats a 500 that says nothing.
        #
        # The transaction is left aborted, which is correct — the request ends
        # here and `get_session` closes without committing.
        raise Conflict(
            "This email address is already registered to a different account.",
            code="email_taken",
        ) from exc

    user = result.scalar_one()
    return user
