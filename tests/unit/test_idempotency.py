"""``app/cache/idempotency.py`` — D6/D47's claim-and-replay store.

Four things worth a file of their own: the envelope's round trip (and its
tolerance for a key it does not know), the fingerprint's sensitivity to every
field the answer depends on — ``stream`` included, which is the one a reader
forgets — the four claim outcomes of D47's table, and the fail-open behaviour
that makes a Redis outage a degradation rather than a 500.

The concurrency test is the one that proves the design rather than the code:
two identical claims issued together must produce exactly one ``Claimed``,
which is true only because the claim is a single ``SET NX`` and not a
``GET``-then-``SET``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from fakeredis.aioredis import FakeRedis

from app.cache import keys
from app.cache.idempotency import (
    DONE,
    IDEMPOTENCY_HEADER,
    IN_FLIGHT,
    MAX_KEY_LENGTH,
    REPLAY_HEADER,
    Claimed,
    FingerprintMismatch,
    IdempotencyEnvelope,
    IdempotencyStore,
    IdempotentRequest,
    InFlight,
    Replay,
    fingerprint,
)
from app.schemas.chat import ChatCompletionRequest, InputMessage

USER_ID = UUID("11111111-1111-1111-1111-111111111111")
OTHER_USER_ID = UUID("22222222-2222-2222-2222-222222222222")
KEY = "client-chosen-key-001"
REQUEST_ID = "req_0123456789"


# --------------------------------------------------------------------------- #
# A request body, structurally
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Message:
    role: str = "user"
    content: str = "what does this gateway do?"
    file_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Body:
    """Satisfies :class:`IdempotentRequest` structurally, which is the whole
    point of the protocol: ``cache/`` never imports the chat schemas."""

    model: str = "auto"
    messages: list[_Message] = field(default_factory=lambda: [_Message()])
    conversation_id: UUID | None = None
    temperature: float = 0.0
    max_tokens: int | None = None
    top_p: float | None = None
    stop: list[str] = field(default_factory=list)
    stream: bool = False


def _fp(body: IdempotentRequest | None = None, *, user_id: UUID = USER_ID) -> str:
    return fingerprint(body if body is not None else _Body(), user_id=user_id)


# --------------------------------------------------------------------------- #
# The envelope
# --------------------------------------------------------------------------- #
def test_the_envelope_round_trips() -> None:
    envelope = IdempotencyEnvelope(
        state=DONE,
        fingerprint="a" * 64,
        request_id=REQUEST_ID,
        stream=True,
        response={"id": REQUEST_ID, "object": "chat.completion"},
    )

    assert IdempotencyEnvelope.from_json(envelope.to_json()) == envelope


def test_an_in_flight_envelope_carries_no_response() -> None:
    envelope = IdempotencyEnvelope(
        state=IN_FLIGHT, fingerprint="a" * 64, request_id=REQUEST_ID, stream=False
    )

    restored = IdempotencyEnvelope.from_json(envelope.to_json())

    assert restored.response is None
    assert restored.state == IN_FLIGHT


def test_the_envelope_is_written_in_full_including_defaults() -> None:
    """``MessageMeta.to_jsonb``'s rule: a reader never has to know which version
    wrote a row to know whether a missing key means ``null`` or "not recorded"."""
    raw = json.loads(
        IdempotencyEnvelope(
            state=IN_FLIGHT, fingerprint="a" * 64, request_id=REQUEST_ID, stream=False
        ).to_json()
    )

    assert set(raw) == {"state", "fingerprint", "request_id", "stream", "response", "cache_status"}


def test_an_unknown_key_is_ignored_rather_than_fatal() -> None:
    raw = json.dumps(
        {
            "state": DONE,
            "fingerprint": "a" * 64,
            "request_id": REQUEST_ID,
            "stream": False,
            "response": None,
            "something_a_later_phase_added": 7,
        }
    )

    assert IdempotencyEnvelope.from_json(raw).request_id == REQUEST_ID


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("[]", id="not-an-object"),
        pytest.param(
            json.dumps({"state": "wat", "fingerprint": "a", "request_id": "r"}), id="state"
        ),
        pytest.param(
            json.dumps({"state": DONE, "fingerprint": 7, "request_id": "r"}), id="fingerprint"
        ),
        pytest.param(
            json.dumps({"state": DONE, "fingerprint": "a", "request_id": 7}), id="request_id"
        ),
        pytest.param(
            json.dumps({"state": DONE, "fingerprint": "a", "request_id": "r", "stream": "yes"}),
            id="stream",
        ),
        pytest.param(
            json.dumps({"state": DONE, "fingerprint": "a", "request_id": "r", "response": "text"}),
            id="response",
        ),
    ],
)
def test_a_wrong_type_raises_rather_than_degrading(raw: str) -> None:
    """Lenient about absence, never about shape — the same asymmetry
    ``MessageMeta.from_jsonb`` draws."""
    with pytest.raises(ValueError):
        IdempotencyEnvelope.from_json(raw)


# --------------------------------------------------------------------------- #
# fingerprint
# --------------------------------------------------------------------------- #
def test_the_same_body_fingerprints_identically() -> None:
    assert _fp(_Body()) == _fp(_Body())


def test_a_real_chat_request_satisfies_the_protocol() -> None:
    """The protocol exists so ``cache/`` need not import ``schemas/``; if the
    real body ever stops satisfying it, this fails (and so does mypy)."""
    body: IdempotentRequest = ChatCompletionRequest(
        messages=[InputMessage(content="hello")],
    )

    assert len(fingerprint(body, user_id=USER_ID)) == 64


@pytest.mark.parametrize(
    "changed",
    [
        pytest.param(_Body(model="fast"), id="slot"),
        pytest.param(_Body(messages=[_Message(content="a different question")]), id="content"),
        pytest.param(_Body(messages=[_Message(role="system"), _Message()]), id="messages"),
        pytest.param(_Body(messages=[_Message(file_refs=["b" * 64])]), id="file_refs"),
        pytest.param(_Body(conversation_id=uuid4()), id="conversation_id"),
        pytest.param(_Body(temperature=0.5), id="temperature"),
        pytest.param(_Body(max_tokens=256), id="max_tokens"),
        pytest.param(_Body(top_p=0.9), id="top_p"),
        pytest.param(_Body(stop=["\n\n"]), id="stop"),
        pytest.param(_Body(stream=True), id="stream"),
    ],
)
def test_every_field_the_answer_depends_on_changes_the_fingerprint(changed: _Body) -> None:
    assert _fp(changed) != _fp(_Body())


def test_the_user_is_folded_in() -> None:
    assert _fp(user_id=OTHER_USER_ID) != _fp(user_id=USER_ID)


def test_streaming_and_non_streaming_are_different_questions() -> None:
    """Named on its own because it is the one a reader forgets: a streaming
    retry replaying a non-streaming body hands SSE a JSON object."""
    assert _fp(_Body(stream=True)) != _fp(_Body(stream=False))


# --------------------------------------------------------------------------- #
# The four claim outcomes (D47's table)
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(redis_client: FakeRedis) -> IdempotencyStore:
    return IdempotencyStore(redis_client)


async def _claim(
    store: IdempotencyStore,
    *,
    fp: str | None = None,
    request_id: str = REQUEST_ID,
    stream: bool = False,
    key: str = KEY,
    user_id: UUID = USER_ID,
) -> Any:
    return await store.claim(
        key,
        user_id=user_id,
        fingerprint=fp if fp is not None else _fp(),
        request_id=request_id,
        stream=stream,
    )


async def _complete(
    store: IdempotencyStore,
    *,
    fp: str | None = None,
    response: dict[str, Any] | None = None,
    stream: bool = False,
    key: str = KEY,
) -> None:
    await store.complete(
        key,
        user_id=USER_ID,
        fingerprint=fp if fp is not None else _fp(),
        request_id=REQUEST_ID,
        stream=stream,
        response=response if response is not None else {"id": REQUEST_ID},
    )


async def test_a_fresh_key_is_claimed(store: IdempotencyStore) -> None:
    assert await _claim(store) == Claimed()


async def test_a_second_claim_while_in_flight_is_in_flight(store: IdempotencyStore) -> None:
    await _claim(store)

    result = await _claim(store, request_id="req_the_retry")

    assert isinstance(result, InFlight)
    assert result.envelope.request_id == REQUEST_ID, "the envelope names the *holder*"


async def test_a_completed_key_replays_and_the_envelope_round_trips(
    store: IdempotencyStore,
) -> None:
    body = {"id": REQUEST_ID, "choices": [{"index": 0, "finish_reason": "stop"}]}
    await _claim(store)
    await _complete(store, response=body)

    result = await _claim(store, request_id="req_the_retry")

    assert isinstance(result, Replay)
    assert result.envelope.state == DONE
    assert result.envelope.response == body
    assert result.envelope.request_id == REQUEST_ID


@pytest.mark.parametrize("complete_first", [False, True], ids=["in-flight", "done"])
async def test_a_different_body_under_the_same_key_is_a_mismatch(
    store: IdempotencyStore, complete_first: bool
) -> None:
    """Stripe's semantics, in both states — answering a different question under
    a reused key is the failure this check exists to prevent."""
    await _claim(store)
    if complete_first:
        await _complete(store)

    result = await _claim(store, fp=_fp(_Body(model="fast")))

    assert isinstance(result, FingerprintMismatch)


async def test_a_streaming_retry_of_a_non_streaming_answer_is_a_mismatch(
    store: IdempotencyStore,
) -> None:
    """The fingerprint's ``stream`` field doing its job through the store."""
    await _claim(store, fp=_fp(_Body(stream=False)))
    await _complete(store, fp=_fp(_Body(stream=False)))

    result = await _claim(store, fp=_fp(_Body(stream=True)), stream=True)

    assert isinstance(result, FingerprintMismatch)


async def test_release_makes_the_key_claimable_again(store: IdempotencyStore) -> None:
    """Trap 3: a failed request must not lock its key for a day."""
    await _claim(store)
    await store.release(KEY, user_id=USER_ID)

    assert await _claim(store, request_id="req_the_retry") == Claimed()


async def test_one_users_key_cannot_collide_with_anothers(store: IdempotencyStore) -> None:
    await _claim(store, user_id=USER_ID)

    assert await _claim(store, user_id=OTHER_USER_ID, fp=_fp(user_id=OTHER_USER_ID)) == Claimed()


async def test_the_claim_uses_the_frozen_key_builder(
    store: IdempotencyStore, redis_client: FakeRedis
) -> None:
    await _claim(store)

    assert await redis_client.get(f"idem:{USER_ID}:{KEY}") is not None
    assert await redis_client.get(keys.idempotency(USER_ID, KEY)) is not None


@pytest.mark.parametrize("complete_first", [False, True], ids=["claim", "complete"])
async def test_the_ttl_is_the_frozen_one(
    store: IdempotencyStore, redis_client: FakeRedis, complete_first: bool
) -> None:
    await _claim(store)
    if complete_first:
        await _complete(store)

    ttl = await redis_client.ttl(keys.idempotency(USER_ID, KEY))

    assert 0 < ttl <= keys.IDEMPOTENCY_TTL_S


async def test_concurrent_claims_produce_exactly_one_winner(store: IdempotencyStore) -> None:
    """The ``NX`` is the design. A ``GET``-then-``SET`` would let every one of
    these through, and D6 would have bought nothing."""
    results = await asyncio.gather(
        *(_claim(store, request_id=f"req_{index}") for index in range(8))
    )

    assert sum(isinstance(result, Claimed) for result in results) == 1
    assert all(isinstance(result, Claimed | InFlight) for result in results)


# --------------------------------------------------------------------------- #
# Fails open (Contract C — caching's rule, not quota's)
# --------------------------------------------------------------------------- #
def _break(monkeypatch: pytest.MonkeyPatch, store: IdempotencyStore, command: str) -> None:
    async def broken(*_args: Any, **_kwargs: Any) -> None:
        raise ConnectionError("redis is gone")

    monkeypatch.setattr(store._redis, command, broken)


async def test_a_claim_against_a_dead_redis_proceeds(
    store: IdempotencyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break(monkeypatch, store, "set")

    assert await _claim(store) == Claimed()


async def test_a_failed_collision_read_proceeds(
    store: IdempotencyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _claim(store)
    _break(monkeypatch, store, "get")

    assert await _claim(store) == Claimed()


async def test_a_complete_failure_is_swallowed(
    store: IdempotencyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _claim(store)
    _break(monkeypatch, store, "set")

    # Must not raise: the request itself succeeded.
    await _complete(store)


async def test_a_release_failure_is_swallowed(
    store: IdempotencyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _claim(store)
    _break(monkeypatch, store, "delete")

    # Must not raise: this is already a failure path.
    await store.release(KEY, user_id=USER_ID)


async def test_a_corrupt_envelope_is_treated_as_claimable(
    store: IdempotencyStore, redis_client: FakeRedis
) -> None:
    await redis_client.set(keys.idempotency(USER_ID, KEY), json.dumps({"unexpected": "shape"}))

    assert await _claim(store) == Claimed()


async def test_a_key_that_vanished_between_the_set_and_the_get_is_claimable(
    store: IdempotencyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The holder released, or the TTL expired, in the microsecond between the
    two commands. Nobody owns the key, so proceeding is the honest answer."""

    async def never_claims(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(store._redis, "set", never_claims)

    assert await _claim(store) == Claimed()


# --------------------------------------------------------------------------- #
# Constants Step 6 reads
# --------------------------------------------------------------------------- #
def test_the_two_headers_are_distinct_facts() -> None:
    """Trap 11: a replay of a cache hit sets both, and they must not collapse."""
    assert IDEMPOTENCY_HEADER == "Idempotency-Key"
    assert REPLAY_HEADER == "X-Idempotent-Replay"
    assert MAX_KEY_LENGTH == 255
