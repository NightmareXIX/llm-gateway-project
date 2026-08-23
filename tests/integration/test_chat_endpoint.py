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

import hashlib
import json
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ProvidersConfig, get_providers_config
from app.db.models import Conversation, File, Message, Request
from app.db.repo import messages as messages_repo
from app.deps import get_resolver
from app.memory.canonical import MessageMeta, text_block
from app.perception.storage import MemoryStore
from app.providers.registry import build_registry
from tests import provider_fixtures
from tests.conftest import TokenFactory
from tests.provider_fixtures import ScriptedHandler

pytestmark = pytest.mark.integration

COMPLETIONS = "/v1/chat/completions"
FILES = "/v1/files"
PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"


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
async def groq_streaming(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """``groq``'s streaming sibling: every request gets the recorded
    ``stream_success`` SSE body instead of a single JSON response.

    Enough for the one thing worth proving at this layer — that ``stream: true``
    reaches a real upstream and the answer comes back out the other end — since
    the failover and restart behaviour this transport cannot express are already
    covered end to end in ``tests/unit/test_orchestrator.py`` against the real
    router and adapter.
    """
    client = provider_fixtures.client_streaming(
        [provider_fixtures.read_sse("groq", "stream_success").encode("utf-8")]
    )
    app.state.provider_registry = build_registry(client=client, config=_groq_only())
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def groq_script(app: FastAPI) -> AsyncIterator[Callable[..., ScriptedHandler]]:
    """Install a handler that answers a *sequence* of fixtures, one per call.

    The failover fixture. ``groq`` above repeats one response forever, which
    cannot express "the first candidate 429s and the second one answers" — the
    single behaviour Step 5 exists to deliver.

    Pinned to a Groq-only fleet of two models: ``general``'s
    ``openai/gpt-oss-120b`` and ``fast``'s ``openai/gpt-oss-20b``. D10's
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


def _narrow_slot(config: ProvidersConfig, slot: str, providers: tuple[str, ...]) -> ProvidersConfig:
    """Restrict one slot's candidate list to the named providers, leaving every
    other slot — in particular ``perception`` — exactly as
    ``config/providers.yaml`` declares it.

    Phase 5 Step 2's file-ref-across-a-switch test needs a *deterministic*
    answering provider per slot (``fast`` -> Groq, so the text-only candidate
    forces an extraction; ``general`` -> Gemini, so the second turn is answered
    by the one provider that could have read the file natively) without
    disabling either provider outright — Groq must stay enabled for ``fast`` to
    answer, so ``_groq_only``'s whole-provider toggle above cannot express
    this.
    """
    declared = config.slots[slot]
    candidates = tuple(c for c in declared.candidates if c.provider in providers)
    narrowed = declared.model_copy(update={"candidates": candidates})
    return config.model_copy(update={"slots": {**config.slots, slot: narrowed}})


def _shrink_context(
    config: ProvidersConfig, slot: str, *, provider: str, context_tokens: int
) -> ProvidersConfig:
    """``_narrow_slot``'s sibling for Phase 5 Step 3's truncation tests: narrow
    a slot to one deterministic provider and shrink its context window small
    enough that a real multi-turn history forces D4 truncation — without
    touching ``config/providers.yaml`` or the fitting algorithm itself.
    """
    narrowed = _narrow_slot(config, slot, (provider,))
    declared = narrowed.slots[slot]
    candidates = tuple(
        candidate.model_copy(update={"context_tokens": context_tokens})
        for candidate in declared.candidates
    )
    shrunk = declared.model_copy(update={"candidates": candidates})
    return narrowed.model_copy(update={"slots": {**narrowed.slots, slot: shrunk}})


@pytest.fixture
async def attachment_fleet(app: FastAPI) -> AsyncIterator[Callable[..., ScriptedHandler]]:
    """``fleet_script``'s sibling for the one test that needs a deterministic
    answering provider per slot rather than the committed failover order:
    ``fast`` narrowed to Groq, ``general`` narrowed to Gemini.
    """
    installed: list[httpx.AsyncClient] = []

    def install(*specs: str) -> ScriptedHandler:
        handler = ScriptedHandler(*(provider_fixtures.load(*spec.split("/", 1)) for spec in specs))
        client = handler.client()
        installed.append(client)
        config = get_providers_config()
        config = _narrow_slot(config, "fast", ("groq",))
        config = _narrow_slot(config, "general", ("gemini",))
        app.state.provider_registry = build_registry(client=client, config=config)
        return handler

    try:
        yield install
    finally:
        for client in installed:
            await client.aclose()


@pytest.fixture
def store(app: FastAPI) -> MemoryStore:
    """Swap the lifespan's object store for a dict, so a file_ref test can
    upload through the real endpoint without touching Supabase or a disk."""
    memory = MemoryStore()
    app.state.object_store = memory
    return memory


@pytest.fixture
def no_perception(app: FastAPI) -> None:
    """``PERCEPTION_ENABLED=False``, as ``deps.get_resolver`` expresses it.

    The two tests below are about *what a `file_ref` turn stores*, not about
    what the lane then does with it — that is
    ``tests/integration/test_perception_lane.py``'s whole subject. Switching
    the lane off keeps them pinned to the one thing they assert, and turns the
    500 they expect from an accident of Milestone A into the kill switch
    behaving as designed: ``NoAttachments`` raises rather than resolving a
    reference to nothing.
    """
    app.dependency_overrides[get_resolver] = lambda: None


def _headers(make_jwt: TokenFactory, **kwargs: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_jwt(**kwargs)}"}


async def _upload(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    """Upload the fixture PDF and return the hash this caller now owns."""
    response = await client.post(
        FILES,
        files={"file": ("report.pdf", PDF_BYTES, "application/pdf")},
        headers=headers,
    )
    assert response.status_code == 201
    return str(response.json()["file_hash"])


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


async def _seed_history(
    session: AsyncSession, *, conversation_id: UUID, user_id: UUID, turns: int
) -> None:
    """Append ``turns`` user/assistant pairs directly through the repo,
    bypassing HTTP entirely.

    Phase 5 Step 3's truncation tests need a long history, and driving it
    through ``turns`` real HTTP round trips would make the suite slow for no
    reason ``messages_repo.append`` cannot already prove correct on its own
    (every other test in this module exercises it through the endpoint).
    """
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
    assert stored[1].meta["model_used"] == "openai/gpt-oss-120b"
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
        "model": "openai/gpt-oss-120b",
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
    assert body["served_by"]["model"] == "openai/gpt-oss-20b"
    assert body["requested_slot"] == "fast"
    assert body["substituted"] is False
    assert groq.last_json()["model"] == "openai/gpt-oss-20b"


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
    assert row.model == "openai/gpt-oss-120b"
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
    assert body["served_by"]["model"] == "openai/gpt-oss-120b"
    assert body["substituted"] is True
    assert body["attempts"] == 2
    # The chain as the wire saw it: the named slot first, then the spill.
    assert handler.models() == ["openai/gpt-oss-20b", "openai/gpt-oss-120b"]

    stored = await _messages(db_session, body["conversation_id"])
    assert stored[1].meta["substituted"] is True
    assert stored[1].meta["attempts"] == 2
    assert stored[1].meta["model_used"] == "openai/gpt-oss-120b"


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
    assert row.model == "openai/gpt-oss-120b"

    first, second = row.attempts
    assert first["n"] == 1
    assert first["outcome"] == "error"
    assert first["error_code"] == "rate_limited"
    assert first["model"] == "openai/gpt-oss-20b"
    assert second["outcome"] == "ok"
    assert second["model"] == "openai/gpt-oss-120b"
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
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
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
    assert handler.models() == ["openai/gpt-oss-120b", "gemini-3.6-flash"]

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
    assert first.json()["served_by"]["model"] == "openai/gpt-oss-120b"

    conversation = await db_session.get(Conversation, conversation_id)
    assert conversation is not None
    conversation.pinned_model = "groq/openai/gpt-oss-20b"
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

    assert second.json()["served_by"]["model"] == "openai/gpt-oss-20b"
    assert handler.models()[-1] == "openai/gpt-oss-20b"


# --------------------------------------------------------------------------- #
# Phase 5 Step 2 — continuity across a provider switch, end to end
#
# development-plan.md's exit criterion for this phase, and the one no existing
# test performs: `test_a_second_turn_sends_the_whole_history` above never
# switches provider, and `test_failover_crosses_providers` never sends a second
# turn. Everything below proves both at once, against genuinely different
# providers within one conversation.
# --------------------------------------------------------------------------- #
async def test_a_thread_survives_a_provider_switch(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    fleet_script: Callable[..., ScriptedHandler],
    db_session: AsyncSession,
) -> None:
    """Three turns, one conversation, three providers.

    Turn one requests ``fast`` and Groq answers outright. Turn two requests
    ``general`` — a deliberate switch — and Groq (``general``'s own first
    candidate, a different model than ``fast``'s) is scripted to fail, so the
    turn actually reaches Gemini by the same in-slot failover Milestone A
    already proved, only now on top of real history. Turn three asks ``general``
    a third time with *both* of its first two candidates scripted to fail, so it
    spills all the way to OpenRouter — the third provider, reached without a
    third slot name exactly as the design note under Step 2 requires.

    `auth_failed` for every scripted failure, deliberately not `rate_limited` or
    `server_error_html`: an `AuthFailed` is failover-eligible with no
    same-candidate retry (`retryable_same_provider` is `False`, unlike
    `Unavailable`'s one built-in retry in `router.py`, which would silently
    spend two of D1's three attempts on a single candidate) and needs
    `FAILURE_THRESHOLD` consecutive failures to open the breaker, not one
    (unlike `RateLimited`, which opens it immediately and would turn turn
    three's repeat attempt on Groq's `general` model into a breaker-skip
    instead of the second real failure this test is scripting). One scripted
    fixture per real attempt is what that combination buys.
    """
    handler = fleet_script(
        "groq/success",
        "groq/auth_failed",
        "gemini/success",
        "groq/auth_failed",
        "gemini/auth_failed",
        "openrouter/success",
    )
    headers = _headers(make_jwt)
    first_text = "Remember this launch phrase: violet horizon."

    # `max_tokens` set explicitly on every turn: `fitting.input_budget` reserves
    # the *whole* configured output ceiling when a request omits it, and
    # OpenRouter's `general` candidate is declared with `max_output_tokens`
    # equal to its own `context_window` — no `max_tokens` would make turn
    # three's real destination raise `ContextTooLong` out of `render()` before
    # a single byte reached the mock transport, for a reason that has nothing
    # to do with what this test is proving.
    first = await client.post(
        COMPLETIONS,
        json={
            "model": "fast",
            "max_tokens": 512,
            "messages": [{"role": "user", "content": first_text}],
        },
        headers=headers,
    )
    assert first.status_code == 200, first.text
    conversation_id = first.json()["conversation_id"]
    assert first.json()["served_by"] == {
        "slot": "fast",
        "provider": "groq",
        "model": "openai/gpt-oss-20b",
    }

    second = await client.post(
        COMPLETIONS,
        json={
            "model": "general",
            "max_tokens": 512,
            "conversation_id": conversation_id,
            "messages": [{"role": "user", "content": "Switching models. What did I say first?"}],
        },
        headers=headers,
    )
    assert second.status_code == 200, second.text
    body2 = second.json()
    assert body2["served_by"] == {
        "slot": "general",
        "provider": "gemini",
        "model": "gemini-3.6-flash",
    }
    # In-slot failover, not a substitution: nothing named a slot the router
    # then overrode.
    assert body2["substituted"] is False
    assert body2["attempts"] == 2

    third = await client.post(
        COMPLETIONS,
        json={
            "model": "general",
            "max_tokens": 512,
            "conversation_id": conversation_id,
            "messages": [{"role": "user", "content": "One more time: what was the launch phrase?"}],
        },
        headers=headers,
    )
    assert third.status_code == 200, third.text
    body3 = third.json()
    assert body3["served_by"] == {
        "slot": "general",
        "provider": "openrouter",
        "model": "nvidia/nemotron-3-super-120b-a12b:free",
    }
    assert body3["substituted"] is False
    assert body3["attempts"] == 3

    # The chain as the wire saw it, across all three turns.
    assert handler.models() == [
        "openai/gpt-oss-20b",
        "openai/gpt-oss-120b",
        "gemini-3.6-flash",
        "openai/gpt-oss-120b",
        "gemini-3.6-flash",
        "nvidia/nemotron-3-super-120b-a12b:free",
    ]

    # The three provider shapes, on the three requests that actually answered.
    groq_sent = json.loads(handler.requests[0].content)
    gemini_sent = json.loads(handler.requests[2].content)
    openrouter_sent = json.loads(handler.requests[5].content)
    assert "messages" in groq_sent and "contents" not in groq_sent
    assert "contents" in gemini_sent and "messages" not in gemini_sent
    assert "messages" in openrouter_sent and "contents" not in openrouter_sent

    # Turn one's user text survived two switches and reached turn three's payload
    # — proof the render pipeline, not just the router, carried history across
    # providers.
    assert first_text in json.dumps(openrouter_sent)

    stored = await _messages(db_session, conversation_id)
    assert [(m.seq, m.role) for m in stored] == [
        (0, "user"),
        (1, "assistant"),
        (2, "user"),
        (3, "assistant"),
        (4, "user"),
        (5, "assistant"),
    ]
    assert stored[1].meta["provider_used"] == "groq"
    assert stored[3].meta["provider_used"] == "gemini"
    assert stored[5].meta["provider_used"] == "openrouter"


async def test_a_file_ref_survives_a_provider_switch_from_cache(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    attachment_fleet: Callable[..., ScriptedHandler],
    db_session: AsyncSession,
) -> None:
    """Phase 4's handoff, cashed: one uploaded file, one stored ``file_ref``
    block, and one conversation that switches providers mid-thread without a
    second extraction.

    Turn one asks about it under ``fast`` (Groq, text-only), which forces a
    real Gemini extraction. Turn two asks a follow-up under ``general``
    (Gemini, which *can* read the file natively) *without* repeating
    ``file_refs`` — the stored block from turn one is already in history, and
    ``render``'s step 1 scans the whole history for ``file_ref`` blocks, not
    just the current turn's message.

    The tier on turn two is ``cache``, not ``native``: ``lane.py``'s tier 0
    check runs *before* tier 1's native check and returns a stored ``llm``
    reading unconditionally (``_run_chain``, "tier 0" — the same ordering
    ``docs/limitations.md`` names as "cache-beats-native on layout
    questions"). That is why §1's own definition of done hedges with
    "``extraction_tier`` goes ``llm`` → ``cache`` *or* ``native``" rather than
    promising native outright — this is the ``fast``-then-``general``
    ordering, and cache wins it every time. What both turns share, and what
    this test actually exists to prove, is that one upload and one extraction
    answer two different providers in one thread with no second round trip to
    the perception lane at all.
    """
    handler = attachment_fleet("gemini/extraction_complete", "groq/success", "gemini/success")
    headers = _headers(make_jwt)
    file_hash = await _upload(client, headers)

    first = await client.post(
        COMPLETIONS,
        json={
            "model": "fast",
            "messages": [
                {"role": "user", "content": "what does this say?", "file_refs": [file_hash]}
            ],
        },
        headers=headers,
    )
    assert first.status_code == 200, first.text
    body1 = first.json()
    assert body1["extraction_tier"] == "llm"
    assert body1["degraded"] is False
    conversation_id = body1["conversation_id"]

    second = await client.post(
        COMPLETIONS,
        json={
            "model": "general",
            "conversation_id": conversation_id,
            "messages": [{"role": "user", "content": "and what does it say about the appendix?"}],
        },
        headers=headers,
    )
    assert second.status_code == 200, second.text
    body2 = second.json()
    assert body2["extraction_tier"] == "cache"
    assert body2["degraded"] is False

    # Exactly the three scripted calls ran: the extraction, Groq's answer, and
    # Gemini's answer. A second extraction would have been a fourth, unscripted
    # request, which `ScriptedHandler` would have raised on — this just names
    # the count so a future regression fails here with a clear message instead.
    assert handler.calls == 3

    # One upload, one stored `file_ref` block — turn two never sent one.
    files = (await db_session.execute(select(File))).scalars().all()
    assert len(files) == 1
    assert files[0].file_hash == file_hash

    stored = await _messages(db_session, conversation_id)
    file_ref_blocks = [
        block
        for message in stored
        if message.role == "user"
        for block in message.content
        if block["type"] == "file_ref"
    ]
    assert len(file_ref_blocks) == 1
    assert file_ref_blocks[0]["file_hash"] == file_hash

    # Groq cannot read the file, so it was handed the extracted text, wrapped in
    # the same envelope every adapter shares.
    groq_sent = json.dumps(json.loads(handler.requests[1].content))
    assert "<document" in groq_sent
    assert "quarterly report" in groq_sent.lower()

    # Gemini's second answer replays the same cached reading, in its own
    # ``parts`` shape but through the identical envelope — not `inline_data`,
    # because the cache hit above is what tier the resolver actually returned.
    gemini_sent = json.dumps(json.loads(handler.requests[2].content))
    assert "<document" in gemini_sent
    assert "quarterly report" in gemini_sent.lower()
    assert "inline_data" not in gemini_sent


async def test_streaming_and_non_streaming_agree_on_a_switched_thread(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    app: FastAPI,
    db_session: AsyncSession,
) -> None:
    """The same switch, twice: once fully non-streaming, once with the second
    turn streamed. Both paths render through the same ``render()`` call, and
    this is the test that keeps that true — the streamed twin's ``done`` event
    and persisted history must agree with the plain twin's response and rows.

    Two independent conversations rather than one, since streaming and
    non-streaming are different requests and cannot both be "turn two" of the
    same thread; what has to agree is the *shape* each path produces for an
    equivalent switch, not a shared conversation id.
    """
    headers = _headers(make_jwt)
    config = _narrow_slot(get_providers_config(), "fast", ("groq",))
    config = _narrow_slot(config, "general", ("gemini",))

    # Twin A — fully non-streaming.
    client_a = ScriptedHandler(
        provider_fixtures.load("groq", "success"), provider_fixtures.load("gemini", "success")
    ).client()
    app.state.provider_registry = build_registry(client=client_a, config=config)
    try:
        first_a = await client.post(
            COMPLETIONS,
            json={"model": "fast", "messages": [{"role": "user", "content": "hello there"}]},
            headers=headers,
        )
        conversation_a = first_a.json()["conversation_id"]
        second_a = await client.post(
            COMPLETIONS,
            json={
                "model": "general",
                "conversation_id": conversation_a,
                "messages": [{"role": "user", "content": "what did I say first?"}],
            },
            headers=headers,
        )
    finally:
        await client_a.aclose()

    assert second_a.status_code == 200, second_a.text
    served_by_a = second_a.json()["served_by"]
    rows_a = await _messages(db_session, conversation_a)

    # Twin B — the same switch, second turn streamed.
    client_b1 = provider_fixtures.RecordingHandler(
        provider_fixtures.load("groq", "success")
    ).client()
    app.state.provider_registry = build_registry(client=client_b1, config=config)
    try:
        first_b = await client.post(
            COMPLETIONS,
            json={"model": "fast", "messages": [{"role": "user", "content": "hello there"}]},
            headers=headers,
        )
    finally:
        await client_b1.aclose()
    conversation_b = first_b.json()["conversation_id"]

    client_b2 = provider_fixtures.client_streaming(
        [provider_fixtures.read_sse("gemini", "stream_success").encode("utf-8")]
    )
    app.state.provider_registry = build_registry(client=client_b2, config=config)
    try:
        second_b = await client.post(
            COMPLETIONS,
            json={
                "model": "general",
                "conversation_id": conversation_b,
                "messages": [{"role": "user", "content": "what did I say first?"}],
                "stream": True,
            },
            headers=headers,
        )
    finally:
        await client_b2.aclose()

    assert second_b.status_code == 200, second_b.text
    done = _sse_event(second_b.text, "done")
    assert done["served_by"] == served_by_a
    rows_b = await _messages(db_session, conversation_b)

    # Same shape, not the same words: the two twins hit two different recorded
    # fixtures (a JSON completion and an SSE stream), so their assistant text
    # legitimately differs. What has to agree is *how* each turn was served —
    # same roles, same provider per turn — and the user's own words, which this
    # test controls and both twins sent identically.
    def _shape(rows: list[Message]) -> list[tuple[str, str | None]]:
        return [(row.role, row.meta.get("provider_used")) for row in rows]

    assert _shape(rows_a) == _shape(rows_b)
    user_a = [row.content for row in rows_a if row.role == "user"]
    user_b = [row.content for row in rows_b if row.role == "user"]
    assert user_a == user_b


def _sse_event(body: str, name: str) -> dict[str, Any]:
    """The JSON payload of one named SSE event — ``streaming/sse.py``'s
    ``f"event: {name}\\ndata: {body}\\n\\n"`` framing, read back."""
    for block in body.split("\n\n"):
        if block.startswith(f"event: {name}\n"):
            data_line = block.split("\n", 1)[1]
            parsed: dict[str, Any] = json.loads(data_line.removeprefix("data: "))
            return parsed
    raise AssertionError(f"no {name!r} event found in stream:\n{body}")


# --------------------------------------------------------------------------- #
# Phase 5 Step 3 — D4 under a real history: fitting exercised, truncation
# disclosed (D34). `_shrink_context` forces the fitting step to actually drop
# turns, rather than only unit-testing the algorithm in isolation, and the
# three tests below check the three hops `messages_dropped` takes: the
# response, the stored `meta`, and the `done` event.
# --------------------------------------------------------------------------- #
async def test_a_truncated_answer_discloses_how_much_history_it_dropped(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    app: FastAPI,
    db_session: AsyncSession,
) -> None:
    """A turn built on a partial history says so, on the response and in the
    stored row — the same three-hop disclosure `degraded`/`extraction_tier`
    already got in Phase 4, and D4's own honest-degradation story finally
    reaching the wire.
    """
    headers = _headers(make_jwt)
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

    config = _shrink_context(get_providers_config(), "fast", provider="groq", context_tokens=200)
    handler = provider_fixtures.RecordingHandler(provider_fixtures.load("groq", "success"))
    upstream = handler.client()
    app.state.provider_registry = build_registry(client=upstream, config=config)
    try:
        response = await client.post(
            COMPLETIONS,
            json={
                "model": "fast",
                "max_tokens": 50,
                "conversation_id": str(conversation.id),
                "messages": [{"role": "user", "content": "what did I say first?"}],
            },
            headers=headers,
        )
    finally:
        await upstream.aclose()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["messages_dropped"] > 0

    # Exactly one omission marker reached the wire, however many messages it
    # says were dropped — `_apply_marker` merges rather than appending a second
    # one, and this is the assertion that would catch it not doing so.
    sent = json.dumps(handler.last_json())
    assert sent.count("earlier messages omitted") == 1

    stored = await _messages(db_session, conversation.id)
    assert stored[-1].role == "assistant"
    assert stored[-1].meta["messages_dropped"] == body["messages_dropped"]


async def test_streaming_done_event_discloses_the_same_truncation(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    app: FastAPI,
    db_session: AsyncSession,
) -> None:
    """D34's streaming twin: the `done` event carries the same number the
    non-streaming response would have, and the stored row agrees with it.
    """
    headers = _headers(make_jwt)
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

    config = _shrink_context(get_providers_config(), "fast", provider="groq", context_tokens=200)
    upstream = provider_fixtures.client_streaming(
        [provider_fixtures.read_sse("groq", "stream_success").encode("utf-8")]
    )
    app.state.provider_registry = build_registry(client=upstream, config=config)
    try:
        response = await client.post(
            COMPLETIONS,
            json={
                "model": "fast",
                "max_tokens": 50,
                "conversation_id": str(conversation.id),
                "messages": [{"role": "user", "content": "what did I say first?"}],
                "stream": True,
            },
            headers=headers,
        )
    finally:
        await upstream.aclose()

    assert response.status_code == 200, response.text
    done = _sse_event(response.text, "done")
    assert done["messages_dropped"] > 0

    stored = await _messages(db_session, conversation.id)
    assert stored[-1].role == "assistant"
    assert stored[-1].meta["messages_dropped"] == done["messages_dropped"]


async def test_a_200_message_history_truncates_without_a_provider_error(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    app: FastAPI,
    db_session: AsyncSession,
) -> None:
    """`development-plan.md`'s own exit criterion for this phase: feed a
    200-message history to a small-context model, and the answer comes back
    truncated rather than as a `ContextTooLong` error.
    """
    headers = _headers(make_jwt)
    opener = await client.post(
        COMPLETIONS,
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
        headers=headers,
    )
    assert opener.status_code == 200, opener.text
    conversation_id = UUID(opener.json()["conversation_id"])
    conversation = await db_session.get(Conversation, conversation_id)
    assert conversation is not None
    # The opener above already wrote one user/assistant pair; 99 more pairs
    # brings the stored history to exactly 200 messages.
    await _seed_history(
        db_session, conversation_id=conversation.id, user_id=conversation.user_id, turns=99
    )
    assert len(await _messages(db_session, conversation.id)) == 200

    config = _shrink_context(get_providers_config(), "fast", provider="groq", context_tokens=800)
    handler = provider_fixtures.RecordingHandler(provider_fixtures.load("groq", "success"))
    upstream = handler.client()
    app.state.provider_registry = build_registry(client=upstream, config=config)
    try:
        response = await client.post(
            COMPLETIONS,
            json={
                "model": "fast",
                "max_tokens": 50,
                "conversation_id": str(conversation.id),
                "messages": [{"role": "user", "content": "what did I say first?"}],
            },
            headers=headers,
        )
    finally:
        await upstream.aclose()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["messages_dropped"] > 0
    assert len(handler.requests) == 1


# --------------------------------------------------------------------------- #
# Streaming (Phase 2 Steps 9-10)
# --------------------------------------------------------------------------- #
async def test_streaming_delivers_frames_and_persists_the_answer(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_streaming: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The wiring Step 10 exists to complete: ``stream: true`` now reaches a
    real upstream over real SSE, and refreshing afterwards finds the same two
    rows the non-streaming path would have written — one ``messages`` row named
    with the serving model, one ``requests`` row with the trail."""
    response = await client.post(
        COMPLETIONS,
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=_headers(make_jwt),
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    assert "event: meta" in body
    assert "event: delta" in body
    assert "event: done" in body

    result = await db_session.execute(select(Message).where(Message.role == "assistant"))
    (assistant,) = result.scalars().all()
    assert assistant.meta["provider_used"] == "groq"
    assert assistant.meta["attempts"] == 1

    (row,) = await _requests(db_session)
    assert row.status == "ok"
    assert row.provider == "groq"


async def test_a_pre_stream_failure_is_a_json_envelope_not_a_200(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_script: Callable[..., ScriptedHandler],
    db_session: AsyncSession,
) -> None:
    """D13, on the wire the client actually asked for: a pool that fails before
    a single token exists must stay a debuggable 502, never a 200 that only
    says "failed" once you start reading the body. The `requests` row for it
    is written by the endpoint itself — the collector never sees a turn that
    never sent a byte."""
    handler = groq_script("rate_limited", "rate_limited")

    response = await client.post(
        COMPLETIONS,
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=_headers(make_jwt),
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "rate_limited"
    assert handler.calls == 2

    (row,) = await _requests(db_session)
    assert row.status == "error"
    assert row.error_code == "rate_limited"

    # The user's message survives (committed before the router was ever called,
    # D14) but no assistant row exists — nothing was ever generated to store.
    assert row.conversation_id is not None
    messages = await _messages(db_session, row.conversation_id)
    assert [message.role for message in messages] == ["user"]


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


async def test_asking_for_the_internal_perception_slot_is_a_400(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    db_session: AsyncSession,
) -> None:
    """Phase 4 Step 1 (D26): `perception` is routable — `registry.candidates()`
    resolves it, since the extraction lane calls it by name — but a client
    naming it explicitly gets the same 400 a typo gets, never a real request."""
    response = await client.post(
        COMPLETIONS,
        json={"model": "perception", "messages": [{"role": "user", "content": "hi"}]},
        headers=_headers(make_jwt),
    )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "unknown_slot"
    assert "perception" not in error["details"]["available"]
    assert await _requests(db_session) == []


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


# --------------------------------------------------------------------------- #
# Phase 4 Step 4 — `file_refs` on a chat turn
# --------------------------------------------------------------------------- #
async def test_a_valid_file_ref_stores_a_two_block_message_then_fails_loudly(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    no_perception: None,
    db_session: AsyncSession,
) -> None:
    """The whole path from upload to stored `file_ref` block works, and with
    the lane switched off the turn then fails *loudly*: render step 1's default
    `NoAttachments` resolver raises, because a history that references a file it
    cannot show the model must never be sent to one silently.

    Step 8 gave the resolver something real to do, so the failure here is now
    the ``PERCEPTION_ENABLED`` escape hatch rather than an unbuilt seam — which
    is the more useful assertion anyway, since that switch is what a deploy
    reaches for when the lane itself is what is being debugged.
    """
    headers = _headers(make_jwt)
    file_hash = await _upload(client, headers)

    response = await client.post(
        COMPLETIONS,
        json={
            "messages": [
                {"role": "user", "content": "what does this say?", "file_refs": [file_hash]}
            ]
        },
        headers=headers,
    )

    # Unhandled: `NoAttachments.resolve` raises `NotImplementedError`, which is
    # not an `AppError`, so it lands in the catch-all handler as a generic 500.
    # A 500 rather than a tidy message is deliberate — it is our bug or our
    # switch, never something the caller can act on.
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"

    result = await db_session.execute(select(Message).where(Message.role == "user"))
    rows = list(result.scalars().all())
    assert len(rows) == 1
    assert rows[0].content == [
        {"type": "text", "text": "what does this say?"},
        {
            "type": "file_ref",
            "file_hash": file_hash,
            "filename": "report.pdf",
            "mime": "application/pdf",
            "bytes": len(PDF_BYTES),
        },
    ]


async def test_multiple_file_refs_on_one_message_all_land_after_the_text(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    no_perception: None,
    db_session: AsyncSession,
) -> None:
    headers = _headers(make_jwt)
    first_hash = await _upload(client, headers)
    second_response = await client.post(
        FILES,
        files={"file": ("chart.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png")},
        headers=headers,
    )
    assert second_response.status_code == 201
    second_hash = second_response.json()["file_hash"]

    response = await client.post(
        COMPLETIONS,
        json={
            "messages": [
                {
                    "role": "user",
                    "content": "compare these",
                    "file_refs": [first_hash, second_hash],
                }
            ]
        },
        headers=headers,
    )

    assert response.status_code == 500

    result = await db_session.execute(select(Message).where(Message.role == "user"))
    rows = list(result.scalars().all())
    assert len(rows) == 1
    content = rows[0].content
    assert content[0] == {"type": "text", "text": "compare these"}
    assert {block["file_hash"] for block in content[1:]} == {first_hash, second_hash}


async def test_an_unowned_file_hash_is_a_404_and_writes_nothing(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    db_session: AsyncSession,
) -> None:
    """D24: ownership is checked once, before any message is written. Bob's
    hash is a 404, never a 403 — the same rule `conversations` follows — and
    Alice's attempt to reference it leaves no trace."""
    alice = _headers(make_jwt, sub=uuid4(), email="alice@example.com")
    bob = _headers(make_jwt, sub=uuid4(), email="bob@example.com")
    bobs_hash = await _upload(client, bob)

    response = await client.post(
        COMPLETIONS,
        json={"messages": [{"role": "user", "content": "read this", "file_refs": [bobs_hash]}]},
        headers=alice,
    )

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "file_not_found"
    assert error["details"]["file_hash"] == bobs_hash

    result = await db_session.execute(select(Message))
    assert list(result.scalars().all()) == []


async def test_a_malformed_file_hash_is_a_422(
    client: httpx.AsyncClient, make_jwt: TokenFactory
) -> None:
    """A hash that does not match the pattern never reaches the database."""
    response = await client.post(
        COMPLETIONS,
        json={"messages": [{"role": "user", "content": "hi", "file_refs": ["not-a-real-hash"]}]},
        headers=_headers(make_jwt),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


async def test_more_than_four_file_refs_is_a_422(
    client: httpx.AsyncClient, make_jwt: TokenFactory
) -> None:
    five_hashes = [hashlib.sha256(str(n).encode()).hexdigest() for n in range(5)]
    response = await client.post(
        COMPLETIONS,
        json={"messages": [{"role": "user", "content": "hi", "file_refs": five_hashes}]},
        headers=_headers(make_jwt),
    )
    assert response.status_code == 422
