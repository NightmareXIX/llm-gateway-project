"""D6/D47 end to end: ``Idempotency-Key`` on ``POST /v1/chat/completions``.

Phase 7 Step 5 built the store and unit-tested it against ``fakeredis``. This
module is the wiring, which is where the decision actually lives: *when* the
claim is issued (before the conversation is resolved), *who* completes it (the
endpoint on a non-streaming turn, the collector on a streamed one), and *what
happens when nothing goes right* — a failure that must release, a Redis that is
down, a key reused for a different question.

**Every request here sets ``temperature`` away from zero on purpose.** D19's
exact cache would otherwise answer the second call itself, and "one provider
call" would pass for a reason that has nothing to do with idempotency. The one
test that *wants* both mechanisms at once says so in its own name.

The last test in the file is the important one for everybody who never sends the
header: a request without it must behave exactly as it did before this step
existed, asserted against a body spelled out in full rather than against a
subset that could quietly shrink.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import keys
from app.cache.idempotency import (
    IDEMPOTENCY_HEADER,
    IN_FLIGHT,
    REPLAY_HEADER,
    IdempotencyEnvelope,
    IdempotencyStore,
    fingerprint,
)
from app.config import ProvidersConfig, get_providers_config
from app.db.models import Conversation, Message, Request
from app.deps import get_idempotency_store
from app.providers.registry import build_registry
from app.schemas.chat import ChatCompletionRequest
from tests import provider_fixtures
from tests.conftest import TokenFactory
from tests.provider_fixtures import ScriptedHandler

pytestmark = pytest.mark.integration

COMPLETIONS = "/v1/chat/completions"

USER_ID = UUID("11111111-2222-3333-4444-555555555555")
"""Pinned rather than generated, so a test can build the Redis key — and the
fingerprint stored under it — before the request that would create it."""


def _groq_only() -> ProvidersConfig:
    """The committed table with the other two providers switched off, the same
    narrowing ``test_chat_endpoint.py`` uses and for the same reason: these
    tests assert on how many calls left the process, which is a statement about
    idempotency and not about how long the committed candidate chain is."""
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


@pytest.fixture
async def groq_script(app: FastAPI) -> AsyncIterator[Callable[..., ScriptedHandler]]:
    """Install a handler answering a fixed sequence, replaceable mid-test.

    The release tests need a first request that fails and a second, under the
    same key, that succeeds — which is two different scripts against one app.
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


class StreamCounter:
    """A streaming upstream that counts how many times it was actually reached.

    ``provider_fixtures.client_streaming`` repeats one body forever and keeps no
    tally, and the tally is the whole assertion here: a replayed stream must
    reach no provider at all.
    """

    def __init__(self, body: bytes) -> None:
        self._body = body
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(200, stream=provider_fixtures.ScriptedByteStream([self._body]))


@pytest.fixture
async def groq_streaming(app: FastAPI) -> AsyncIterator[StreamCounter]:
    counter = StreamCounter(provider_fixtures.read_sse("groq", "stream_success").encode("utf-8"))
    client = provider_fixtures.client_from(counter)
    app.state.provider_registry = build_registry(client=client, config=_groq_only())
    try:
        yield counter
    finally:
        await client.aclose()


def _headers(make_jwt: TokenFactory, key: str | None = None, **kwargs: Any) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {make_jwt(sub=USER_ID, **kwargs)}"}
    if key is not None:
        headers[IDEMPOTENCY_HEADER] = key
    return headers


def _ask(**overrides: Any) -> dict[str, Any]:
    """One turn, deliberately uncacheable (``temperature`` != 0, D19)."""
    return {
        "model": "fast",
        "messages": [{"role": "user", "content": "what is a gateway?"}],
        "temperature": 0.7,
        **overrides,
    }


async def _requests(session: AsyncSession) -> list[Request]:
    result = await session.execute(select(Request).order_by(Request.created_at, Request.id))
    return list(result.scalars().all())


async def _messages(session: AsyncSession) -> list[Message]:
    result = await session.execute(select(Message).order_by(Message.seq))
    return list(result.scalars().all())


async def _quota_counters(redis: FakeRedis) -> dict[str, Any]:
    """Every quota counter Redis currently holds, so a replay can be asserted to
    move none of them (D47: a replay spends nothing)."""
    found: dict[str, Any] = {}
    for key in sorted(str(raw) for raw in await redis.keys("q:*")):
        found[key] = await redis.get(key)
    return found


def _events(body: str) -> list[str]:
    """The event names of an SSE body, in order."""
    return [
        block.split("\n", 1)[0].removeprefix("event: ")
        for block in body.split("\n\n")
        if block.startswith("event: ")
    ]


def _event(body: str, name: str) -> dict[str, Any]:
    """The JSON payload of one named SSE event — ``streaming/sse.py``'s framing
    read back, the same shape ``test_chat_endpoint.py`` reads it in."""
    for block in body.split("\n\n"):
        if block.startswith(f"event: {name}\n"):
            parsed: dict[str, Any] = json.loads(block.split("\n", 1)[1].removeprefix("data: "))
            return parsed
    raise AssertionError(f"no {name!r} event found in stream:\n{body}")


def _streamed_text(body: str) -> str:
    """Every ``delta`` event's content, reassembled — OpenAI's
    ``choices[].delta.content`` shape, which is what ``DeltaEvent`` emits."""
    pieces = []
    for block in body.split("\n\n"):
        if block.startswith("event: delta\n"):
            event = json.loads(block.split("\n", 1)[1].removeprefix("data: "))
            pieces.append(event["choices"][0]["delta"]["content"])
    return "".join(pieces)


# --------------------------------------------------------------------------- #
# The headline case
# --------------------------------------------------------------------------- #
async def test_the_same_key_twice_answers_once(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    db_session: AsyncSession,
    redis_client: FakeRedis,
) -> None:
    """D6 in one test: identical body, one provider call, one stored turn.

    The second call is a *replay*, not a second answer — so it discloses itself
    (``X-Idempotent-Replay``), writes a ``requests`` row on its own ``replayed``
    axis with no provider on it, appends no message, and spends no quota.
    """
    headers = _headers(make_jwt, key="key-alpha")

    first = await client.post(COMPLETIONS, json=_ask(), headers=headers)
    assert first.status_code == 200, first.text
    assert REPLAY_HEADER not in first.headers
    spent = await _quota_counters(redis_client)
    assert spent, "the first turn should have moved at least one quota counter"

    second = await client.post(COMPLETIONS, json=_ask(), headers=headers)

    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert second.headers[REPLAY_HEADER] == "true"
    # The body is the *original's*, id included — which is exactly why it is not
    # this request's own id. A client comparing the two bodies must find them
    # identical, and a request id that changed would break that first.
    assert second.json()["id"] != second.headers["x-request-id"]
    assert groq.requests and len(groq.requests) == 1

    # Nothing about the conversation moved: the claim is checked before
    # `_resolve_conversation` (trap 2), so a replay cannot open a thread, append
    # a turn, or touch `preferred_slot`.
    conversations = (await db_session.execute(select(Conversation))).scalars().all()
    assert len(conversations) == 1
    assert [message.role for message in await _messages(db_session)] == ["user", "assistant"]
    assert await _quota_counters(redis_client) == spent

    # Selected by status rather than by position: both rows share the test
    # transaction's `now()`, so `created_at` cannot order them.
    rows = {row.status: row for row in await _requests(db_session)}
    assert set(rows) == {"ok", "replayed"}
    original, replay = rows["ok"], rows["replayed"]
    assert original.status == "ok"
    assert original.provider == "groq"
    assert replay.status == "replayed"
    assert replay.provider is None
    assert replay.model is None
    assert replay.cache_hit is False
    assert replay.requested_slot == "fast"
    assert replay.conversation_id == conversations[0].id


async def test_the_same_key_with_a_different_body_is_refused(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    db_session: AsyncSession,
) -> None:
    """Stripe's semantics (D47): silently answering a different question under a
    reused key is the failure this check exists to prevent."""
    headers = _headers(make_jwt, key="key-beta")

    first = await client.post(COMPLETIONS, json=_ask(), headers=headers)
    assert first.status_code == 200, first.text

    second = await client.post(
        COMPLETIONS,
        json=_ask(messages=[{"role": "user", "content": "a completely different question"}]),
        headers=headers,
    )

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_key_reuse"
    assert len(groq.requests) == 1
    # A refusal is not a replay: no row is written for a request that was never
    # served, and certainly not one claiming an answer was handed back.
    assert [row.status for row in await _requests(db_session)] == ["ok"]


async def test_a_duplicate_arriving_while_the_first_is_in_flight_is_refused(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    redis_client: FakeRedis,
) -> None:
    """The concurrent-retry case, driven deterministically.

    Two genuinely simultaneous requests are what ``SET NX`` protects against,
    and ``tests/unit/test_idempotency.py`` proves that with ``asyncio.gather``
    against the store itself. Here the *endpoint's* half is what is under test —
    that an existing ``in_flight`` envelope becomes a 409 carrying
    ``Retry-After`` rather than a second provider call — so the winning claim is
    written by hand instead of raced for, which is the same state and none of
    the flakiness.
    """
    body = _ask()
    stored = IdempotencyEnvelope(
        state=IN_FLIGHT,
        fingerprint=fingerprint(ChatCompletionRequest.model_validate(body), user_id=USER_ID),
        request_id=str(uuid4()),
        stream=False,
    )
    await redis_client.set(keys.idempotency(USER_ID, "key-gamma"), stored.to_json())

    response = await client.post(
        COMPLETIONS, json=body, headers=_headers(make_jwt, key="key-gamma")
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_in_flight"
    assert response.headers["retry-after"] == "1"
    assert groq.requests == []


# --------------------------------------------------------------------------- #
# Trap 3 — a failure gives the key back
# --------------------------------------------------------------------------- #
async def test_a_failed_turn_releases_its_key_and_the_retry_really_runs(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_script: Callable[..., ScriptedHandler],
    redis_client: FakeRedis,
    db_session: AsyncSession,
) -> None:
    """Trap 3, which is the whole reason ``release`` exists: a 502 that left an
    ``in_flight`` envelope behind would lock that key for a day and answer the
    client's retry — the exact thing D6 exists to serve — with a 409.

    ``bad_request`` rather than ``rate_limited`` for the failure: a ``429`` opens
    the circuit breaker, and the retry below would then be skipped before it ever
    reached the upstream this test needs it to reach. A ``BadRequest`` is neither
    failover-eligible nor breaker-eligible, so it fails exactly once and leaves
    nothing behind but the released key.
    """
    headers = _headers(make_jwt, key="key-delta")
    failing = groq_script("bad_request")

    failure = await client.post(COMPLETIONS, json=_ask(), headers=headers)

    assert failure.status_code >= 400
    assert failing.calls == 1
    assert await redis_client.get(keys.idempotency(USER_ID, "key-delta")) is None

    succeeding = groq_script("success")
    retry = await client.post(COMPLETIONS, json=_ask(), headers=headers)

    assert retry.status_code == 200, retry.text
    assert REPLAY_HEADER not in retry.headers
    assert succeeding.calls == 1
    # Sorted rather than ordered: both rows share the transaction's `now()`, so
    # `created_at` cannot separate them.
    assert sorted(row.status for row in await _requests(db_session)) == ["error", "ok"]


# --------------------------------------------------------------------------- #
# The streaming twin (D5's replay machinery, reused)
# --------------------------------------------------------------------------- #
async def test_a_streamed_turn_replays_as_a_stream(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_streaming: StreamCounter,
    db_session: AsyncSession,
) -> None:
    """The claim is completed by the collector, long after the endpoint returned,
    and the replay is framed identically to the live stream — same events, same
    ``served_by`` — because it goes through the very machinery D5 built for cache
    hits."""
    headers = _headers(make_jwt, key="key-epsilon")

    live = await client.post(COMPLETIONS, json=_ask(stream=True), headers=headers)
    assert live.status_code == 200, live.text
    assert groq_streaming.calls == 1

    replay = await client.post(COMPLETIONS, json=_ask(stream=True), headers=headers)

    assert replay.status_code == 200
    assert replay.headers["content-type"].startswith("text/event-stream")
    assert replay.headers[REPLAY_HEADER] == "true"
    assert groq_streaming.calls == 1

    assert _events(replay.text) == _events(live.text)
    assert _event(replay.text, "done")["served_by"] == _event(live.text, "done")["served_by"]
    assert _streamed_text(replay.text) == _streamed_text(live.text)

    # One assistant row, from the live turn only: a replay writes no message.
    assert [message.role for message in await _messages(db_session)] == ["user", "assistant"]
    assert sorted(row.status for row in await _requests(db_session)) == ["ok", "replayed"]


async def test_a_streamed_failure_releases_its_key(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_script: Callable[..., ScriptedHandler],
    redis_client: FakeRedis,
) -> None:
    """D13's pre-first-byte exhaustion never reaches the collector — it raises
    out of the orchestrator and the endpoint's own ``except`` releases it, which
    is the path the wrapper's blanket handler exists to cover."""
    groq_script("bad_request")

    response = await client.post(
        COMPLETIONS, json=_ask(stream=True), headers=_headers(make_jwt, key="key-zeta")
    )

    assert response.status_code >= 400
    assert await redis_client.get(keys.idempotency(USER_ID, "key-zeta")) is None


async def test_a_replay_counts_on_its_own_axis_in_the_metrics(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
) -> None:
    """Trap 18's other half, at the metrics end rather than the SQL end: a
    replay is neither a success nor a failure, so the fifth facade counts it
    under its own ``status`` with no provider to attribute it to — and the
    duration histogram, which only times turns a provider actually served,
    stays where it was."""
    headers = _headers(make_jwt, key="key-kappa")
    await client.post(COMPLETIONS, json=_ask(), headers=headers)
    await client.post(COMPLETIONS, json=_ask(), headers=headers)

    metrics = await client.get("/metrics")

    assert (
        'gateway_requests_total{provider="unknown",model="unknown",'
        'status="replayed",key_pool="none"} 1' in metrics.text
    )
    assert 'gateway_request_duration_ms_count{provider="groq",mode="complete"} 1' in metrics.text


# --------------------------------------------------------------------------- #
# Trap 11 — `X-Cache` and `X-Idempotent-Replay` are different facts
# --------------------------------------------------------------------------- #
def _cacheable(**overrides: Any) -> dict[str, Any]:
    """The one shape in this module D19 *will* cache: ``temperature`` at zero."""
    return _ask(temperature=0.0, **overrides)


async def test_a_replayed_cache_hit_carries_both_headers(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
) -> None:
    """Two mechanisms, two headers, and neither collapses into the other.

    ``X-Cache`` says where the answer being handed back originally came from;
    ``X-Idempotent-Replay`` says this particular call computed nothing at all.
    A replay of a turn that was itself a cache hit is true on both counts, which
    is only expressible because the envelope remembers the original's ``X-Cache``
    rather than inventing one at replay time.
    """
    warmed = await client.post(COMPLETIONS, json=_cacheable(), headers=_headers(make_jwt))
    assert warmed.headers["x-cache"] == "MISS", warmed.text

    headers = _headers(make_jwt, key="key-theta")
    hit = await client.post(COMPLETIONS, json=_cacheable(), headers=headers)
    assert hit.status_code == 200, hit.text
    assert hit.headers["x-cache"] == "HIT"
    assert REPLAY_HEADER not in hit.headers

    replay = await client.post(COMPLETIONS, json=_cacheable(), headers=headers)

    assert replay.status_code == 200, replay.text
    assert replay.headers["x-cache"] == "HIT"
    assert replay.headers[REPLAY_HEADER] == "true"
    assert replay.json() == hit.json()
    # One provider call across all three: the cache answered the second, and
    # idempotency answered the third.
    assert len(groq.requests) == 1


async def test_a_streamed_cache_hit_still_completes_its_claim(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_streaming: StreamCounter,
    db_session: AsyncSession,
) -> None:
    """The path with the most ways to drop a ticket: a streamed turn served from
    the cache never reaches ``Collector.persist``, so the claim has to be
    completed by ``persist_cache_hit`` instead. Getting that wrong leaves an
    ``in_flight`` envelope behind and answers the retry with a 409 — which is
    exactly what this asserts does not happen."""
    warmed = await client.post(
        COMPLETIONS, json=_cacheable(stream=True), headers=_headers(make_jwt)
    )
    assert warmed.status_code == 200, warmed.text
    assert groq_streaming.calls == 1

    headers = _headers(make_jwt, key="key-iota")
    hit = await client.post(COMPLETIONS, json=_cacheable(stream=True), headers=headers)
    assert hit.headers["x-cache"] == "HIT"
    assert REPLAY_HEADER not in hit.headers

    replay = await client.post(COMPLETIONS, json=_cacheable(stream=True), headers=headers)

    assert replay.status_code == 200
    assert replay.headers[REPLAY_HEADER] == "true"
    assert replay.headers["x-cache"] == "HIT"
    assert _events(replay.text) == _events(hit.text)
    assert _streamed_text(replay.text) == _streamed_text(hit.text)
    assert groq_streaming.calls == 1
    assert [row.status for row in await _requests(db_session)].count("replayed") == 1


# --------------------------------------------------------------------------- #
# Fail open, and the header itself
# --------------------------------------------------------------------------- #
class BoomRedis:
    """A Redis whose every call raises — D47's fail-open rule under test."""

    async def set(self, *args: Any, **kwargs: Any) -> None:
        raise ConnectionError("redis is down")

    async def get(self, *args: Any, **kwargs: Any) -> None:
        raise ConnectionError("redis is down")

    async def delete(self, *args: Any, **kwargs: Any) -> None:
        raise ConnectionError("redis is down")


async def test_a_dead_redis_serves_both_requests_rather_than_neither(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    app: FastAPI,
) -> None:
    """Caching's rule, not quota's (D15): nothing is being *spent* by proceeding,
    so answering twice beats refusing to answer at all. Idempotency silently
    switches itself off and the client sees today's behaviour."""
    app.dependency_overrides[get_idempotency_store] = lambda: IdempotencyStore(BoomRedis())  # type: ignore[arg-type]
    headers = _headers(make_jwt, key="key-eta")

    first = await client.post(COMPLETIONS, json=_ask(), headers=headers)
    second = await client.post(COMPLETIONS, json=_ask(), headers=headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert REPLAY_HEADER not in second.headers
    assert len(groq.requests) == 2


@pytest.mark.parametrize(
    "key",
    ["", "a" * 256, "has a space", "tab\there"],
    ids=["empty", "too-long", "whitespace", "control"],
)
async def test_an_unusable_key_is_a_400_before_anything_happens(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    key: str,
) -> None:
    """A clear 400 rather than the ``ValueError`` ``keys._segment`` would raise —
    it is the client's header, so it is the client's mistake."""
    response = await client.post(COMPLETIONS, json=_ask(), headers=_headers(make_jwt, key=key))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_idempotency_key"
    assert groq.requests == []


# --------------------------------------------------------------------------- #
# The regression guard: no header, no change
# --------------------------------------------------------------------------- #
async def test_a_request_with_no_key_is_unchanged_by_this_step(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq: provider_fixtures.RecordingHandler,
    db_session: AsyncSession,
) -> None:
    """The property Step 6 is judged on, asserted against a body spelled out in
    full rather than a subset that could quietly shrink.

    Two identical uncacheable requests without the header make two provider
    calls and produce two turns, exactly as they did before idempotency existed;
    no response carries the replay header, and no ``replayed`` row appears.
    """
    headers = _headers(make_jwt)

    response = await client.post(COMPLETIONS, json=_ask(), headers=headers)

    assert response.status_code == 200, response.text
    assert REPLAY_HEADER not in response.headers
    assert response.headers["x-cache"] == "BYPASS"
    body = response.json()
    assert set(body) == {
        "id",
        "object",
        "created",
        "model",
        "choices",
        "usage",
        "served_by",
        "requested_slot",
        "substituted",
        "attempts",
        "degraded",
        "extraction_tier",
        "messages_dropped",
        "warning",
        "key_pool",
        "conversation_id",
        "message_id",
    }
    assert body["id"] == response.headers["x-request-id"]
    assert body["served_by"] == {
        "slot": "fast",
        "provider": "groq",
        "model": "openai/gpt-oss-20b",
    }
    assert body["usage"] == {
        "prompt_tokens": 48,
        "completion_tokens": 27,
        "total_tokens": 75,
        "estimated": False,
    }
    assert body["key_pool"] == "shared"

    again = await client.post(COMPLETIONS, json=_ask(), headers=headers)

    assert again.status_code == 200, again.text
    assert again.json()["conversation_id"] != body["conversation_id"]
    assert len(groq.requests) == 2
    assert [row.status for row in await _requests(db_session)] == ["ok", "ok"]
