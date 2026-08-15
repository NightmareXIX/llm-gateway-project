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

from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ProvidersConfig, get_providers_config
from app.db.models import Conversation, Message, Request
from app.providers.registry import build_registry
from tests import provider_fixtures
from tests.conftest import TokenFactory
from tests.provider_fixtures import ScriptedHandler

pytestmark = pytest.mark.integration

COMPLETIONS = "/v1/chat/completions"


def _groq_only() -> ProvidersConfig:
    """The committed slot table with the other two providers switched off.

    Most of the tests below assert on a *two-candidate* fleet — the exact chain
    that was attempted, the exact length of the trail, which provider ended up on
    the ``requests`` row. Those are statements about the router, not about which
    providers Phase 2 Step 6 happened to enable, and pinning the fleet is what
    keeps them from having to be rewritten every time the YAML grows a candidate.
    Cross-provider failover has its own test, against the real config.

    ``model_copy`` rather than mutation: both models are frozen, and
    ``get_providers_config`` is ``lru_cache``d, so editing in place would leak into
    every other test in the session.
    """
    config = get_providers_config()
    providers = {
        name: entry if name == "groq" else entry.model_copy(update={"enabled": False})
        for name, entry in config.providers.items()
    }
    return config.model_copy(update={"providers": providers})


@pytest.fixture
async def groq(app: FastAPI) -> AsyncIterator[provider_fixtures.RecordingHandler]:
    """Swap the app's registry for one that answers from a recorded fixture.

    Yields the handler, so a test can reassign ``handler.recorded`` to a failure
    case before its request, and read ``handler.last_json()`` afterwards to
    assert on what the gateway actually sent upstream.
    """
    handler = provider_fixtures.RecordingHandler(provider_fixtures.load("groq", "success"))
    client = handler.client()
    app.state.provider_registry = build_registry(client=client, config=_groq_only())
    try:
        yield handler
    finally:
        await client.aclose()


@pytest.fixture
async def groq_script(app: FastAPI) -> AsyncIterator[Callable[..., ScriptedHandler]]:
    """Install a handler that answers a *sequence* of fixtures, one per call.

    The failover fixture. ``groq`` above repeats one response forever, which
    cannot express "the first candidate 429s and the second one answers" — the
    single behaviour Step 5 exists to deliver.

    Pinned to a Groq-only fleet of two models: ``general``'s
    ``llama-3.3-70b-versatile`` and ``fast``'s ``llama-3.1-8b-instant``. D10's
    spill means either slot's chain reaches both, so these tests exercise the
    failover loop against a chain short enough to script exactly.
    """
    installed: list[httpx.AsyncClient] = []

    def install(*names: str) -> ScriptedHandler:
        handler = ScriptedHandler(*(provider_fixtures.load("groq", name) for name in names))
        client = handler.client()
        installed.append(client)
        app.state.provider_registry = build_registry(client=client, config=_groq_only())
        return handler

    try:
        yield install
    finally:
        for client in installed:
            await client.aclose()


@pytest.fixture
async def fleet_script(app: FastAPI) -> AsyncIterator[Callable[..., ScriptedHandler]]:
    """``groq_script``'s cross-provider sibling, against the *committed* config.

    Fixtures are named ``"provider/case"``, because the point of this fixture is
    that consecutive attempts land on different providers. Nothing else in this
    module uses the real three-provider fleet, which is deliberate: one test
    proving Milestone A beats six tests that all have to be re-scripted whenever a
    candidate is added.
    """
    installed: list[httpx.AsyncClient] = []

    def install(*specs: str) -> ScriptedHandler:
        handler = ScriptedHandler(*(provider_fixtures.load(*spec.split("/", 1)) for spec in specs))
        client = handler.client()
        installed.append(client)
        app.state.provider_registry = build_registry(client=client)
        return handler

    try:
        yield install
    finally:
        for client in installed:
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
# Failover — Phase 2 Step 5, and the first time any of this is true
# --------------------------------------------------------------------------- #
async def test_a_rate_limited_slot_is_substituted_and_says_so(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_script: Callable[..., ScriptedHandler],
    db_session: AsyncSession,
) -> None:
    """D2 end to end: the named slot fails, another answers, and the response
    discloses it. Before D10's spill this assertion was unwritable — inside one
    slot the requested and served slots cannot differ (ADR-011)."""
    handler = groq_script("rate_limited", "success")

    response = await client.post(
        COMPLETIONS,
        json={"model": "fast", "messages": [{"role": "user", "content": "hello"}]},
        headers=_headers(make_jwt),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["requested_slot"] == "fast"
    assert body["served_by"]["slot"] == "general"
    assert body["served_by"]["model"] == "llama-3.3-70b-versatile"
    assert body["substituted"] is True
    assert body["attempts"] == 2
    # The chain as the wire saw it: the named slot first, then the spill.
    assert handler.models() == ["llama-3.1-8b-instant", "llama-3.3-70b-versatile"]

    stored = await _messages(db_session, body["conversation_id"])
    assert stored[1].meta["substituted"] is True
    assert stored[1].meta["attempts"] == 2
    assert stored[1].meta["model_used"] == "llama-3.3-70b-versatile"


async def test_auto_failing_over_is_not_a_substitution(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_script: Callable[..., ScriptedHandler],
) -> None:
    """Nothing was overridden — the client asked the gateway to choose. Reporting
    `substituted` here would cry wolf on every ordinary failover and train people
    to ignore the field that matters."""
    handler = groq_script("rate_limited", "success")

    response = await client.post(
        COMPLETIONS,
        json={"model": "auto", "messages": [{"role": "user", "content": "hello"}]},
        headers=_headers(make_jwt),
    )

    body = response.json()
    assert body["substituted"] is False
    assert body["attempts"] == 2
    assert body["served_by"]["slot"] == "fast"
    assert handler.calls == 2


async def test_exactly_one_assistant_message_however_many_attempts(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_script: Callable[..., ScriptedHandler],
    db_session: AsyncSession,
) -> None:
    """One logical message, one row. The discarded attempt leaves no trace in
    ``messages`` — which is exactly why the trail in ``requests`` has to exist."""
    groq_script("rate_limited", "success")

    response = await client.post(
        COMPLETIONS,
        json={"messages": [{"role": "user", "content": "hello"}]},
        headers=_headers(make_jwt),
    )

    stored = await _messages(db_session, response.json()["conversation_id"])
    assert [(m.seq, m.role) for m in stored] == [(0, "user"), (1, "assistant")]


async def test_the_attempt_trail_reaches_the_requests_row(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_script: Callable[..., ScriptedHandler],
    db_session: AsyncSession,
) -> None:
    """``requests.attempts`` is the only place a discarded attempt survives, and
    Phase 7's dashboard reads this shape."""
    groq_script("rate_limited", "success")

    await client.post(
        COMPLETIONS,
        json={"model": "fast", "messages": [{"role": "user", "content": "hello"}]},
        headers=_headers(make_jwt),
    )

    (row,) = await _requests(db_session)
    assert row.status == "ok"
    assert row.substituted is True
    assert row.served_slot == "general"
    assert row.model == "llama-3.3-70b-versatile"

    first, second = row.attempts
    assert first["n"] == 1
    assert first["outcome"] == "error"
    assert first["error_code"] == "rate_limited"
    assert first["model"] == "llama-3.1-8b-instant"
    assert second["outcome"] == "ok"
    assert second["model"] == "llama-3.3-70b-versatile"
    assert row.wasted_tokens_out == 0


async def test_a_failed_request_still_records_what_it_tried(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_script: Callable[..., ScriptedHandler],
    db_session: AsyncSession,
) -> None:
    """The row that answers "why did this take eight seconds before failing?".
    Two candidates, both spent, and the trail is the only record of the first."""
    handler = groq_script("rate_limited", "rate_limited")

    response = await client.post(
        COMPLETIONS,
        json={"messages": [{"role": "user", "content": "hello"}]},
        headers=_headers(make_jwt),
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "rate_limited"
    assert handler.calls == 2

    (row,) = await _requests(db_session)
    assert row.status == "error"
    assert [attempt["outcome"] for attempt in row.attempts] == ["error", "error"]
    assert [attempt["model"] for attempt in row.attempts] == [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
    ]
    # The last candidate attempted, so "which slot was it on?" stays answerable.
    assert row.served_slot == "fast"


# --------------------------------------------------------------------------- #
# Milestone A — failover across genuinely different providers
# --------------------------------------------------------------------------- #
async def test_failover_crosses_providers(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    fleet_script: Callable[..., ScriptedHandler],
    db_session: AsyncSession,
) -> None:
    """The one test in this module that runs against the real three-provider fleet.

    Everything above proves the failover *loop* works, but only across two Groq
    models — which cannot distinguish "the router recovers from a failure" from
    "the router recovers from a failure it understands the shape of". Here the
    second attempt is a different provider, with a different request shape, a
    different auth header, the model in the URL instead of the body, and a
    completely different response schema. Nothing in ``router.py`` knows any of
    that; the whole difference lives behind Contract A.

    This is Milestone A's exit criterion, and the only place it is asserted.
    """
    handler = fleet_script("groq/rate_limited", "gemini/success")

    response = await client.post(
        COMPLETIONS,
        json={"model": "general", "messages": [{"role": "user", "content": "hello"}]},
        headers=_headers(make_jwt),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["served_by"]["provider"] == "gemini"
    assert body["served_by"]["model"] == "gemini-3.6-flash"
    assert body["served_by"]["slot"] == "general"
    # Still the requested slot, so this is a failover rather than a substitution.
    assert body["substituted"] is False
    assert body["attempts"] == 2

    # The chain as the wire saw it. The second entry comes out of the URL, because
    # Gemini has no `model` field in its body at all.
    assert handler.models() == ["llama-3.3-70b-versatile", "gemini-3.6-flash"]

    (row,) = await _requests(db_session)
    assert row.provider == "gemini"
    assert [attempt["provider"] for attempt in row.attempts] == ["groq", "gemini"]

    # Gemini's own `usageMetadata`, read through its own `extract_usage` and landing
    # in the same columns Groq's `usage` block does — which is the accounting half of
    # the abstraction. Derived from the fixture rather than pasted from it, because
    # that fixture is a live capture whose token counts change on every re-record.
    served = provider_fixtures.load("gemini", "success")
    assert served.body is not None
    metadata = served.body["usageMetadata"]
    assert row.tokens_in == metadata["promptTokenCount"]
    assert row.tokens_out == metadata["candidatesTokenCount"] + metadata["thoughtsTokenCount"]

    stored = await _messages(db_session, body["conversation_id"])
    assert stored[1].meta["provider_used"] == "gemini"
    assert stored[1].meta["attempts"] == 2


async def test_a_bad_request_aborts_on_the_first_candidate(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_script: Callable[..., ScriptedHandler],
    db_session: AsyncSession,
) -> None:
    """Asserted through the endpoint, not just the router: a malformed payload is
    equally unparseable on the next model, and walking it down the chain would
    turn one fast failure into two slow ones."""
    handler = groq_script("bad_request")

    response = await client.post(
        COMPLETIONS,
        json={"messages": [{"role": "user", "content": "hello"}]},
        headers=_headers(make_jwt),
    )

    assert response.status_code == 502
    assert handler.calls == 1
    (row,) = await _requests(db_session)
    assert len(row.attempts) == 1


async def test_a_pinned_conversation_ignores_the_requested_slot(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_script: Callable[..., ScriptedHandler],
    db_session: AsyncSession,
) -> None:
    """D3's read side. Nothing writes ``pinned_model`` until tool calls land, so
    the test sets it the way that code eventually will — and the router honours it
    over ``auto``, which would otherwise have picked ``general``."""
    handler = groq_script("success", "success")
    headers = _headers(make_jwt)

    first = await client.post(
        COMPLETIONS, json={"messages": [{"role": "user", "content": "hi"}]}, headers=headers
    )
    conversation_id = UUID(first.json()["conversation_id"])
    assert first.json()["served_by"]["model"] == "llama-3.3-70b-versatile"

    conversation = await db_session.get(Conversation, conversation_id)
    assert conversation is not None
    conversation.pinned_model = "groq/llama-3.1-8b-instant"
    await db_session.flush()

    second = await client.post(
        COMPLETIONS,
        json={
            "model": "auto",
            "conversation_id": str(conversation_id),
            "messages": [{"role": "user", "content": "and again"}],
        },
        headers=headers,
    )

    assert second.json()["served_by"]["model"] == "llama-3.1-8b-instant"
    assert handler.models()[-1] == "llama-3.1-8b-instant"


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
