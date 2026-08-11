"""Session bootstrap — ``GET /v1/me``."""

from __future__ import annotations

from fastapi import APIRouter

from app.auth.dependency import PrincipalDep
from app.core.errors import NotFound
from app.db.repo import users as users_repo
from app.deps import SessionDep
from app.schemas.auth import MeResponse
from app.schemas.errors import AUTHENTICATED_ERROR_RESPONSES, NOT_FOUND_RESPONSE

router = APIRouter(prefix="/v1", tags=["auth"], responses=AUTHENTICATED_ERROR_RESPONSES)


@router.get("/me", response_model=MeResponse, responses=NOT_FOUND_RESPONSE)
async def read_me(principal: PrincipalDep, session: SessionDep) -> MeResponse:
    """Identity for the caller, whichever credential they used.

    The ``users`` row is read rather than carried on the :class:`Principal`:
    ``email`` and ``email_verified`` are display concerns, and keeping them off
    the principal is what stops it from drifting into a general-purpose user
    object that every layer reads a different field from.
    """
    user = await users_repo.get_by_id(session, principal.user_id)
    if user is None:
        # A principal always implies a row: the JWT path upserts it and the API
        # key path joins to it. Reaching this means the user was deleted between
        # authentication and now.
        raise NotFound("This account no longer exists.", code="user_not_found")

    return MeResponse(
        user_id=user.id,
        email=user.email,
        email_verified=user.email_verified,
        tier=user.tier,
        auth_method=principal.auth_method,
        api_key_id=principal.api_key_id,
    )
