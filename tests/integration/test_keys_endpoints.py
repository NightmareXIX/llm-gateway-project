"""``/v1/keys`` — issuance, listing, revocation, and who is allowed to do them."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest

from app.auth.api_keys import KEY_PREFIX
from tests.conftest import TokenFactory

pytestmark = pytest.mark.integration


def _session_headers(make_jwt: TokenFactory, **kwargs: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_jwt(**kwargs)}"}


async def test_create_returns_the_plaintext_exactly_once(
    client: httpx.AsyncClient, make_jwt: TokenFactory
) -> None:
    headers = _session_headers(make_jwt)

    created = await client.post("/v1/keys", json={"nickname": "laptop"}, headers=headers)

    assert created.status_code == 201
    body = created.json()
    plaintext = body["key"]
    assert plaintext.startswith(KEY_PREFIX)
    assert body["nickname"] == "laptop"
    assert body["is_active"] is True
    assert body["masked"].endswith(plaintext[-4:])

    # It is never returned again — the list carries the masked form only.
    listed = await client.get("/v1/keys", headers=headers)
    assert plaintext not in listed.text
    assert "key" not in listed.json()[0]


async def test_a_new_key_authenticates(client: httpx.AsyncClient, make_jwt: TokenFactory) -> None:
    """The round trip that makes the API-key half of D7 usable by hand."""
    user_id = uuid4()
    headers = _session_headers(make_jwt, sub=user_id)

    created = await client.post("/v1/keys", json={}, headers=headers)
    plaintext = created.json()["key"]

    response = await client.get("/v1/me", headers={"X-API-Key": plaintext})

    assert response.status_code == 200
    assert response.json()["user_id"] == str(user_id)
    assert response.json()["auth_method"] == "api_key"


async def test_list_shows_only_your_own_keys(
    client: httpx.AsyncClient, make_jwt: TokenFactory
) -> None:
    alice = _session_headers(make_jwt, sub=uuid4(), email="alice@example.com")
    bob = _session_headers(make_jwt, sub=uuid4(), email="bob@example.com")

    await client.post("/v1/keys", json={"nickname": "alice-key"}, headers=alice)
    await client.post("/v1/keys", json={"nickname": "bob-key"}, headers=bob)

    listed = await client.get("/v1/keys", headers=alice)

    assert [key["nickname"] for key in listed.json()] == ["alice-key"]


async def test_revoke_kills_the_key(client: httpx.AsyncClient, make_jwt: TokenFactory) -> None:
    headers = _session_headers(make_jwt)
    created = await client.post("/v1/keys", json={}, headers=headers)
    plaintext = created.json()["key"]
    key_id = created.json()["id"]

    assert (await client.get("/v1/me", headers={"X-API-Key": plaintext})).status_code == 200

    revoked = await client.delete(f"/v1/keys/{key_id}", headers=headers)
    assert revoked.status_code == 204

    after = await client.get("/v1/me", headers={"X-API-Key": plaintext})
    assert after.status_code == 401
    assert after.json()["error"]["code"] == "invalid_api_key"


async def test_revoked_keys_stay_listed(client: httpx.AsyncClient, make_jwt: TokenFactory) -> None:
    """ "I revoked that one last week" is what the list is opened to confirm."""
    headers = _session_headers(make_jwt)
    created = await client.post("/v1/keys", json={"nickname": "old"}, headers=headers)
    await client.delete(f"/v1/keys/{created.json()['id']}", headers=headers)

    listed = (await client.get("/v1/keys", headers=headers)).json()

    assert len(listed) == 1
    assert listed[0]["is_active"] is False


async def test_revoking_someone_elses_key_is_a_404(
    client: httpx.AsyncClient, make_jwt: TokenFactory
) -> None:
    """404, not 403 — a 403 would confirm the id names a real key."""
    alice = _session_headers(make_jwt, sub=uuid4(), email="alice@example.com")
    bob = _session_headers(make_jwt, sub=uuid4(), email="bob@example.com")

    created = await client.post("/v1/keys", json={}, headers=alice)
    alice_key_id = created.json()["id"]

    response = await client.delete(f"/v1/keys/{alice_key_id}", headers=bob)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "api_key_not_found"

    # And Alice's key still works.
    still_valid = await client.get("/v1/me", headers={"X-API-Key": created.json()["key"]})
    assert still_valid.status_code == 200


async def test_revoking_an_unknown_id_is_a_404(
    client: httpx.AsyncClient, make_jwt: TokenFactory
) -> None:
    response = await client.delete(f"/v1/keys/{uuid4()}", headers=_session_headers(make_jwt))
    assert response.status_code == 404


@pytest.mark.parametrize("method,path", [("post", "/v1/keys"), ("get", "/v1/keys")])
async def test_an_api_key_cannot_manage_keys(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    user_factory: Callable[..., Any],
    api_key_factory: Callable[..., Any],
    method: str,
    path: str,
) -> None:
    """A leaked key must not be able to issue its own successors, or enumerate.

    Otherwise revoking the compromised key achieves nothing — the attacker
    minted a replacement the moment they got in.
    """
    user = await user_factory()
    plaintext, _ = await api_key_factory(user=user)

    response = await client.request(method, path, headers={"X-API-Key": plaintext}, json={})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "session_required"


async def test_an_api_key_cannot_revoke(
    client: httpx.AsyncClient,
    user_factory: Callable[..., Any],
    api_key_factory: Callable[..., Any],
) -> None:
    user = await user_factory()
    plaintext, api_key = await api_key_factory(user=user)

    response = await client.delete(f"/v1/keys/{api_key.id}", headers={"X-API-Key": plaintext})

    assert response.status_code == 403


async def test_key_management_requires_credentials(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/keys")).status_code == 401
    assert (await client.post("/v1/keys", json={})).status_code == 401


async def test_nickname_is_validated(client: httpx.AsyncClient, make_jwt: TokenFactory) -> None:
    response = await client.post(
        "/v1/keys", json={"nickname": "x" * 65}, headers=_session_headers(make_jwt)
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
