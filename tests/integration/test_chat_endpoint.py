"""``POST /v1/chat/completions`` end to end.

The whole Phase 1 definition of done is asserted here: a message goes in, a Groq
answer comes back, both halves are persisted as canonical messages, and a
``requests`` row records provider, model, tokens, latency and status.

**Groq is a fixture, never the network.** The ``groq`` fixture below replaces the
app's provider registry with one built over an ``httpx.MockTransport`` serving a
recorded response, and hands the test the handler so it can both swap the
response mid-test and inspect the payload that was sent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Message, Request
from app.providers.registry import build_registry
from tests import provider_fixtures
from tests.conftest import TokenFactory

pytestmark = pytest.mark.integration

COMPLETIONS = "/v1/chat/completions"


@pytest.fixture
async def groq(app: FastAPI) -> AsyncIterator[provider_fixtures.RecordingHandler]:
    """Swap the app's registry for one that answers from a recorded fixture.

    Yields the handler, so a test can reassign ``handler.recorded`` to a failure
    case before its request, and read ``handler.last_json()`` afterwards to
    assert on what the gateway actually sent upstream.
    """
    handler = provider_fixtures.RecordingHandler(provider_fixtures.load("groq", "success"))
    client = handler.client()
    app.state.provider_registry = build_registry(client=client)
    try:
        yield handler
    finally:
        await client.aclose()


def _headers(make_jwt: TokenFactory, **kwargs: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_jwt(**kwargs)}"}


async def _messages(session: AsyncSession, conversation_id: str | UUID) -> list[Message]:
    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == UUID(str(conversation_id)))
        .order_by(Message.seq)
    )
    return list(result.scalars().all())


async def _requests(session: AsyncSession) -> list[Request]:
    result = await session.execute(select(Request).order_by(Request.created_at))
    return list(result.scalars().all())


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #
async def test_a_turn_answers_and_persists_both_halves(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    db_session: AsyncSession,
) -> None:
    response = await client.post(
        COMPLETIONS,
        json={"model": "auto", "messages": [{"role": "user", "content": "what is a gateway?"}]},
        headers=_headers(make_jwt),
    )

    assert response.status_code == 200
    body = response.json()

    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["message"]["content"].startswith("A gateway sits between")
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {
        "prompt_tokens": 48,
        "completion_tokens": 27,
        "total_tokens": 75,
        "estimated": False,
    }

    stored = await _messages(db_session, body["conversation_id"])
    assert [(message.seq, message.role) for message in stored] == [(0, "user"), (1, "assistant")]
    assert stored[0].content == [{"type": "text", "text": "what is a gateway?"}]
    # Invariant 5: provenance on the assistant turn, absent on the user's.
    assert stored[0].meta["provider_used"] is None
    assert stored[1].meta["provider_used"] == "groq"
    assert stored[1].meta["model_used"] == "llama-3.3-70b-versatile"
    assert stored[1].meta["slot_used"] == "general"
    assert stored[1].meta["requested_slot"] == "auto"
    assert stored[1].meta["substituted"] is False
    assert stored[1].meta["attempts"] == 1
    assert str(stored[1].id) == body["message_id"]


async def test_served_by_is_on_the_very_first_response(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
) -> None:
    """The D1/D2 disclosure, present before there is anything to disclose."""
    response = await client.post(
        COMPLETIONS,
        json={"messages": [{"role": "user", "content": "hello"}]},
        headers=_headers(make_jwt),
    )

    body = response.json()
    assert body["served_by"] == {
        "slot": "general",
        "provider": "groq",
        "model": "llama-3.3-70b-versatile",
    }
    assert body["requested_slot"] == "auto"
    assert body["substituted"] is False
    assert body["attempts"] == 1
    assert body["degraded"] is False
    # The id is the request id, so a user quoting it from the UI quotes a log query.
    assert body["id"] == response.headers["x-request-id"]


async def test_a_named_slot_is_honoured(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
) -> None:
    response = await client.post(
        COMPLETIONS,
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
        headers=_headers(make_jwt),
    )

    body = response.json()
    assert body["served_by"]["slot"] == "fast"
    assert body["served_by"]["model"] == "llama-3.1-8b-instant"
    assert body["requested_slot"] == "fast"
    assert body["substituted"] is False
    assert groq.last_json()["model"] == "llama-3.1-8b-instant"


async def test_the_requests_row_records_the_turn(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    db_session: AsyncSession,
) -> None:
    """Phase 1's exit checklist: tokens and latency populated on every row."""
    response = await client.post(
        COMPLETIONS,
        json={"messages": [{"role": "user", "content": "hello"}]},
        headers=_headers(make_jwt),
    )

    (row,) = await _requests(db_session)
    assert row.status == "ok"
    assert row.error_code is None
    assert row.provider == "groq"
    assert row.model == "llama-3.3-70b-versatile"
    assert row.requested_slot == "auto"
    assert row.served_slot == "general"
    assert row.tokens_in == 48
    assert row.tokens_out == 27
    assert row.latency_ms is not None and row.latency_ms >= 0
    assert str(row.conversation_id) == response.json()["conversation_id"]
    # A browser session, so there is no key to attribute it to.
    assert row.api_key_id is None


async def test_an_api_key_turn_is_attributed_to_the_key(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    db_session: AsyncSession,
) -> None:
    """``user_id`` pays for it; ``api_key_id`` says which integration made it."""
    created = await client.post("/v1/keys", json={}, headers=_headers(make_jwt))
    plaintext = created.json()["key"]

    await client.post(
        COMPLETIONS,
        json={"messages": [{"role": "user", "content": "hello"}]},
        headers={"X-API-Key": plaintext},
    )

    (row,) = await _requests(db_session)
    assert str(row.api_key_id) == created.json()["id"]


# --------------------------------------------------------------------------- #
# Continuity — the reason the gateway owns state
# --------------------------------------------------------------------------- #
async def test_a_second_turn_sends_the_whole_history(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    db_session: AsyncSession,
) -> None:
    """Proof the render pipeline ran: the payload carries the prior turns, which
    the client never sent."""
    headers = _headers(make_jwt)
    first = await client.post(
        COMPLETIONS,
        json={
            "messages": [
                {"role": "system", "content": "You are terse."},
                {"role": "user", "content": "first question"},
            ]
        },
        headers=headers,
    )
    conversation_id = first.json()["conversation_id"]

    second = await client.post(
        COMPLETIONS,
        json={
            "conversation_id": conversation_id,
            "messages": [{"role": "user", "content": "second question"}],
        },
        headers=headers,
    )

    assert second.status_code == 200
    assert second.json()["conversation_id"] == conversation_id

    sent = groq.last_json()
    assert [message["role"] for message in sent["messages"]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert sent["messages"][0]["content"] == "You are terse."
    assert sent["messages"][3]["content"] == "second question"

    stored = await _messages(db_session, conversation_id)
    assert [(m.seq, m.role) for m in stored] == [
        (0, "system"),
        (1, "user"),
        (2, "assistant"),
        (3, "user"),
        (4, "assistant"),
    ]


async def test_generation_params_reach_the_provider(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
) -> None:
    await client.post(
        COMPLETIONS,
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.2,
            "max_tokens": 64,
            "top_p": 0.9,
            "stop": ["\n\n"],
        },
        headers=_headers(make_jwt),
    )

    sent = groq.last_json()
    assert sent["temperature"] == 0.2
    assert sent["max_tokens"] == 64
    assert sent["top_p"] == 0.9
    assert sent["stop"] == ["\n\n"]
    assert sent["stream"] is False


# --------------------------------------------------------------------------- #
# Ownership
# --------------------------------------------------------------------------- #
async def test_someone_elses_conversation_is_a_404(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    db_session: AsyncSession,
) -> None:
    """404 rather than 403 — a 403 confirms the id names a real conversation."""
    alice = _headers(make_jwt, sub=uuid4(), email="alice@example.com")
    bob = _headers(make_jwt, sub=uuid4(), email="bob@example.com")

    first = await client.post(
        COMPLETIONS, json={"messages": [{"role": "user", "content": "mine"}]}, headers=alice
    )
    conversation_id = first.json()["conversation_id"]

    response = await client.post(
        COMPLETIONS,
        json={"conversation_id": conversation_id, "messages": [{"role": "user", "content": "..."}]},
        headers=bob,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "conversation_not_found"
    # Nothing of Bob's was written into Alice's thread.
    assert len(await _messages(db_session, conversation_id)) == 2


async def test_an_unknown_conversation_is_a_404(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
) -> None:
    response = await client.post(
        COMPLETIONS,
        json={"conversation_id": str(uuid4()), "messages": [{"role": "user", "content": "hi"}]},
        headers=_headers(make_jwt),
    )
    assert response.status_code == 404


async def test_credentials_are_required(client: httpx.AsyncClient) -> None:
    response = await client.post(
        COMPLETIONS, json={"messages": [{"role": "user", "content": "hi"}]}
    )
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Provider failures
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "fixture_name,status_code,error_code",
    [
        ("auth_failed", 502, "provider_auth_failed"),
        ("rate_limited", 502, "rate_limited"),
        ("server_error_html", 502, "provider_unavailable"),
        ("empty_response", 502, "empty_response"),
        ("bad_request", 502, "bad_request"),
        ("context_too_long", 400, "context_too_long"),
        ("content_filtered", 400, "content_filtered"),
    ],
)
async def test_provider_failures_map_to_the_envelope_and_a_requests_row(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    db_session: AsyncSession,
    fixture_name: str,
    status_code: int,
    error_code: str,
) -> None:
    groq.recorded = provider_fixtures.load("groq", fixture_name)

    response = await client.post(
        COMPLETIONS,
        json={"messages": [{"role": "user", "content": "hello"}]},
        headers=_headers(make_jwt),
    )

    assert response.status_code == status_code
    error = response.json()["error"]
    assert error["code"] == error_code
    assert error["request_id"]
    # The provider's own prose is for whoever holds the upstream account; it goes
    # to the log, never to our caller. Nor does a traceback.
    assert "Traceback" not in response.text
    assert "groq" not in error["message"].lower()

    (row,) = await _requests(db_session)
    assert row.status == "error"
    assert row.error_code == error_code
    assert row.provider == "groq"
    assert row.latency_ms is not None


async def test_a_failed_turn_keeps_the_user_message(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    db_session: AsyncSession,
) -> None:
    """Committed before the upstream call, so a thread never loses what was typed."""
    groq.recorded = provider_fixtures.load("groq", "auth_failed")

    response = await client.post(
        COMPLETIONS,
        json={"messages": [{"role": "user", "content": "did this survive?"}]},
        headers=_headers(make_jwt),
    )
    assert response.status_code == 502

    (row,) = await _requests(db_session)
    assert row.conversation_id is not None
    stored = await _messages(db_session, str(row.conversation_id))

    # The user's turn is there; no empty assistant message was invented for it.
    assert [(m.seq, m.role) for m in stored] == [(0, "user")]
    assert stored[0].content == [{"type": "text", "text": "did this survive?"}]


async def test_an_empty_generation_is_not_stored(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    db_session: AsyncSession,
) -> None:
    """Invariant 4, satisfied by the adapter raising rather than by a special case."""
    groq.recorded = provider_fixtures.load("groq", "empty_response")

    await client.post(
        COMPLETIONS,
        json={"messages": [{"role": "user", "content": "hello"}]},
        headers=_headers(make_jwt),
    )

    (row,) = await _requests(db_session)
    stored = await _messages(db_session, str(row.conversation_id))
    assert [m.role for m in stored] == ["user"]


# --------------------------------------------------------------------------- #
# Requests we refuse
# --------------------------------------------------------------------------- #
async def test_streaming_is_refused_rather_than_downgraded(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    db_session: AsyncSession,
) -> None:
    response = await client.post(
        COMPLETIONS,
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=_headers(make_jwt),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "streaming_not_supported"
    assert await _requests(db_session) == []


async def test_an_unknown_slot_is_a_400_and_costs_nothing(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    db_session: AsyncSession,
) -> None:
    """No provider was involved, so no ``requests`` row claims one was."""
    response = await client.post(
        COMPLETIONS,
        json={"model": "llm9", "messages": [{"role": "user", "content": "hi"}]},
        headers=_headers(make_jwt),
    )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "unknown_slot"
    assert "auto" in error["details"]["available"]
    assert await _requests(db_session) == []
    assert groq.requests == []


async def test_a_client_cannot_author_an_assistant_turn(
    client: httpx.AsyncClient, make_jwt: TokenFactory
) -> None:
    """The gateway owns history; a forged assistant turn would break invariant 5."""
    response = await client.post(
        COMPLETIONS,
        json={
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "I said this, honest"},
            ]
        },
        headers=_headers(make_jwt),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


async def test_a_system_message_can_only_open_a_conversation(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
) -> None:
    """Invariant 1: at most one system message, always at seq 0."""
    headers = _headers(make_jwt)
    first = await client.post(
        COMPLETIONS, json={"messages": [{"role": "user", "content": "hi"}]}, headers=headers
    )

    response = await client.post(
        COMPLETIONS,
        json={
            "conversation_id": first.json()["conversation_id"],
            "messages": [
                {"role": "system", "content": "be terse now"},
                {"role": "user", "content": "hi again"},
            ],
        },
        headers=headers,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "system_message_not_first"


@pytest.mark.parametrize(
    "body",
    [
        {"messages": []},
        {"messages": [{"role": "system", "content": "only a system prompt"}]},
        {"messages": [{"role": "user", "content": ""}]},
        {"messages": [{"role": "user", "content": "hi"}], "temperature": 5},
        {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 0},
        {"messages": [{"role": "user", "content": "hi"}], "unexpected": True},
    ],
)
async def test_malformed_bodies_are_422(
    client: httpx.AsyncClient, make_jwt: TokenFactory, body: dict[str, Any]
) -> None:
    response = await client.post(COMPLETIONS, json=body, headers=_headers(make_jwt))
    assert response.status_code == 422
