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
from app.db.models import Conversation, Message, Request
from app.db.repo import messages as messages_repo
from app.memory.canonical import MessageMeta, text_block
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


def _narrow_slot(config: ProvidersConfig, slot: str, providers: tuple[str, ...]) -> ProvidersConfig:
    """``test_chat_endpoint.py``'s helper, duplicated rather than imported —
    these are two independent test modules and neither is the other's private
    API. Restricts one slot's candidate list to the named providers."""
    declared = config.slots[slot]
    narrowed = declared.model_copy(
        update={
            "candidates": tuple(
                candidate for candidate in declared.candidates if candidate.provider in providers
            )
        }
    )
    return config.model_copy(update={"slots": {**config.slots, slot: narrowed}})


def _shrink_context(
    config: ProvidersConfig, slot: str, *, provider: str, context_tokens: int
) -> ProvidersConfig:
    """Phase 5 Step 3's helper, duplicated for the same reason as
    ``_narrow_slot`` above: narrow a slot to one deterministic provider and
    shrink its context window small enough that a real multi-turn history
    forces D4 truncation, without touching ``config/providers.yaml`` or the
    fitting algorithm itself."""
    narrowed = _narrow_slot(config, slot, (provider,))
    declared = narrowed.slots[slot]
    candidates = tuple(
        candidate.model_copy(update={"context_tokens": context_tokens})
        for candidate in declared.candidates
    )
    shrunk = declared.model_copy(update={"candidates": candidates})
    return narrowed.model_copy(update={"slots": {**narrowed.slots, slot: shrunk}})


async def _seed_history(
    session: AsyncSession, *, conversation_id: UUID, user_id: UUID, turns: int
) -> None:
    """Append ``turns`` user/assistant pairs directly through the repo,
    bypassing HTTP — the same shortcut ``test_chat_endpoint.py`` uses to build
    a long history without ``turns`` real round trips."""
    for n in range(turns):
        await messages_repo.append(
            session,
            conversation_id=conversation_id,
            user_id=user_id,
            role="user",
            content=[text_block(f"Synthetic turn {n}: filling the history to force truncation.")],
        )
        await messages_repo.append(
            session,
            conversation_id=conversation_id,
            user_id=user_id,
            role="assistant",
            content=[text_block(f"Synthetic reply {n}.")],
            meta=MessageMeta(
                provider_used="groq",
                model_used="synthetic",
                slot_used="fast",
                requested_slot="fast",
                attempts=1,
                tokens_in=5,
                tokens_out=5,
            ),
        )
    await session.commit()


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


# --------------------------------------------------------------------------- #
# Phase 5 Step 4 — D35: a truncated turn is never cached
# --------------------------------------------------------------------------- #
async def _seeded_conversation(
    client: httpx.AsyncClient, headers: dict[str, str], db_session: AsyncSession
) -> Conversation:
    """A fresh conversation, opened on ``fast`` and grown to the same seeded
    shape ``test_chat_endpoint.py``'s truncation tests use. Two conversations
    built this way carry byte-identical history — everything in
    ``_seed_history`` is a deterministic function of the turn index — so a
    later question over either one produces the same ``request_hash`` (D19
    keys on content, never on ``conversation_id``), which is what lets two
    *separate* conversations stand in for "the same question asked twice"
    without a growing history breaking the hash match a same-conversation
    repeat would.
    """
    opener = await client.post(
        COMPLETIONS,
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
        headers=headers,
    )
    assert opener.status_code == 200, opener.text
    conversation_id = UUID(opener.json()["conversation_id"])
    conversation = await db_session.get(Conversation, conversation_id)
    assert conversation is not None
    await _seed_history(
        db_session, conversation_id=conversation.id, user_id=conversation.user_id, turns=6
    )
    return conversation


def _followup_question(conversation: Conversation) -> dict[str, Any]:
    return {
        "model": "fast",
        "temperature": 0,
        "max_tokens": 50,
        "conversation_id": str(conversation.id),
        "messages": [{"role": "user", "content": "what did I say first?"}],
    }


async def test_a_truncated_turn_is_never_cached(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    app: FastAPI,
    db_session: AsyncSession,
) -> None:
    """D35: two identical, deterministic requests whose history the fitting
    step had to truncate must both cost a provider call — an entry written by
    a truncated turn would let ``auto``'s partial-history answer be replayed
    for up to an hour to a caller who might have gotten the whole thing.
    """
    headers = _headers(make_jwt)
    first_conversation = await _seeded_conversation(client, headers, db_session)
    second_conversation = await _seeded_conversation(client, headers, db_session)

    config = _shrink_context(get_providers_config(), "fast", provider="groq", context_tokens=200)
    handler = provider_fixtures.RecordingHandler(provider_fixtures.load("groq", "success"))
    upstream = handler.client()
    app.state.provider_registry = build_registry(client=upstream, config=config)
    try:
        first = await client.post(
            COMPLETIONS, json=_followup_question(first_conversation), headers=headers
        )
        second = await client.post(
            COMPLETIONS, json=_followup_question(second_conversation), headers=headers
        )
    finally:
        await upstream.aclose()

    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert first.json()["messages_dropped"] > 0
    assert second.json()["messages_dropped"] > 0
    assert first.headers["x-cache"] == "MISS"
    assert second.headers["x-cache"] == "MISS"
    assert len(handler.requests) == 2  # the second call was a real attempt, never a replay


async def test_an_untruncated_turn_over_a_long_history_still_caches(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    app: FastAPI,
    db_session: AsyncSession,
) -> None:
    """The D35 gate must not have broken caching generally: the same long,
    identical pair of questions still produces a hit once nothing about the
    history had to be dropped to answer it.
    """
    headers = _headers(make_jwt)
    first_conversation = await _seeded_conversation(client, headers, db_session)
    second_conversation = await _seeded_conversation(client, headers, db_session)

    config = _narrow_slot(get_providers_config(), "fast", ("groq",))
    handler = provider_fixtures.RecordingHandler(provider_fixtures.load("groq", "success"))
    upstream = handler.client()
    app.state.provider_registry = build_registry(client=upstream, config=config)
    try:
        first = await client.post(
            COMPLETIONS, json=_followup_question(first_conversation), headers=headers
        )
        second = await client.post(
            COMPLETIONS, json=_followup_question(second_conversation), headers=headers
        )
    finally:
        await upstream.aclose()

    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert first.json()["messages_dropped"] == 0
    assert second.json()["messages_dropped"] == 0
    assert first.headers["x-cache"] == "MISS"
    assert second.headers["x-cache"] == "HIT"
    assert len(handler.requests) == 1  # the second call was a genuine replay
