"""Supabase token verification — the part of the gateway an attacker reaches first."""

from __future__ import annotations

from uuid import uuid4

import pytest
from jose import jwt

from app.auth.jwt import (
    CODE_EMAIL_NOT_VERIFIED,
    CODE_INVALID_TOKEN,
    CODE_TOKEN_EXPIRED,
    JwksCache,
    verify_token,
)
from app.config import Settings
from app.core.clock import FixedClock
from app.core.errors import ServiceUnavailable, Unauthenticated
from tests.conftest import JwksServer, SigningKey, TokenFactory


# --------------------------------------------------------------------------- #
# Signature, audience, issuer, expiry
# --------------------------------------------------------------------------- #
async def test_valid_token_yields_claims(
    make_jwt: TokenFactory, jwks_cache: JwksCache, settings: Settings
) -> None:
    user_id = uuid4()
    token = make_jwt(sub=user_id, email="Someone@Example.com")

    claims = await verify_token(token, jwks=jwks_cache, settings=settings)

    assert claims.sub == user_id
    assert claims.email == "Someone@Example.com"
    assert claims.email_verified is True
    assert claims.raw["role"] == "authenticated"


async def test_expired_token_is_distinguishable(
    make_jwt: TokenFactory, jwks_cache: JwksCache, settings: Settings
) -> None:
    """Its own code, because the client's fix is 'refresh', not 'sign in again'."""
    token = make_jwt(expires_in_s=-3600)

    with pytest.raises(Unauthenticated) as caught:
        await verify_token(token, jwks=jwks_cache, settings=settings)

    assert caught.value.code == CODE_TOKEN_EXPIRED


async def test_token_signed_by_another_key_is_rejected(
    make_jwt: TokenFactory, jwks_cache: JwksCache, settings: Settings
) -> None:
    """Same ``kid``, different key: the forgery case the signature check exists for."""
    impostor = SigningKey(kid="test-signing-key-1")
    claims = jwt.get_unverified_claims(make_jwt())
    token = impostor.sign(claims)

    with pytest.raises(Unauthenticated) as caught:
        await verify_token(token, jwks=jwks_cache, settings=settings)

    assert caught.value.code == CODE_INVALID_TOKEN


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("aud", "some-other-audience"),
        ("iss", "https://evil.supabase.co/auth/v1"),
    ],
)
async def test_wrong_audience_or_issuer_is_rejected(
    make_jwt: TokenFactory, jwks_cache: JwksCache, settings: Settings, claim: str, value: str
) -> None:
    """A validly-signed token from another project is still not ours."""
    token = make_jwt(**{claim: value})

    with pytest.raises(Unauthenticated) as caught:
        await verify_token(token, jwks=jwks_cache, settings=settings)

    assert caught.value.code == CODE_INVALID_TOKEN


async def test_token_without_audience_is_rejected(
    make_jwt: TokenFactory, jwks_cache: JwksCache, settings: Settings
) -> None:
    """python-jose accepts a missing ``aud``; the gateway does not."""
    token = make_jwt(drop=("aud",))

    with pytest.raises(Unauthenticated) as caught:
        await verify_token(token, jwks=jwks_cache, settings=settings)

    assert caught.value.code == CODE_INVALID_TOKEN


async def test_hs256_token_is_refused_without_fetching_keys(
    jwks_cache: JwksCache, jwks_server: JwksServer, settings: Settings, signing_key: SigningKey
) -> None:
    """Algorithm confusion: a symmetric token signed with the *public* key bytes.

    The algorithm is checked before a key is looked up, so this never reaches
    the JWKS at all — asserted here, because a fetch would mean the header was
    trusted far enough to matter.
    """
    public_jwk = signing_key.public_jwk()
    token = jwt.encode(
        {"sub": str(uuid4()), "aud": settings.SUPABASE_JWT_AUDIENCE},
        public_jwk["x"],
        algorithm="HS256",
        headers={"kid": signing_key.kid},
    )

    with pytest.raises(Unauthenticated) as caught:
        await verify_token(token, jwks=jwks_cache, settings=settings)

    assert caught.value.code == CODE_INVALID_TOKEN
    assert jwks_server.calls == 0


async def test_garbage_is_rejected(jwks_cache: JwksCache, settings: Settings) -> None:
    with pytest.raises(Unauthenticated) as caught:
        await verify_token("not.a.jwt", jwks=jwks_cache, settings=settings)

    assert caught.value.code == CODE_INVALID_TOKEN


# --------------------------------------------------------------------------- #
# Email verification and anonymous sessions
# --------------------------------------------------------------------------- #
async def test_verified_email_passes(
    make_jwt: TokenFactory, jwks_cache: JwksCache, settings: Settings
) -> None:
    claims = await verify_token(make_jwt(email_verified=True), jwks=jwks_cache, settings=settings)
    assert claims.email_verified is True


async def test_unverified_email_is_refused_with_its_own_code(
    make_jwt: TokenFactory, jwks_cache: JwksCache, settings: Settings
) -> None:
    """The frontend shows "check your inbox" for this, not a login screen."""
    token = make_jwt(email_verified=False)

    with pytest.raises(Unauthenticated) as caught:
        await verify_token(token, jwks=jwks_cache, settings=settings)

    assert caught.value.code == CODE_EMAIL_NOT_VERIFIED


async def test_absent_claim_fails_closed_by_default(
    make_jwt: TokenFactory, jwks_cache: JwksCache, settings: Settings
) -> None:
    """A Supabase shape change must not silently let unconfirmed users through."""
    token = make_jwt(email_verified=None)

    with pytest.raises(Unauthenticated) as caught:
        await verify_token(token, jwks=jwks_cache, settings=settings)

    assert caught.value.code == CODE_EMAIL_NOT_VERIFIED


async def test_absent_claim_passes_when_the_requirement_is_switched_off(
    make_jwt: TokenFactory, jwks_cache: JwksCache, settings: Settings
) -> None:
    """The kill switch: an env var, not an emergency redeploy."""
    relaxed = settings.model_copy(update={"REQUIRE_VERIFIED_EMAIL": False})
    token = make_jwt(email_verified=None)

    claims = await verify_token(token, jwks=jwks_cache, settings=relaxed)

    assert claims.email_verified is False


async def test_top_level_claim_is_accepted_as_a_fallback(
    make_jwt: TokenFactory, jwks_cache: JwksCache, settings: Settings, signing_key: SigningKey
) -> None:
    """Some GoTrue versions emit it outside ``user_metadata``.

    Signed by hand because the factory puts the flag in ``user_metadata``, which
    is the path this test is specifically not exercising.
    """
    claims = jwt.get_unverified_claims(make_jwt(email_verified=None))
    claims["email_verified"] = True
    token = signing_key.sign(claims)

    verified = await verify_token(token, jwks=jwks_cache, settings=settings)

    assert verified.email_verified is True


async def test_anonymous_session_is_refused(
    make_jwt: TokenFactory, jwks_cache: JwksCache, settings: Settings
) -> None:
    """Anonymous sign-ins stay off by construction, not by a dashboard toggle."""
    token = make_jwt(is_anonymous=True)

    with pytest.raises(Unauthenticated) as caught:
        await verify_token(token, jwks=jwks_cache, settings=settings)

    assert caught.value.code == CODE_INVALID_TOKEN


# --------------------------------------------------------------------------- #
# JWKS caching
# --------------------------------------------------------------------------- #
async def test_keys_are_fetched_once_and_reused(
    make_jwt: TokenFactory, jwks_cache: JwksCache, jwks_server: JwksServer, settings: Settings
) -> None:
    await verify_token(make_jwt(), jwks=jwks_cache, settings=settings)
    await verify_token(make_jwt(), jwks=jwks_cache, settings=settings)

    assert jwks_server.calls == 1


async def test_unknown_kid_refreshes_once_then_is_throttled(
    make_jwt: TokenFactory,
    jwks_cache: JwksCache,
    jwks_server: JwksServer,
    settings: Settings,
    frozen_clock: FixedClock,
) -> None:
    """A rotated key deserves a refresh. A storm of made-up ones does not.

    Without the floor, anyone can turn each request they send into a request we
    send to Supabase.
    """
    for _ in range(5):
        with pytest.raises(Unauthenticated):
            await verify_token(make_jwt(kid="rotated-away"), jwks=jwks_cache, settings=settings)

    assert jwks_server.calls == 1

    # Past the floor, a genuine rotation is picked up.
    frozen_clock.advance(61)
    with pytest.raises(Unauthenticated):
        await verify_token(make_jwt(kid="rotated-away"), jwks=jwks_cache, settings=settings)

    assert jwks_server.calls == 2


async def test_expired_cache_is_refetched(
    make_jwt: TokenFactory,
    jwks_cache: JwksCache,
    jwks_server: JwksServer,
    settings: Settings,
    frozen_clock: FixedClock,
) -> None:
    await verify_token(make_jwt(), jwks=jwks_cache, settings=settings)
    frozen_clock.advance(13 * 60 * 60)
    await verify_token(make_jwt(), jwks=jwks_cache, settings=settings)

    assert jwks_server.calls == 2


async def test_unreachable_jwks_on_a_cold_cache_is_a_503(
    make_jwt: TokenFactory, jwks_cache: JwksCache, jwks_server: JwksServer, settings: Settings
) -> None:
    """Our outage, not the caller's bad request — so not a 401."""
    jwks_server.status_code = 503

    with pytest.raises(ServiceUnavailable):
        await verify_token(make_jwt(), jwks=jwks_cache, settings=settings)


async def test_stale_keys_survive_a_failed_refresh(
    make_jwt: TokenFactory,
    jwks_cache: JwksCache,
    jwks_server: JwksServer,
    settings: Settings,
    frozen_clock: FixedClock,
) -> None:
    """Supabase having a bad minute should not log everyone out."""
    await verify_token(make_jwt(), jwks=jwks_cache, settings=settings)

    jwks_server.status_code = 500
    frozen_clock.advance(13 * 60 * 60)  # cache is now stale, refresh will fail

    claims = await verify_token(make_jwt(), jwks=jwks_cache, settings=settings)

    assert claims.email == "user@example.com"
    assert jwks_server.calls == 2
