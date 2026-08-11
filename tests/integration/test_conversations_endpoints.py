"""``/v1/conversations`` — the history endpoints the UI needs immediately.

Every route is a thin call onto an ownership-scoped repository, so what is worth
asserting here is not the SQL — ``test_repo_conversations.py`` covers that — but
the HTTP contract on top of it: canonical blocks survive the trip, and a
conversation belonging to someone else is indistinguishable from one that does
not exist.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repo import messages as messages_repo
from app.memory.canonical import MessageMeta, text_block
from app.providers.registry import build_registry
from tests import provider_fixtures
from tests.conftest import TokenFactory

pytestmark = pytest.mark.integration

CONVERSATIONS = "/v1/conversations"


@pytest.fixture
async def groq(app: FastAPI) -> AsyncIterator[provider_fixtures.RecordingHandler]:
    """A registry that answers from a fixture, for the tests that send a turn."""
    handler = provider_fixtures.RecordingHandler(provider_fixtures.load("groq", "success"))
    client = handler.client()
    app.state.provider_registry = build_registry(client=client)
    try:
        yield handler
    finally:
        await client.aclose()


def _headers(make_jwt: TokenFactory, **kwargs: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_jwt(**kwargs)}"}


async def test_listing_is_scoped_and_ordered_by_activity(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
) -> None:
    alice = _headers(make_jwt, sub=uuid4(), email="alice@example.com")
    bob = _headers(make_jwt, sub=uuid4(), email="bob@example.com")

    first = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "older"}]},
        headers=alice,
    )
    second = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "newer"}]},
        headers=alice,
    )
    await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "bob's"}]},
        headers=bob,
    )

    listed = await client.get(CONVERSATIONS, headers=alice)

    assert listed.status_code == 200
    ids = [row["id"] for row in listed.json()]
    assert ids == [second.json()["conversation_id"], first.json()["conversation_id"]]
    assert listed.json()[0]["title"] is None
    assert listed.json()[0]["preferred_slot"] == "auto"
    assert listed.json()[0]["pinned_model"] is None


async def test_reading_returns_the_canonical_history(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
) -> None:
    """Blocks and ``meta`` cross unchanged — the model indicator reads the latter."""
    headers = _headers(make_jwt)
    created = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "what is a gateway?"}]},
        headers=headers,
    )
    conversation_id = created.json()["conversation_id"]

    detail = await client.get(f"{CONVERSATIONS}/{conversation_id}", headers=headers)

    assert detail.status_code == 200
    body = detail.json()
    assert body["id"] == conversation_id

    user, assistant = body["messages"]
    assert user["seq"] == 0
    assert user["role"] == "user"
    assert user["content"] == [{"type": "text", "text": "what is a gateway?"}]
    assert user["meta"]["provider_used"] is None

    assert assistant["seq"] == 1
    assert assistant["role"] == "assistant"
    assert assistant["meta"]["provider_used"] == "groq"
    assert assistant["meta"]["model_used"] == "llama-3.3-70b-versatile"
    assert assistant["meta"]["slot_used"] == "general"
    assert assistant["meta"]["substituted"] is False
    assert assistant["meta"]["tokens_out"] == 27


async def test_an_omission_marker_survives_the_trip(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    db_session: AsyncSession,
    user_factory: Callable[..., Any],
    conversation_factory: Callable[..., Any],
) -> None:
    """D4's visible scar is something the transcript must be able to render."""
    user = await user_factory()
    conversation = await conversation_factory(user=user)
    await messages_repo.append(
        db_session,
        conversation_id=conversation.id,
        user_id=user.id,
        role="user",
        content=[
            {"type": "omission_marker", "omitted_count": 4, "reason": "context_truncation"},
            text_block("recap?"),
        ],
    )
    await messages_repo.append(
        db_session,
        conversation_id=conversation.id,
        user_id=user.id,
        role="assistant",
        content=[text_block("here you go")],
        meta=MessageMeta(provider_used="groq", model_used="llama-3.3-70b-versatile"),
    )
    await db_session.commit()

    detail = await client.get(
        f"{CONVERSATIONS}/{conversation.id}",
        headers=_headers(make_jwt, sub=user.id, email=user.email),
    )

    blocks = detail.json()["messages"][0]["content"]
    assert blocks[0] == {
        "type": "omission_marker",
        "omitted_count": 4,
        "reason": "context_truncation",
    }
    assert blocks[1] == {"type": "text", "text": "recap?"}


async def test_a_new_thread_reads_as_empty_not_missing(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    user_factory: Callable[..., Any],
    conversation_factory: Callable[..., Any],
) -> None:
    user = await user_factory()
    conversation = await conversation_factory(user=user)

    detail = await client.get(
        f"{CONVERSATIONS}/{conversation.id}",
        headers=_headers(make_jwt, sub=user.id, email=user.email),
    )

    assert detail.status_code == 200
    assert detail.json()["messages"] == []


async def test_renaming_persists_and_bumps_activity(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    user_factory: Callable[..., Any],
    conversation_factory: Callable[..., Any],
) -> None:
    user = await user_factory()
    conversation = await conversation_factory(user=user)
    headers = _headers(make_jwt, sub=user.id, email=user.email)

    renamed = await client.patch(
        f"{CONVERSATIONS}/{conversation.id}", json={"title": "Gateways"}, headers=headers
    )

    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Gateways"
    assert (await client.get(CONVERSATIONS, headers=headers)).json()[0]["title"] == "Gateways"

    cleared = await client.patch(
        f"{CONVERSATIONS}/{conversation.id}", json={"title": None}, headers=headers
    )
    assert cleared.json()["title"] is None


async def test_deleting_removes_the_thread(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
) -> None:
    headers = _headers(make_jwt)
    created = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers=headers,
    )
    conversation_id = created.json()["conversation_id"]

    deleted = await client.delete(f"{CONVERSATIONS}/{conversation_id}", headers=headers)
    assert deleted.status_code == 204

    assert (
        await client.get(f"{CONVERSATIONS}/{conversation_id}", headers=headers)
    ).status_code == 404
    assert (
        await client.delete(f"{CONVERSATIONS}/{conversation_id}", headers=headers)
    ).status_code == 404
    assert (await client.get(CONVERSATIONS, headers=headers)).json() == []


@pytest.mark.parametrize(
    "method,payload", [("get", None), ("patch", {"title": "x"}), ("delete", None)]
)
async def test_someone_elses_conversation_is_a_404(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    user_factory: Callable[..., Any],
    conversation_factory: Callable[..., Any],
    method: str,
    payload: dict[str, Any] | None,
) -> None:
    """404, never 403 — a 403 would confirm the id names a real conversation."""
    alice = await user_factory(email="alice@example.com")
    conversation = await conversation_factory(user=alice)
    bob = _headers(make_jwt, sub=uuid4(), email="bob@example.com")

    response = await client.request(
        method, f"{CONVERSATIONS}/{conversation.id}", json=payload, headers=bob
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "conversation_not_found"


@pytest.mark.parametrize("path", [CONVERSATIONS, f"{CONVERSATIONS}/{uuid4()}"])
async def test_credentials_are_required(client: httpx.AsyncClient, path: str) -> None:
    assert (await client.get(path)).status_code == 401
