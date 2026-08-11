"""``get_principal`` end to end: both credential paths, against a real database.

The five cases Step 3 names — valid JWT, expired, bad signature, valid key,
revoked key — plus the user-mirroring behaviour that only a real ``ON CONFLICT``
can demonstrate.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest
from jose import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import dependency
from app.config import Settings
from app.core.logging import REQUEST_ID_HEADER
from app.db.models import User
from tests.conftest import SigningKey, TokenFactory, assert_envelope

pytestmark = pytest.mark.integration


async def _fetch_user(session: AsyncSession, user_id: Any) -> User | None:
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


# --------------------------------------------------------------------------- #
# The session path
# --------------------------------------------------------------------------- #
async def test_valid_jwt_authenticates(client: httpx.AsyncClient, make_jwt: TokenFactory) -> None:
    user_id = uuid4()
    token = make_jwt(sub=user_id, email="new@example.com")

    response = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == str(user_id)
    assert body["email"] == "new@example.com"
    assert body["auth_method"] == "session"
    assert body["api_key_id"] is None
    assert body["tier"] == "free"


async def test_first_request_creates_the_local_user_row(
    client: httpx.AsyncClient, make_jwt: TokenFactory, db_session: AsyncSession
) -> None:
    """Upsert-on-login: Supabase owns identity, we keep a mirror for the FKs."""
    user_id = uuid4()

    await client.get("/v1/me", headers={"Authorization": f"Bearer {make_jwt(sub=user_id)}"})

    user = await _fetch_user(db_session, user_id)
    assert user is not None
    assert user.email_verified is True
    assert user.tier == "free"


async def test_repeat_requests_are_idempotent(
    client: httpx.AsyncClient, make_jwt: TokenFactory, db_session: AsyncSession
) -> None:
    user_id = uuid4()
    headers = {"Authorization": f"Bearer {make_jwt(sub=user_id, email='a@example.com')}"}

    for _ in range(3):
        assert (await client.get("/v1/me", headers=headers)).status_code == 200

    result = await db_session.execute(select(User).where(User.id == user_id))
    assert len(result.scalars().all()) == 1


async def test_email_is_lower_cased_on_write(
    client: httpx.AsyncClient, make_jwt: TokenFactory, db_session: AsyncSession
) -> None:
    """The unique index is only real if every write agrees on casing."""
    user_id = uuid4()
    token = make_jwt(sub=user_id, email="Mixed.Case@Example.COM")

    await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})

    user = await _fetch_user(db_session, user_id)
    assert user is not None
    assert user.email == "mixed.case@example.com"


async def test_verification_state_is_refreshed_on_every_upsert(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    db_session: AsyncSession,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user who confirms later stops looking stale on their next request.

    The unverified leg needs the requirement relaxed — otherwise the request is
    refused before it ever reaches the upsert, which is the point of the check
    but not what this test is about.
    """
    user_id = uuid4()
    relaxed = settings.model_copy(update={"REQUIRE_VERIFIED_EMAIL": False})

    with monkeypatch.context() as patched:
        patched.setattr(dependency, "get_settings", lambda: relaxed)

        unverified = make_jwt(sub=user_id, email="later@example.com", email_verified=False)
        await client.get("/v1/me", headers={"Authorization": f"Bearer {unverified}"})

        user = await _fetch_user(db_session, user_id)
        assert user is not None
        assert user.email_verified is False

    # They click the link; the very next request corrects the mirror.
    verified = make_jwt(sub=user_id, email="later@example.com", email_verified=True)
    response = await client.get("/v1/me", headers={"Authorization": f"Bearer {verified}"})

    assert response.status_code == 200
    assert response.json()["email_verified"] is True

    await db_session.refresh(user)
    assert user.email_verified is True


async def test_expired_jwt_is_a_401(client: httpx.AsyncClient, make_jwt: TokenFactory) -> None:
    token = make_jwt(expires_in_s=-60)

    response = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "token_expired"


async def test_forged_signature_is_a_401(client: httpx.AsyncClient, make_jwt: TokenFactory) -> None:
    impostor = SigningKey()
    token = impostor.sign(jwt.get_unverified_claims(make_jwt()))

    response = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


async def test_unverified_email_is_refused_with_a_distinct_code(
    client: httpx.AsyncClient, make_jwt: TokenFactory
) -> None:
    token = make_jwt(email_verified=False)

    response = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "email_not_verified"


# --------------------------------------------------------------------------- #
# The API key path
# --------------------------------------------------------------------------- #
async def test_valid_api_key_authenticates(
    client: httpx.AsyncClient,
    user_factory: Callable[..., Any],
    api_key_factory: Callable[..., Any],
) -> None:
    user = await user_factory(email="owner@example.com")
    plaintext, api_key = await api_key_factory(user=user)

    response = await client.get("/v1/me", headers={"X-API-Key": plaintext})

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == str(user.id)
    assert body["auth_method"] == "api_key"
    assert body["api_key_id"] == str(api_key.id)


async def test_revoked_api_key_is_a_401(
    client: httpx.AsyncClient,
    user_factory: Callable[..., Any],
    api_key_factory: Callable[..., Any],
) -> None:
    user = await user_factory()
    plaintext, _ = await api_key_factory(user=user, is_active=False)

    response = await client.get("/v1/me", headers={"X-API-Key": plaintext})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


async def test_unknown_and_malformed_keys_are_indistinguishable(
    client: httpx.AsyncClient,
) -> None:
    """Neither response tells the caller whether the key format was even right."""
    unknown = await client.get("/v1/me", headers={"X-API-Key": "gw_live_" + "a" * 32})
    malformed = await client.get("/v1/me", headers={"X-API-Key": "obviously-not-a-key"})

    assert unknown.status_code == malformed.status_code == 401
    assert unknown.json()["error"]["code"] == malformed.json()["error"]["code"] == "invalid_api_key"
    assert unknown.json()["error"]["message"] == malformed.json()["error"]["message"]


async def test_api_key_usage_is_recorded(
    client: httpx.AsyncClient,
    user_factory: Callable[..., Any],
    api_key_factory: Callable[..., Any],
    db_session: AsyncSession,
) -> None:
    user = await user_factory()
    plaintext, api_key = await api_key_factory(user=user)
    assert api_key.last_used_at is None

    await client.get("/v1/me", headers={"X-API-Key": plaintext})

    await db_session.refresh(api_key)
    assert api_key.last_used_at is not None


# --------------------------------------------------------------------------- #
# No credentials, and the precedence rule
# --------------------------------------------------------------------------- #
async def test_no_credentials_is_a_401_in_the_envelope(client: httpx.AsyncClient) -> None:
    """Also the envelope's only test against the fully wired app.

    ``tests/unit/test_errors.py`` proves the handlers in isolation, on an app it
    builds itself. This proves the real one — real routers, real middleware
    order, real auth dependency — produces the same four fields, so a mistake in
    ``create_app`` cannot pass unnoticed.
    """
    response = await client.get("/v1/me")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    error = assert_envelope(response.json())
    assert error["code"] == "missing_credentials"
    assert error["request_id"] == response.headers[REQUEST_ID_HEADER]


async def test_a_broken_bearer_header_does_not_fall_through_to_the_api_key(
    client: httpx.AsyncClient,
    user_factory: Callable[..., Any],
    api_key_factory: Callable[..., Any],
) -> None:
    """The silent-identity-swap case the ordering rule exists to prevent.

    A client whose session broke while a stale key header lingers must be told,
    not quietly re-authenticated as a different principal.
    """
    user = await user_factory()
    plaintext, _ = await api_key_factory(user=user)

    response = await client.get(
        "/v1/me",
        headers={"Authorization": "Basic bm90LWEtdG9rZW4=", "X-API-Key": plaintext},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_authorization_header"


async def test_a_valid_bearer_wins_over_a_present_api_key(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    user_factory: Callable[..., Any],
    api_key_factory: Callable[..., Any],
) -> None:
    key_owner = await user_factory()
    plaintext, _ = await api_key_factory(user=key_owner)
    session_user_id = uuid4()

    response = await client.get(
        "/v1/me",
        headers={
            "Authorization": f"Bearer {make_jwt(sub=session_user_id)}",
            "X-API-Key": plaintext,
        },
    )

    assert response.status_code == 200
    assert response.json()["user_id"] == str(session_user_id)
    assert response.json()["auth_method"] == "session"
