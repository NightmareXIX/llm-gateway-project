"""Gateway API key management — ``/v1/keys`` — and BYOK provider keys —
``/v1/provider-keys``.

Every route in both routers requires a *session*. An API-key principal is
refused, and that restriction is the point rather than an inconvenience: a
leaked ``gw_live_`` key that can mint more keys survives the revocation of the
original, a leaked key that can list keys tells an attacker exactly what else
to go looking for, and a leaked key that could add or read someone's own
upstream provider credential would let a compromised integration bill a
victim's Gemini account. Bootstrapping any of these requires proving you own
the account.

The BYOK routes implement §9.2's add flow and §9.8's rate limit, and nothing
downstream reads a stored key yet — the resolver that spends one is Phase 6
Step 4.
"""

from __future__ import annotations

from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.auth.api_keys import generate_api_key, mask
from app.auth.dependency import PrincipalDep
from app.config import get_providers_config
from app.core.clock import SYSTEM_CLOCK
from app.core.crypto import decrypt_provider_key, encrypt_provider_key
from app.core.errors import AppError, Forbidden, InvalidRequest, NotFound
from app.core.logging import get_logger
from app.db.models import ApiKey, ProviderKey
from app.db.repo import api_keys as api_keys_repo
from app.db.repo import provider_keys as provider_keys_repo
from app.deps import RPH, RateLimiterDep, RegistryDep, SessionDep
from app.providers.errors import ProviderError
from app.providers.registry import UnknownSlot
from app.schemas.errors import AUTHENTICATED_ERROR_RESPONSES, NOT_FOUND_RESPONSE
from app.schemas.keys import (
    ApiKeyCreated,
    ApiKeyCreateRequest,
    ApiKeyOut,
    ProviderKeyCreateRequest,
    ProviderKeyOut,
    ProviderKeyStatus,
)

logger = get_logger("app.api.keys")

router = APIRouter(prefix="/v1/keys", tags=["keys"], responses=AUTHENTICATED_ERROR_RESPONSES)

provider_keys_router = APIRouter(
    prefix="/v1/provider-keys", tags=["provider-keys"], responses=AUTHENTICATED_ERROR_RESPONSES
)


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


# --------------------------------------------------------------------------- #
# BYOK provider keys — Phase 6 Step 3 (§9.2, §9.8)
# --------------------------------------------------------------------------- #
class InvalidProviderKey(AppError):
    """422: the provider itself rejected this credential.

    Distinct from the 400 an unknown/disabled provider name gets — the request
    was fine, the *key* was not, and §9.2 wants a message a human can act on
    rather than a generic 400.
    """

    status_code = 422
    code = "invalid_provider_key"
    message = "The provider rejected this key."


class ProviderKeyCheckUnavailable(AppError):
    """503: we could not check the key at all — the provider was unreachable.

    Never ``InvalidProviderKey``. §9.2's whole point: telling a user their key
    is bad when the truth is that the provider was down is the confusing
    failure this distinction exists to avoid.
    """

    status_code = 503
    code = "provider_unavailable"
    message = (
        "Could not verify this key right now — the provider was unreachable. Try again shortly."
    )


KEY_VALIDATION_LIMIT_PER_HOUR: Final = 5
"""D43: 5/hour/user on the two routes that call an upstream provider on a
caller-supplied credential. A constant here, not in ``limits.yaml`` — that
file's ``gateway:`` block is per-tier throughput policy, and an anti-abuse
floor on one endpoint is neither per-tier nor throughput."""


async def _enforce_key_validation_limit(principal: PrincipalDep, limiter: RateLimiterDep) -> None:
    """D43, composed the same way ``api/v1/chat.py``'s ``RateLimitDep`` is.

    ``get_principal`` is resolved once per request and cached by FastAPI, so
    depending on it here and on the endpoint itself costs one authentication,
    not two. ``limiter is None`` means ``RATE_LIMIT_ENABLED=false``, which
    skips this the same way the chat path does.
    """
    if limiter is not None:
        await limiter.enforce_one(principal.user_id, RPH, KEY_VALIDATION_LIMIT_PER_HOUR)


ValidationRateLimitDep = Annotated[None, Depends(_enforce_key_validation_limit)]


def _mask_provider_key(last_4: str) -> str:
    """``"••••a91c"`` — reveals nothing usable, unlike ``auth.api_keys.mask``
    there is no stable prefix to show alongside it, since a provider key's
    shape is the provider's to define, not ours."""
    return f"••••{last_4}"


def _to_provider_key_out(key: ProviderKey) -> ProviderKeyOut:
    return ProviderKeyOut(
        provider=key.provider,
        masked=_mask_provider_key(key.last_4),
        nickname=key.nickname,
        # The column is `str`; the CHECK constraint enforces the same three
        # values the Literal names, so this narrowing cannot fail in practice.
        validation_status=key.validation_status,  # type: ignore[arg-type]
        last_validated_at=key.last_validated_at,
        last_used_at=key.last_used_at,
        is_active=key.is_active,
        created_at=key.created_at,
    )


@provider_keys_router.get("", response_model=list[ProviderKeyStatus])
async def list_provider_keys(
    principal: PrincipalDep, session: SessionDep
) -> list[ProviderKeyStatus]:
    """One row per **enabled** provider in ``providers.yaml``, joined against
    this user's active rows — providers, not slots (D36)."""
    _require_session(principal)

    active_by_provider = {
        row.provider: row
        for row in await provider_keys_repo.list_active_for_user(session, principal.user_id)
    }
    config = get_providers_config()
    return [
        ProviderKeyStatus(
            provider=name,
            pool="private" if name in active_by_provider else "shared",
            key=_to_provider_key_out(active_by_provider[name])
            if name in active_by_provider
            else None,
        )
        for name, entry in config.providers.items()
        if entry.enabled
    ]


@provider_keys_router.post(
    "", response_model=ProviderKeyStatus, status_code=status.HTTP_201_CREATED
)
async def add_provider_key(
    body: ProviderKeyCreateRequest,
    principal: PrincipalDep,
    session: SessionDep,
    registry: RegistryDep,
    _rate_limit: ValidationRateLimitDep,
) -> ProviderKeyStatus:
    """§9.2's add flow, in this order and no other: rate limit, provider
    lookup, live validation, and only *then* a write. A garbage key never
    reaches the database — see ``test_provider_keys_endpoints.py`` for the
    assertion that nothing was stored.
    """
    _require_session(principal)

    try:
        adapter = registry.adapter_for(body.provider)
    except UnknownSlot as exc:
        raise InvalidRequest(
            f"{body.provider!r} is not a known, enabled provider.", code="unknown_provider"
        ) from exc

    plaintext = body.key.get_secret_value()

    try:
        validation = await adapter.validate_key(plaintext)
    except ProviderError as exc:
        # We learned nothing about the key — the provider was unreachable, not
        # the key being bad. Nothing is written on this path either.
        logger.warning(
            "provider_key.validation_unavailable",
            provider=body.provider,
            error_type=type(exc).__name__,
        )
        raise ProviderKeyCheckUnavailable(
            f"Could not verify this {body.provider} key right now — the provider "
            "was unreachable. Try again shortly."
        ) from exc

    if not validation.valid:
        raise InvalidProviderKey(validation.detail or f"{body.provider} rejected this key.")

    row = await provider_keys_repo.upsert(
        session,
        user_id=principal.user_id,
        provider=body.provider,
        encrypted_key=encrypt_provider_key(plaintext),
        last_4=plaintext[-4:],
        nickname=body.nickname,
        validation_status="valid",
        last_validated_at=SYSTEM_CLOCK.now(),
    )
    await session.commit()

    logger.info("provider_key.added", provider=body.provider)
    return ProviderKeyStatus(provider=body.provider, pool="private", key=_to_provider_key_out(row))


@provider_keys_router.delete(
    "/{provider}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=NOT_FOUND_RESPONSE,
)
async def remove_provider_key(provider: str, principal: PrincipalDep, session: SessionDep) -> None:
    """Remove this user's key for one provider. No key stored is a 404."""
    _require_session(principal)

    removed = await provider_keys_repo.deactivate(
        session, user_id=principal.user_id, provider=provider
    )
    if not removed:
        raise NotFound("No stored key for that provider.", code="provider_key_not_found")

    await session.commit()
    logger.info("provider_key.removed", provider=provider)


@provider_keys_router.post("/{provider}/validate", response_model=ProviderKeyStatus)
async def revalidate_provider_key(
    provider: str,
    principal: PrincipalDep,
    session: SessionDep,
    registry: RegistryDep,
    _rate_limit: ValidationRateLimitDep,
) -> ProviderKeyStatus:
    """The settings page's "check again" button — re-check a stored key
    without re-pasting it. Updates ``validation_status``/``last_validated_at``
    either way (:func:`app.db.repo.provider_keys.record_validation_result`).
    """
    _require_session(principal)

    row = await provider_keys_repo.get_active(session, user_id=principal.user_id, provider=provider)
    if row is None:
        raise NotFound("No stored key for that provider.", code="provider_key_not_found")

    try:
        adapter = registry.adapter_for(provider)
    except UnknownSlot as exc:
        raise InvalidRequest(
            f"{provider!r} is not a known, enabled provider.", code="unknown_provider"
        ) from exc

    plaintext = decrypt_provider_key(row.encrypted_key)

    try:
        validation = await adapter.validate_key(plaintext)
    except ProviderError as exc:
        logger.warning(
            "provider_key.validation_unavailable",
            provider=provider,
            error_type=type(exc).__name__,
        )
        raise ProviderKeyCheckUnavailable(
            f"Could not verify this {provider} key right now — the provider "
            "was unreachable. Try again shortly."
        ) from exc

    await provider_keys_repo.record_validation_result(
        session, key_id=row.id, valid=validation.valid, validated_at=SYSTEM_CLOCK.now()
    )
    await session.commit()
    await session.refresh(row)

    logger.info("provider_key.revalidated", provider=provider, valid=validation.valid)
    return ProviderKeyStatus(provider=provider, pool="private", key=_to_provider_key_out(row))
