"""Gateway API key management — ``/v1/keys``.

Every route here requires a *session*. An API-key principal is refused, and that
restriction is the point rather than an inconvenience: a leaked ``gw_live_`` key
that can mint more keys survives the revocation of the original, and a leaked key
that can list keys tells an attacker exactly what else to go looking for.
Bootstrapping a credential requires proving you own the account.

BYOK — the user's *own* Gemini/Groq/OpenRouter keys — is a different table and a
different flow, and lands in Phase 6 (§9).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status

from app.auth.api_keys import generate_api_key, mask
from app.auth.dependency import PrincipalDep
from app.core.errors import Forbidden, NotFound
from app.core.logging import get_logger
from app.db.models import ApiKey
from app.db.repo import api_keys as api_keys_repo
from app.deps import SessionDep
from app.schemas.errors import AUTHENTICATED_ERROR_RESPONSES, NOT_FOUND_RESPONSE
from app.schemas.keys import ApiKeyCreated, ApiKeyCreateRequest, ApiKeyOut

logger = get_logger("app.api.keys")

router = APIRouter(prefix="/v1/keys", tags=["keys"], responses=AUTHENTICATED_ERROR_RESPONSES)


def _require_session(principal: PrincipalDep) -> None:
    """Refuse key management to anything holding only an API key."""
    if not principal.is_session:
        raise Forbidden(
            "API keys can only be managed from a signed-in session.",
            code="session_required",
        )


def _to_out(api_key: ApiKey) -> ApiKeyOut:
    return ApiKeyOut(
        id=api_key.id,
        masked=mask(api_key.key_prefix, api_key.last_4),
        nickname=api_key.nickname,
        is_active=api_key.is_active,
        last_used_at=api_key.last_used_at,
        created_at=api_key.created_at,
    )


@router.post("", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_key(
    body: ApiKeyCreateRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> ApiKeyCreated:
    """Mint a key and return it once.

    This response is the only time the plaintext exists outside the caller's
    process. It is not logged here and it is not recoverable later — the row
    holds a SHA-256 digest and four characters.
    """
    _require_session(principal)

    generated = generate_api_key()
    api_key = await api_keys_repo.create(
        session,
        user_id=principal.user_id,
        key_hash=generated.key_hash,
        key_prefix=generated.key_prefix,
        last_4=generated.last_4,
        nickname=body.nickname,
    )
    await session.commit()

    logger.info("api_key.created", api_key_id=str(api_key.id))
    out = _to_out(api_key)
    return ApiKeyCreated(**out.model_dump(), key=generated.plaintext)


@router.get("", response_model=list[ApiKeyOut])
async def list_keys(principal: PrincipalDep, session: SessionDep) -> list[ApiKeyOut]:
    """The caller's keys, masked. Revoked ones are included and flagged."""
    _require_session(principal)

    keys = await api_keys_repo.list_for_user(session, principal.user_id)
    return [_to_out(key) for key in keys]


@router.delete(
    "/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=NOT_FOUND_RESPONSE,
)
async def revoke_key(key_id: UUID, principal: PrincipalDep, session: SessionDep) -> None:
    """Revoke a key. Someone else's key is a 404, not a 403.

    The repo scopes the UPDATE by ``user_id``, so a key belonging to another
    account simply does not match — and a 403 would confirm that the id names a
    real key.
    """
    _require_session(principal)

    revoked = await api_keys_repo.revoke(session, key_id=key_id, user_id=principal.user_id)
    if not revoked:
        raise NotFound("No such API key.", code="api_key_not_found")

    await session.commit()
    logger.info("api_key.revoked", api_key_id=str(key_id))
