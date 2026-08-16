"""``app/cache/exact.py`` — D5/D19's exact-match cache.

Four things worth a file of their own: the cacheability predicate that read and
write share, the hash's stability and sensitivity, the whitespace-aligned
chunking that makes a replay look like a stream, and the cache's fail-open
behaviour in both directions — a Redis error is a ``MISS`` on read and a shrug
on write, never an exception a caller has to catch.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fakeredis.aioredis import FakeRedis

from app.cache import keys
from app.cache.exact import (
    BYPASS,
    HIT,
    MISS,
    CachedResponse,
    ExactCache,
    chunk_for_replay,
    is_cacheable,
    request_hash,
)
from app.memory.canonical import (
    CanonicalMessage,
    MessageMeta,
    file_ref_block,
    text_block,
)

CREATED_AT = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


def _message(
    *, role: str = "user", seq: int = 0, text: str = "hello", blocks: list[Any] | None = None
) -> CanonicalMessage:
    return CanonicalMessage(
        id=uuid4(),
        conversation_id=uuid4(),
        role=role,  # type: ignore[arg-type]
        content=blocks if blocks is not None else [text_block(text)],
        meta=MessageMeta(provider_used="groq" if role == "assistant" else None),
        created_at=CREATED_AT,
        seq=seq,
    )


def _cached(**overrides: Any) -> CachedResponse:
    defaults: dict[str, Any] = {
        "text": "a gateway sits between three providers",
        "provider": "groq",
        "model": "llama-3.3-70b-versatile",
        "slot": "general",
        "finish_reason": "stop",
        "tokens_in": 48,
        "tokens_out": 27,
        "usage_estimated": False,
        "created_at": CREATED_AT.isoformat(),
    }
    defaults.update(overrides)
    return CachedResponse(**defaults)


# --------------------------------------------------------------------------- #
# is_cacheable
# --------------------------------------------------------------------------- #
def test_temperature_zero_with_plain_history_is_cacheable() -> None:
    assert is_cacheable(temperature=0.0, history=[_message()]) is True


def test_temperature_above_zero_bypasses() -> None:
    assert is_cacheable(temperature=0.7, history=[_message()]) is False
    assert is_cacheable(temperature=1.0, history=[_message()]) is False


def test_a_degraded_response_is_never_cacheable() -> None:
    """The one dimension only the write side can supply."""
    assert is_cacheable(temperature=0.0, history=[_message()], degraded=True) is False
    # The read side never passes it, so it defaults to permissive on this axis.
    assert is_cacheable(temperature=0.0, history=[_message()]) is True


def test_a_file_ref_anywhere_in_history_bypasses() -> None:
    with_attachment = [
        _message(seq=0, text="here's a file"),
        _message(
            seq=1,
            role="user",
            blocks=[
                file_ref_block(
                    file_hash="a" * 64, filename="doc.pdf", mime="application/pdf", bytes=1024
                )
            ],
        ),
    ]
    assert is_cacheable(temperature=0.0, history=with_attachment) is False


# --------------------------------------------------------------------------- #
# request_hash
# --------------------------------------------------------------------------- #
def _hash(**overrides: Any) -> str:
    defaults: dict[str, Any] = {
        "requested_slot": "auto",
        "history": [_message()],
        "temperature": 0.0,
        "max_tokens": None,
        "top_p": None,
        "stop": [],
    }
    defaults.update(overrides)
    return request_hash(**defaults)


def test_identical_inputs_hash_identically() -> None:
    """Stability: the whole point of the fingerprint is that two structurally
    identical requests collide on purpose."""
    assert _hash() == _hash()


def test_different_requested_slot_hashes_differently() -> None:
    """``auto`` and a named slot are different questions even when they resolve
    identically today (D19)."""
    assert _hash(requested_slot="auto") != _hash(requested_slot="general")


def test_the_served_model_is_not_part_of_the_hash() -> None:
    """A hit has to be servable whoever would have answered it — the served
    model never enters the fingerprint at all, only the requested slot does."""
    # There is no `served_model` parameter to pass, which is itself the proof:
    # the function's signature cannot express the thing D19 says must not count.
    import inspect

    assert "served" not in inspect.signature(request_hash).parameters


@pytest.mark.parametrize(
    "overrides",
    [
        {"temperature": 0.2},
        {"max_tokens": 64},
        {"top_p": 0.9},
        {"stop": ["\n\n"]},
    ],
)
def test_a_different_generation_knob_hashes_differently(overrides: dict[str, Any]) -> None:
    assert _hash() != _hash(**overrides)


def test_a_different_history_hashes_differently() -> None:
    assert _hash(history=[_message(text="hello")]) != _hash(history=[_message(text="goodbye")])


def test_message_order_is_part_of_the_fingerprint() -> None:
    """``sort_keys`` must not reorder the history list itself — only its dict
    keys — or two conversations that said the same two things in opposite order
    would collide."""
    forward = [_message(seq=0, text="first"), _message(seq=1, text="second", role="assistant")]
    backward = [_message(seq=0, text="second"), _message(seq=1, text="first", role="assistant")]
    assert _hash(history=forward) != _hash(history=backward)


def test_the_hash_is_a_lowercase_hex_sha256() -> None:
    digest = _hash()
    assert len(digest) == 64
    assert digest == digest.lower()
    int(digest, 16)  # raises if it is not hex


# --------------------------------------------------------------------------- #
# chunk_for_replay
# --------------------------------------------------------------------------- #
def test_empty_text_produces_no_chunks() -> None:
    assert chunk_for_replay("") == []


def test_short_text_is_one_chunk() -> None:
    assert chunk_for_replay("hi there") == ["hi there"]


def test_chunks_reassemble_to_the_original_text() -> None:
    text = "a gateway sits between three independently rate-limited providers and answers as one"
    chunks = chunk_for_replay(text)
    assert "".join(chunks) == text
    assert len(chunks) > 1


def test_chunks_break_on_whitespace_not_mid_word() -> None:
    text = "the quick brown fox jumps over the lazy dog again and again and again"
    chunks = chunk_for_replay(text, target_chars=10)
    # Every chunk but the last ends right at a space, which is what keeps a
    # replayed delta from splitting a word the way a real chunk boundary never
    # would.
    assert all(chunk.endswith(" ") for chunk in chunks[:-1])


# --------------------------------------------------------------------------- #
# ExactCache — fails open in both directions (Contract C)
# --------------------------------------------------------------------------- #
@pytest.fixture
def cache(redis_client: FakeRedis) -> ExactCache:
    return ExactCache(redis_client)


async def test_a_miss_is_none(cache: ExactCache) -> None:
    assert await cache.get("no-such-hash") is None


async def test_put_then_get_round_trips(cache: ExactCache) -> None:
    value = _cached()
    await cache.put("some-hash", value)

    got = await cache.get("some-hash")

    assert got == value


async def test_a_write_sets_the_configured_ttl(cache: ExactCache, redis_client: FakeRedis) -> None:
    await cache.put("some-hash", _cached())

    ttl = await redis_client.ttl(keys.exact_cache("some-hash"))

    assert 0 < ttl <= keys.EXACT_CACHE_TTL_S


async def test_a_read_failure_degrades_to_a_miss(
    cache: ExactCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken_get(*_args: Any, **_kwargs: Any) -> None:
        raise ConnectionError("redis is gone")

    monkeypatch.setattr(cache._redis, "get", broken_get)

    assert await cache.get("some-hash") is None


async def test_a_write_failure_is_swallowed(
    cache: ExactCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken_set(*_args: Any, **_kwargs: Any) -> None:
        raise ConnectionError("redis is gone")

    monkeypatch.setattr(cache._redis, "set", broken_set)

    # Must not raise. There is nothing here worth an outage over.
    await cache.put("some-hash", _cached())


async def test_a_corrupt_stored_value_is_treated_as_a_miss(
    cache: ExactCache, redis_client: FakeRedis
) -> None:
    await redis_client.set(keys.exact_cache("some-hash"), json.dumps({"unexpected": "shape"}))

    assert await cache.get("some-hash") is None


async def test_reads_and_writes_use_the_frozen_key_builder(
    cache: ExactCache, redis_client: FakeRedis
) -> None:
    await cache.put("abc123", _cached())

    assert await redis_client.get("cache:exact:abc123") is not None


# --------------------------------------------------------------------------- #
# Header constants
# --------------------------------------------------------------------------- #
def test_the_three_cache_status_values_are_distinct() -> None:
    assert {HIT, MISS, BYPASS} == {"HIT", "MISS", "BYPASS"}
