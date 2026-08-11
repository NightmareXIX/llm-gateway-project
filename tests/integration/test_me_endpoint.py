"""``GET /v1/me`` as a resource, rather than as a way to exercise auth.

[test_auth_dependency.py](tests/integration/test_auth_dependency.py) already
drives every credential path through this route — that is the endpoint's other
job, and none of it is repeated here. What is left is what the route itself
decides, and all of it is a contract the frontend reads on every page load:

**The body is exactly ``MeResponse``.** ``extra="forbid"`` on the way in does not
stop a field leaking on the way *out*; the response model does, and the shape
assertion below is what notices if someone widens it. The ``users`` row carries
things the client has no business seeing, and one careless ``**user.__dict__``
is all it takes.

**Identity is read from the row, not carried on the ``Principal``.** The
principal is four frozen fields (§1.2). ``email`` and ``email_verified`` are
display concerns, and keeping them off it is what stops it drifting into a
general-purpose user object that every layer reads a different field from.

**A missing row is a 404, not a 500.** A principal always implies a row, so
reaching that branch means the account was deleted between authentication and
the read. Rare, and it still has to produce the envelope. It is unreachable
through a real credential — the JWT path upserts the row and the API key path
joins to it — so it is reached here by overriding ``get_principal``, which is
also the honest description of the situation the branch guards against.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.auth.dependency import get_principal
from app.auth.principal import Principal
from tests.conftest import TokenFactory, assert_envelope

pytestmark = pytest.mark.integration

ME = "/v1/me"

EXPECTED_FIELDS = {"user_id", "email", "email_verified", "tier", "auth_method", "api_key_id"}


async def test_the_body_is_exactly_the_documented_shape(
    client: httpx.AsyncClient, make_jwt: TokenFactory
) -> None:
    """No more fields than the response model names. An extra key here is a leak
    from the ``users`` row, and it would ship silently."""
    response = await client.get(ME, headers={"Authorization": f"Bearer {make_jwt()}"})

    assert response.status_code == 200
    assert set(response.json()) == EXPECTED_FIELDS


async def test_identity_comes_from_the_row_not_from_the_token(
    client: httpx.AsyncClient, user_factory: Callable[..., Any], api_key_factory: Callable[..., Any]
) -> None:
    """An API key carries no email at all, and the response still has one — which
    is only true because the endpoint reads ``users`` rather than the credential."""
    user = await user_factory(email="reader@example.com", tier="plus")
    plaintext, api_key = await api_key_factory(user=user)

    response = await client.get(ME, headers={"X-API-Key": plaintext})

    body = response.json()
    assert body["email"] == "reader@example.com"
    assert body["email_verified"] is True
    assert body["tier"] == "plus"
    assert body["api_key_id"] == str(api_key.id)


async def test_the_tier_is_the_stored_one_not_a_default(
    client: httpx.AsyncClient, user_factory: Callable[..., Any], make_jwt: TokenFactory
) -> None:
    """``tier`` drives what the UI renders and, from Phase 3, the user's quota
    allocation. Supabase does not know about it, so a JWT login must not flatten
    it back to ``free``."""
    user = await user_factory(email="paid@example.com", tier="plus")
    token = make_jwt(sub=user.id, email="paid@example.com")

    response = await client.get(ME, headers={"Authorization": f"Bearer {token}"})

    assert response.json()["tier"] == "plus"


async def test_an_unverified_mirror_is_reported_rather_than_hidden(
    client: httpx.AsyncClient, user_factory: Callable[..., Any], api_key_factory: Callable[..., Any]
) -> None:
    """The frontend shows a banner off this field. Defaulting it to ``true`` would
    make the banner impossible to trigger and the bug impossible to see."""
    user = await user_factory(email_verified=False)
    plaintext, _ = await api_key_factory(user=user)

    response = await client.get(ME, headers={"X-API-Key": plaintext})

    assert response.status_code == 200
    assert response.json()["email_verified"] is False


async def test_a_deleted_account_is_a_404_in_the_envelope(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    """The defensive branch: authenticated, but the row is gone. A 500 here would
    tell the user nothing and would page whoever owns the error rate."""

    async def _ghost() -> Principal:
        return Principal(user_id=uuid4(), auth_method="session", api_key_id=None, tier="free")

    app.dependency_overrides[get_principal] = _ghost

    response = await client.get(ME)

    assert response.status_code == 404
    error = assert_envelope(response.json())
    assert error["code"] == "user_not_found"
    assert error["request_id"]
