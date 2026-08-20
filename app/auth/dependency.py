"""``get_principal`` — the single door every authenticated request comes through.

Two credential types, one outcome. A Supabase session JWT in ``Authorization:
Bearer`` or a gateway key in ``X-API-Key``, and either way what the endpoint
receives is a :class:`Principal`. No endpoint anywhere branches on which was
used, except the key-management routes, which deliberately refuse API keys.

Order matters: ``Authorization`` is checked first, and a malformed one is a 401
rather than a fallthrough to the API-key path. Letting a broken Bearer header
fall through would mean a client with an expired session and a stale key header
silently authenticates as the key — different identity, different attribution, no
error anywhere.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.api_keys import hash_api_key, parse_api_key
from app.auth.jwt import JwksCache, verify_token
from app.auth.principal import Principal
from app.config import Settings, get_settings
from app.core.clock import SYSTEM_CLOCK
from app.core.errors import Unauthenticated
from app.core.logging import bind_user_id, get_logger
from app.db.repo import api_keys as api_keys_repo
from app.db.repo import users as users_repo
from app.deps import RateLimiterDep, SessionDep

logger = get_logger("app.auth")

BEARER_SCHEME = "bearer"
API_KEY_HEADER = "X-API-Key"

CODE_MISSING_CREDENTIALS = "missing_credentials"
CODE_INVALID_API_KEY = "invalid_api_key"


def _bearer_token(header_value: str | None) -> str | None:
    """Extract the token from an ``Authorization`` header, or ``None`` if absent.

    Raises when the header is present but not a usable Bearer credential — see
    the module docstring on why that must not fall through.
    """
    if header_value is None:
        return None

    scheme, _, token = header_value.partition(" ")
    if scheme.lower() != BEARER_SCHEME or not token.strip():
        logger.warning("auth.malformed_authorization_header")
        raise Unauthenticated(
            "The Authorization header must be of the form 'Bearer <token>'.",
            code="invalid_authorization_header",
        )
    return token.strip()


async def _principal_from_jwt(
    token: str,
    *,
    session: AsyncSession,
    jwks: JwksCache,
    settings: Settings,
) -> Principal:
    """Verify a session token and mirror the user locally."""
    claims = await verify_token(token, jwks=jwks, settings=settings)

    user = await users_repo.upsert_from_claims(
        session,
        user_id=claims.sub,
        email=claims.email,
        email_verified=claims.email_verified,
    )
    # Authentication is its own unit of work: it is complete and correct whether
    # or not the endpoint's work then succeeds, and the endpoint's own writes
    # need this row to exist for their foreign keys.
    await session.commit()

    return Principal(
        user_id=user.id,
        auth_method="session",
        api_key_id=None,
        tier=user.tier,
    )


async def _principal_from_api_key(
    presented: str,
    *,
    session: AsyncSession,
) -> Principal:
    """Resolve a ``gw_live_`` key to its owner."""
    found = await api_keys_repo.get_active_with_user(session, hash_api_key(presented))
    if found is None:
        # Unknown, revoked and inactive are one response on purpose. Telling a
        # caller "that key exists but is revoked" confirms a valid key format and
        # a real account to someone who by definition should not have either.
        logger.warning("auth.api_key_rejected")
        raise Unauthenticated(
            "The provided API key is not valid.",
            code=CODE_INVALID_API_KEY,
        )

    api_key, user = found
    await api_keys_repo.touch_last_used(session, key_id=api_key.id, clock=SYSTEM_CLOCK)
    await session.commit()

    return Principal(
        user_id=user.id,
        auth_method="api_key",
        api_key_id=api_key.id,
        tier=user.tier,
    )


async def get_principal(request: Request, session: SessionDep) -> Principal:
    """Authenticate the request. The dependency every protected endpoint declares."""
    settings = get_settings()

    token = _bearer_token(request.headers.get("authorization"))
    if token is not None:
        jwks: JwksCache = request.app.state.jwks_cache
        principal = await _principal_from_jwt(token, session=session, jwks=jwks, settings=settings)
    else:
        raw_key = request.headers.get(API_KEY_HEADER)
        if raw_key is None:
            raise Unauthenticated(
                "Provide a Supabase session token in the Authorization header, "
                "or a gateway API key in X-API-Key.",
                code=CODE_MISSING_CREDENTIALS,
            )

        presented = parse_api_key(raw_key)
        if presented is None:
            # Present but not even key-shaped. Same code and message as a key
            # that simply is not ours — the caller learns nothing about which.
            logger.warning("auth.api_key_malformed")
            raise Unauthenticated(
                "The provided API key is not valid.",
                code=CODE_INVALID_API_KEY,
            )

        principal = await _principal_from_api_key(presented, session=session)

    # From here to the end of the request, every log line carries the user id.
    bind_user_id(str(principal.user_id))
    return principal


PrincipalDep = Annotated[Principal, Depends(get_principal)]
"""What endpoints depend on. Named so the function can be refactored freely."""


# --------------------------------------------------------------------------- #
# D20 — the gateway's own per-user limit, as a dependency
# --------------------------------------------------------------------------- #
async def enforce_rate_limit(principal: PrincipalDep, limiter: RateLimiterDep) -> None:
    """Count this request against the caller's tier, and 429 before any work.

    Lives here rather than in ``app/deps.py`` because it needs the principal,
    and this module already imports ``app.deps`` — the dependency that knows
    about both can only live downstream of both. It arrived in
    ``api/v1/chat.py`` in Phase 3 Step 10, when chat was the only endpoint that
    spent anything; Phase 4 Step 3 gives ``POST /v1/files`` the same gate, and
    two route modules sharing a dependency through a third is better than one
    importing the other.

    **Writes only, still.** Rate-limiting the conversation list makes the UI
    feel broken and protects nothing. What is limited is what costs: a
    generation spends a free tier's quota, and an upload spends storage and a
    request's worth of streaming — the D20 limiter is keyed on the user and
    does not care which.
    """
    if limiter is not None:
        await limiter.enforce(principal)


RateLimitDep = Annotated[None, Depends(enforce_rate_limit)]
"""Declared by an endpoint purely for its side effect: a 429 before any work.

``get_principal`` is resolved once per request and cached by FastAPI, so
depending on it from both here and the endpoint costs one authentication, not
two."""
