"""``POST /v1/chat/completions`` — D5/D19's exact-match cache, end to end.

Everything here runs through the real endpoint against a Groq-only fleet, the
same shape ``test_chat_endpoint.py`` uses. What is new is the assertion that a
*second*, identical, deterministic request never reaches the mock transport at
all — the strongest form of "this did not call the provider" available at this
layer.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ProvidersConfig, get_providers_config
from app.db.models import Message, Request
from app.providers.registry import build_registry
from tests import provider_fixtures
from tests.conftest import TokenFactory

pytestmark = pytest.mark.integration

COMPLETIONS = "/v1/chat/completions"


def _groq_only() -> ProvidersConfig:
    """The committed slot table with the other two providers switched off — the
    same pin ``test_chat_endpoint.py`` uses, and for the same reason: these tests
    assert on an exact call count, and a fleet that could spill to a second
    provider would make that count depend on unrelated config."""
    config = get_providers_config()
    providers = {
        name: entry if name == "groq" else entry.model_copy(update={"enabled": False})
        for name, entry in config.providers.items()
    }
    return config.model_copy(update={"providers": providers})


@pytest.fixture
async def groq(app: FastAPI) -> AsyncIterator[provider_fixtures.RecordingHandler]:
    handler = provider_fixtures.RecordingHandler(provider_fixtures.load("groq", "success"))
    client = handler.client()
    app.state.provider_registry = build_registry(client=client, config=_groq_only())
    try:
        yield handler
    finally:
        await client.aclose()


class _SseHandler:
    """Serves the same recorded SSE body on every call, and keeps a count.

    ``RecordingHandler`` is built around a single JSON :class:`RecordedResponse`
    and cannot serve raw ``text/event-stream`` bytes, so this is its streaming
    twin — small enough not to be worth generalizing back into
    ``tests/provider_fixtures.py`` for one test module.
    """

    def __init__(self, sse_body: bytes) -> None:
        self._sse_body = sse_body
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200, content=self._sse_body, headers={"content-type": "text/event-stream"}
        )

    def client(self) -> httpx.AsyncClient:
        return provider_fixtures.client_from(self)


@pytest.fixture
async def groq_streaming(app: FastAPI) -> AsyncIterator[_SseHandler]:
    """A streaming-capable twin of ``groq``: every request gets the recorded SSE
    body, and requests are still counted the same way."""
    handler = _SseHandler(provider_fixtures.read_sse("groq", "stream_success").encode("utf-8"))
    client = handler.client()
    app.state.provider_registry = build_registry(client=client, config=_groq_only())
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
    result = await session.execute(select(Request).order_by(Request.created_at, Request.id))
    return list(result.scalars().all())


def _sse_meta(body: str) -> dict[str, Any]:
    """The first event's JSON payload — every stream starts with ``meta``."""
    first_data_line = next(line for line in body.splitlines() if line.startswith("data: "))
    parsed: dict[str, Any] = json.loads(first_data_line.removeprefix("data: "))
    return parsed


def _body(
    *, temperature: float | None = None, content: str = "what is a gateway?"
) -> dict[str, Any]:
    payload: dict[str, Any] = {"messages": [{"role": "user", "content": content}]}
    if temperature is not None:
        payload["temperature"] = temperature
    return payload


# --------------------------------------------------------------------------- #
# Non-streaming
# --------------------------------------------------------------------------- #
async def test_a_deterministic_repeat_is_a_hit_and_costs_no_call(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    db_session: AsyncSession,
) -> None:
    headers = _headers(make_jwt)

    first = await client.post(COMPLETIONS, json=_body(temperature=0), headers=headers)
    assert first.status_code == 200
    assert first.headers["x-cache"] == "MISS"

    second = await client.post(COMPLETIONS, json=_body(temperature=0), headers=headers)

    assert second.status_code == 200
    assert second.headers["x-cache"] == "HIT"
    assert len(groq.requests) == 1  # the hit never left the process

    body = second.json()
    first_content = first.json()["choices"][0]["message"]["content"]
    assert body["choices"][0]["message"]["content"] == first_content
    assert body["served_by"] == first.json()["served_by"]
    assert body["attempts"] == 0
    assert body["usage"] == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "estimated": False,
    }


async def test_a_hit_still_writes_a_message_and_a_requests_row(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    db_session: AsyncSession,
) -> None:
    """Two fresh, single-turn conversations — not the same thread continued,
    since a growing history would change the hash and the second call would
    legitimately miss. D19's identity is the *content*, not the conversation."""
    headers = _headers(make_jwt)
    first = await client.post(COMPLETIONS, json=_body(temperature=0), headers=headers)
    second = await client.post(COMPLETIONS, json=_body(temperature=0), headers=headers)
    assert second.headers["x-cache"] == "HIT"

    conversation_id = second.json()["conversation_id"]
    stored = await _messages(db_session, conversation_id)
    assert [m.role for m in stored] == ["user", "assistant"]
    assert stored[-1].meta["attempts"] == 0

    rows = {row.conversation_id: row for row in await _requests(db_session)}
    assert rows[UUID(first.json()["conversation_id"])].cache_hit is False
    hit_row = rows[UUID(conversation_id)]
    assert hit_row.cache_hit is True
    assert hit_row.tokens_in == 0
    assert hit_row.tokens_out == 0
    assert hit_row.status == "ok"


async def test_a_non_deterministic_request_always_bypasses(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
) -> None:
    headers = _headers(make_jwt)

    first = await client.post(COMPLETIONS, json=_body(temperature=0.7), headers=headers)
    second = await client.post(COMPLETIONS, json=_body(temperature=0.7), headers=headers)

    assert first.headers["x-cache"] == "BYPASS"
    assert second.headers["x-cache"] == "BYPASS"
    assert len(groq.requests) == 2


async def test_the_default_temperature_bypasses(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
) -> None:
    """``temperature`` defaults to 1.0 (§1's schema) — nothing opts in by accident."""
    response = await client.post(COMPLETIONS, json=_body(), headers=_headers(make_jwt))
    assert response.headers["x-cache"] == "BYPASS"


async def test_a_different_conversation_content_is_a_miss(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
) -> None:
    headers = _headers(make_jwt)
    await client.post(
        COMPLETIONS, json=_body(temperature=0, content="question one"), headers=headers
    )

    second = await client.post(
        COMPLETIONS, json=_body(temperature=0, content="question two"), headers=headers
    )

    assert second.headers["x-cache"] == "MISS"
    assert len(groq.requests) == 2


async def test_the_cache_is_global_across_users(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
) -> None:
    """D19: scoped by content, not by user — the residual disclosure ("someone
    else asked this exact thing") is the deliberate trade the design documents."""
    alice = _headers(make_jwt, sub=uuid4(), email="alice@example.com")
    bob = _headers(make_jwt, sub=uuid4(), email="bob@example.com")

    first = await client.post(COMPLETIONS, json=_body(temperature=0), headers=alice)
    second = await client.post(COMPLETIONS, json=_body(temperature=0), headers=bob)

    assert first.headers["x-cache"] == "MISS"
    assert second.headers["x-cache"] == "HIT"
    assert len(groq.requests) == 1


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #
async def test_a_streamed_repeat_replays_as_a_stream_and_costs_no_call(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_streaming: _SseHandler,
    db_session: AsyncSession,
) -> None:
    headers = _headers(make_jwt)
    payload = {**_body(temperature=0), "stream": True}

    first = await client.post(COMPLETIONS, json=payload, headers=headers)
    assert first.status_code == 200
    assert first.headers["x-cache"] == "MISS"

    second = await client.post(COMPLETIONS, json=payload, headers=headers)

    assert second.status_code == 200
    assert second.headers["x-cache"] == "HIT"
    assert len(groq_streaming.requests) == 1

    text = second.text
    assert "event: meta" in text
    assert "event: delta" in text
    assert "event: done" in text
    assert '"status":"ok"' in text
    assert '"attempts":0' in text

    hit_conversation_id = _sse_meta(text)["conversation_id"]
    stored = await _messages(db_session, hit_conversation_id)
    assert [m.role for m in stored] == ["user", "assistant"]
    assert stored[-1].meta["attempts"] == 0

    rows = {str(row.conversation_id): row for row in await _requests(db_session)}
    assert rows[hit_conversation_id].cache_hit is True


async def test_a_streamed_replay_carries_the_same_text_as_the_original(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_streaming: _SseHandler,
    db_session: AsyncSession,
) -> None:
    headers = _headers(make_jwt)
    payload = {**_body(temperature=0), "stream": True}

    first_response = await client.post(COMPLETIONS, json=payload, headers=headers)
    second_response = await client.post(COMPLETIONS, json=payload, headers=headers)

    result = await db_session.execute(select(Message).where(Message.role == "assistant"))
    assistants = result.scalars().all()
    original_text = "".join(block["text"] for block in assistants[0].content)
    replayed_text = "".join(block["text"] for block in assistants[1].content)

    assert original_text == replayed_text
    assert first_response.status_code == second_response.status_code == 200
