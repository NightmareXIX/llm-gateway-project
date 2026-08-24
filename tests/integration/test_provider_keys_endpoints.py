"""``/v1/provider-keys`` — §9.2's add flow, §9.8's rate limit, and who is
allowed to touch any of it.

Nothing downstream reads a stored row yet (Phase 6 Step 4 is the resolver), so
every assertion here is about the table and the wire, never about which
credential actually answered a chat turn.

**A scripted registry, not the network.** ``groq_key_transport`` replaces the
app's provider registry with one built over an ``httpx.MockTransport`` serving
one of Groq's recorded ``validate_key`` fixtures — ``models_list`` (200, a real
key), ``auth_failed`` (401, a bad key) or ``server_error_html`` (502, a proxy
answering instead of Groq). Every other provider is disabled for the fleet
this installs, mirroring ``test_chat_endpoint.py``'s ``_groq_only``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ProvidersConfig, get_providers_config
from app.db.repo import provider_keys as provider_keys_repo
from app.providers.registry import build_registry
from tests import provider_fixtures
from tests.conftest import TokenFactory

pytestmark = pytest.mark.integration

PROVIDER_KEYS = "/v1/provider-keys"


def _groq_only() -> ProvidersConfig:
    """The committed slot table with Gemini and OpenRouter switched off.

    Copied from ``test_chat_endpoint.py`` rather than imported: a one-test-file
    helper duplicated once is cheaper than a cross-module dependency between
    two otherwise-unrelated test suites.
    """
    config = get_providers_config()
    providers = {
        name: entry if name == "groq" else entry.model_copy(update={"enabled": False})
        for name, entry in config.providers.items()
    }
    return config.model_copy(update={"providers": providers})


@pytest.fixture
async def groq_key_transport(app: FastAPI) -> AsyncIterator[Callable[[str], None]]:
    """Install a Groq-only registry answering ``validate_key`` from one named
    fixture. Call again mid-test to change what the next call sees."""
    installed: list[httpx.AsyncClient] = []

    def install(fixture_name: str) -> None:
        client = provider_fixtures.client_returning(provider_fixtures.load("groq", fixture_name))
        installed.append(client)
        app.state.provider_registry = build_registry(client=client, config=_groq_only())

    try:
        yield install
    finally:
        for client in installed:
            await client.aclose()


def _session_headers(make_jwt: TokenFactory, **kwargs: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_jwt(**kwargs)}"}


# --------------------------------------------------------------------------- #
# POST — the add flow (§9.2)
# --------------------------------------------------------------------------- #
async def test_a_valid_key_is_stored_and_the_row_flips_to_private(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_key_transport: Callable[[str], None],
    db_session: AsyncSession,
) -> None:
    groq_key_transport("models_list")
    user_id = uuid4()
    headers = _session_headers(make_jwt, sub=user_id)

    response = await client.post(
        PROVIDER_KEYS,
        json={"provider": "groq", "key": "gsk_real_looking_key", "nickname": "my key"},
        headers=headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["provider"] == "groq"
    assert body["pool"] == "private"
    assert body["key"]["nickname"] == "my key"
    assert body["key"]["validation_status"] == "valid"
    assert body["key"]["masked"].endswith("_key")
    assert body["key"]["is_active"] is True

    stored = await provider_keys_repo.get_active(db_session, user_id=user_id, provider="groq")
    assert stored is not None
    assert stored.last_4 == "_key"


async def test_a_rejected_key_stores_nothing(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_key_transport: Callable[[str], None],
    db_session: AsyncSession,
) -> None:
    groq_key_transport("auth_failed")
    user_id = uuid4()
    headers = _session_headers(make_jwt, sub=user_id)

    response = await client.post(
        PROVIDER_KEYS, json={"provider": "groq", "key": "not-a-real-key"}, headers=headers
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_provider_key"

    # The exit criterion, as an assertion: nothing was stored.
    assert await provider_keys_repo.list_active_for_user(db_session, user_id) == []
    listed = await client.get(PROVIDER_KEYS, headers=headers)
    groq_row = next(row for row in listed.json() if row["provider"] == "groq")
    assert groq_row["pool"] == "shared"
    assert groq_row["key"] is None


async def test_an_unreachable_provider_is_503_distinct_from_a_bad_key(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_key_transport: Callable[[str], None],
    db_session: AsyncSession,
) -> None:
    groq_key_transport("server_error_html")
    user_id = uuid4()
    headers = _session_headers(make_jwt, sub=user_id)

    response = await client.post(
        PROVIDER_KEYS, json={"provider": "groq", "key": "sk_whatever"}, headers=headers
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_unavailable"
    assert await provider_keys_repo.list_active_for_user(db_session, user_id) == []


async def test_adding_a_second_key_replaces_the_first(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_key_transport: Callable[[str], None],
    db_session: AsyncSession,
) -> None:
    groq_key_transport("models_list")
    user_id = uuid4()
    headers = _session_headers(make_jwt, sub=user_id)

    await client.post(
        PROVIDER_KEYS, json={"provider": "groq", "key": "sk_first_1111"}, headers=headers
    )
    second = await client.post(
        PROVIDER_KEYS, json={"provider": "groq", "key": "sk_second_2222"}, headers=headers
    )

    assert second.status_code == 201
    rows = await provider_keys_repo.list_for_user(db_session, user_id)
    assert len(rows) == 2
    active = [row for row in rows if row.is_active]
    assert len(active) == 1
    assert active[0].last_4 == "2222"


async def test_an_unknown_provider_is_a_400_before_any_validation(
    client: httpx.AsyncClient, make_jwt: TokenFactory
) -> None:
    response = await client.post(
        PROVIDER_KEYS,
        json={"provider": "not-a-real-provider", "key": "whatever"},
        headers=_session_headers(make_jwt),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_provider"


# --------------------------------------------------------------------------- #
# GET — the settings page's own list
# --------------------------------------------------------------------------- #
async def test_list_shows_every_enabled_provider(
    client: httpx.AsyncClient, make_jwt: TokenFactory
) -> None:
    response = await client.get(PROVIDER_KEYS, headers=_session_headers(make_jwt))

    assert response.status_code == 200
    body = response.json()
    assert {row["provider"] for row in body} == {"groq", "gemini", "openrouter"}
    assert all(row["pool"] == "shared" and row["key"] is None for row in body)


async def test_list_shows_only_your_own_key(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_key_transport: Callable[[str], None],
) -> None:
    groq_key_transport("models_list")
    alice = _session_headers(make_jwt, sub=uuid4(), email="alice@example.com")
    bob = _session_headers(make_jwt, sub=uuid4(), email="bob@example.com")

    await client.post(PROVIDER_KEYS, json={"provider": "groq", "key": "sk_alice"}, headers=alice)

    alice_list = (await client.get(PROVIDER_KEYS, headers=alice)).json()
    alice_row = next(row for row in alice_list if row["provider"] == "groq")
    assert alice_row["pool"] == "private"
    assert alice_row["key"] is not None

    bob_list = (await client.get(PROVIDER_KEYS, headers=bob)).json()
    groq_row = next(row for row in bob_list if row["provider"] == "groq")
    assert groq_row["pool"] == "shared"


# --------------------------------------------------------------------------- #
# DELETE
# --------------------------------------------------------------------------- #
async def test_deleting_an_absent_provider_is_404(
    client: httpx.AsyncClient, make_jwt: TokenFactory
) -> None:
    response = await client.delete(f"{PROVIDER_KEYS}/groq", headers=_session_headers(make_jwt))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "provider_key_not_found"


async def test_deleting_a_stored_key_drops_back_to_shared(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_key_transport: Callable[[str], None],
) -> None:
    groq_key_transport("models_list")
    headers = _session_headers(make_jwt)
    await client.post(PROVIDER_KEYS, json={"provider": "groq", "key": "sk_key"}, headers=headers)

    deleted = await client.delete(f"{PROVIDER_KEYS}/groq", headers=headers)
    assert deleted.status_code == 204

    listed = (await client.get(PROVIDER_KEYS, headers=headers)).json()
    groq_row = next(row for row in listed if row["provider"] == "groq")
    assert groq_row["pool"] == "shared"
    assert groq_row["key"] is None


# --------------------------------------------------------------------------- #
# POST /{provider}/validate — the "check again" button
# --------------------------------------------------------------------------- #
async def test_revalidate_updates_status_and_timestamp_on_success(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_key_transport: Callable[[str], None],
) -> None:
    groq_key_transport("models_list")
    headers = _session_headers(make_jwt)
    await client.post(PROVIDER_KEYS, json={"provider": "groq", "key": "sk_key"}, headers=headers)

    response = await client.post(f"{PROVIDER_KEYS}/groq/validate", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["key"]["validation_status"] == "valid"
    assert body["key"]["last_validated_at"] is not None


async def test_revalidate_flips_status_to_invalid_on_a_rejected_key(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_key_transport: Callable[[str], None],
) -> None:
    groq_key_transport("models_list")
    headers = _session_headers(make_jwt)
    await client.post(PROVIDER_KEYS, json={"provider": "groq", "key": "sk_key"}, headers=headers)

    groq_key_transport("auth_failed")
    response = await client.post(f"{PROVIDER_KEYS}/groq/validate", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["key"]["validation_status"] == "invalid"
    assert body["key"]["last_validated_at"] is not None
    # Still active — a rejected key is disclosed, not silently removed.
    assert body["key"]["is_active"] is True


async def test_revalidating_an_absent_provider_is_404(
    client: httpx.AsyncClient, make_jwt: TokenFactory
) -> None:
    response = await client.post(
        f"{PROVIDER_KEYS}/groq/validate", headers=_session_headers(make_jwt)
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# D43 — the validation rate limit
# --------------------------------------------------------------------------- #
async def test_the_sixth_validation_call_in_an_hour_is_429(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_key_transport: Callable[[str], None],
) -> None:
    groq_key_transport("auth_failed")
    headers = _session_headers(make_jwt)

    for _ in range(5):
        response = await client.post(
            PROVIDER_KEYS, json={"provider": "groq", "key": "sk_key"}, headers=headers
        )
        assert response.status_code == 422  # every attempt is a bad key, on purpose

    sixth = await client.post(
        PROVIDER_KEYS, json={"provider": "groq", "key": "sk_key"}, headers=headers
    )
    assert sixth.status_code == 429
    assert "Retry-After" in sixth.headers


# --------------------------------------------------------------------------- #
# Who is allowed to do any of this
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "method,path",
    [("post", PROVIDER_KEYS), ("get", PROVIDER_KEYS), ("delete", f"{PROVIDER_KEYS}/groq")],
)
async def test_an_api_key_cannot_manage_provider_keys(
    client: httpx.AsyncClient,
    user_factory: Callable[..., Any],
    api_key_factory: Callable[..., Any],
    method: str,
    path: str,
) -> None:
    user = await user_factory()
    plaintext, _ = await api_key_factory(user=user)

    response = await client.request(
        method, path, headers={"X-API-Key": plaintext}, json={"provider": "groq", "key": "x"}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "session_required"


async def test_provider_key_management_requires_credentials(client: httpx.AsyncClient) -> None:
    assert (await client.get(PROVIDER_KEYS)).status_code == 401
    assert (
        await client.post(PROVIDER_KEYS, json={"provider": "groq", "key": "x"})
    ).status_code == 401


# --------------------------------------------------------------------------- #
# The plaintext and the ciphertext never leave the process
# --------------------------------------------------------------------------- #
async def test_no_route_in_this_module_ever_echoes_the_key(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_key_transport: Callable[[str], None],
    db_session: AsyncSession,
) -> None:
    groq_key_transport("models_list")
    sentinel = "sk_super_secret_sentinel_value_9f8e7d"
    user_id = uuid4()
    headers = _session_headers(make_jwt, sub=user_id)

    added = await client.post(
        PROVIDER_KEYS, json={"provider": "groq", "key": sentinel}, headers=headers
    )
    listed = await client.get(PROVIDER_KEYS, headers=headers)
    revalidated = await client.post(f"{PROVIDER_KEYS}/groq/validate", headers=headers)
    removed = await client.delete(f"{PROVIDER_KEYS}/groq", headers=headers)

    row = await provider_keys_repo.list_for_user(db_session, user_id)
    ciphertext = row[0].encrypted_key
    assert ciphertext != sentinel

    for response in (added, listed, revalidated, removed):
        text = response.text
        assert sentinel not in text
        assert ciphertext not in text
