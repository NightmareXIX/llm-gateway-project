"""D5/D19 — the exact-match response cache.

Two identical, deterministic requests should cost one provider call. This module
is the whole of that: a fingerprint over exactly what makes two requests the same
question (:func:`request_hash`), a shared answer to what is worth caching at all
(:func:`is_cacheable`), a thin Redis wrapper that fails open in both directions
(:class:`ExactCache`), and the chunking rule that lets a cache hit replay as a
stream that looks like a real one (:func:`chunk_for_replay`).

**What is stored is not a ``ChatCompletionResponse``.** :class:`CachedResponse` is
a small JSON object — text, provider, model, slot, finish reason, the original
usage, when it was written — deliberately smaller than the wire shape, because a
``request_id`` and a ``message_id`` are wrong the instant they are replayed for a
second caller.

**Fails open, per Contract C.** A Redis error on read is a ``MISS``; a Redis
error on write is a shrug. Caching is an optimization, never a decision that can
refuse a request the way quota's fail-closed rule does (D15) — there is nothing
here worth an outage over.

**Read and write share :func:`is_cacheable`.** A predicate that disagreed with
itself between the two call sites would produce a cache that writes things it can
never hit, or hits things it should have bypassed. ``degraded`` is the one
dimension only the write side can know — nothing is knowable about a response
before it exists — so it defaults to ``False`` and the read side simply never
supplies it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Final, Literal

from redis.asyncio import Redis

from app.cache import keys
from app.core.logging import get_logger
from app.memory.canonical import CanonicalMessage, is_file_ref

logger = get_logger("app.cache.exact")

CACHE_FORMAT_VERSION: Final = 1
"""Bumped on any change to what :func:`request_hash` folds in — invalidates the
whole namespace at once rather than needing a key migration."""

CACHE_HEADER: Final = "X-Cache"

HIT: Final = "HIT"
MISS: Final = "MISS"
BYPASS: Final = "BYPASS"
"""Three values, not two: ``MISS`` alone cannot distinguish "first time this was
asked" from "temperature 0.7, never eligible" — and "why did this not cache?" is
the question that actually gets debugged."""

CacheStatus = Literal["HIT", "MISS", "BYPASS"]

CACHE_REPLAY_CHUNK_CHARS: Final = 24
"""Roughly how large a replayed delta is. Small enough that the synthetic stream
still looks like a stream; whitespace-aligned so it never splits a word."""


@dataclass(frozen=True, slots=True)
class CachedResponse:
    """What actually gets written to ``cache:exact:{sha256}``.

    Everything a replay needs and nothing that would be wrong to replay. No
    ``request_id``, no ``message_id`` — those name *this* call, not the one that
    is about to reuse the text.
    """

    text: str
    provider: str
    model: str
    slot: str
    finish_reason: str
    tokens_in: int
    tokens_out: int
    usage_estimated: bool
    created_at: str
    """ISO-8601 UTC. Informational only — nothing here reads it back to compute
    an age; the Redis TTL is what actually expires the entry."""


def is_cacheable(
    *,
    temperature: float,
    history: Sequence[CanonicalMessage],
    degraded: bool = False,
) -> bool:
    """D19's single cacheability predicate. Read and write both call this.

    ``temperature > 0`` means identical inputs will not reliably reproduce a
    high-value output, and a cached creative answer is worse than a fresh one.
    Any ``file_ref`` anywhere in the history excludes it — Phase 4's extraction
    confidence can change underneath a hash that does not cover it. ``degraded``
    is only ever known after a response exists, so the read side calls this with
    the default and the write side supplies the real value.
    """
    if temperature > 0:
        return False
    if degraded:
        return False
    return all(not any(is_file_ref(block) for block in message.content) for message in history)


def request_hash(
    *,
    requested_slot: str,
    history: Sequence[CanonicalMessage],
    temperature: float,
    max_tokens: int | None,
    top_p: float | None,
    stop: Sequence[str],
) -> str:
    """``sha256`` over exactly what makes two requests the same question.

    The **requested** slot, not the served one — ``auto`` and a named slot are
    different questions even when they resolve identically today, and folding in
    the served model would make a hit's identity depend on which candidate
    happened to answer first. The full canonical history — role, ``seq`` order
    and content blocks verbatim — plus the generation knobs that change what a
    deterministic model would say. ``sort_keys`` makes the encoding
    deterministic across dict-key insertion order without reordering the
    history list itself, which is where the real ordering lives.
    """
    payload = {
        "v": CACHE_FORMAT_VERSION,
        "slot": requested_slot,
        "history": [
            {"role": message.role, "seq": message.seq, "content": message.content}
            for message in history
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "top_p": top_p,
        "stop": list(stop),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def chunk_for_replay(text: str, *, target_chars: int = CACHE_REPLAY_CHUNK_CHARS) -> list[str]:
    """Split cached text into delta-sized pieces, breaking on whitespace.

    Not a tokenizer — a cache hit has no tokens to replay, only the text a real
    stream would have assembled from them. Breaking near a space rather than
    exactly at ``target_chars`` is what keeps a replayed delta from splitting a
    word the way a real provider's chunk boundary never would.
    """
    if not text:
        return []

    chunks: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + target_chars, length)
        if end < length:
            boundary = text.rfind(" ", start, end)
            if boundary > start:
                end = boundary + 1  # keep the space with the chunk before it
        chunks.append(text[start:end])
        start = end
    return chunks


class ExactCache:
    """Reads and writes ``cache:exact:{sha256}``. Fails open in both directions."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def get(self, request_hash: str) -> CachedResponse | None:
        """A hit, or ``None`` — for "absent" and "Redis did not answer" alike.

        The caller cannot tell the two apart, and does not need to: either way
        the honest response is to fall through to the router.
        """
        try:
            raw = await self._redis.get(keys.exact_cache(request_hash))
        except Exception as exc:
            logger.warning("cache.exact.read_failed", error=str(exc), error_type=type(exc).__name__)
            return None

        if raw is None:
            return None

        try:
            data = json.loads(raw)
            return CachedResponse(**data)
        except (TypeError, ValueError, KeyError) as exc:
            logger.warning("cache.exact.corrupt", error=str(exc), error_type=type(exc).__name__)
            return None

    async def put(self, request_hash: str, value: CachedResponse) -> None:
        """Write a hit, or shrug. Nothing here is worth refusing a request over."""
        try:
            await self._redis.set(
                keys.exact_cache(request_hash),
                json.dumps(asdict(value)),
                ex=keys.EXACT_CACHE_TTL_S,
            )
        except Exception as exc:
            logger.warning(
                "cache.exact.write_failed", error=str(exc), error_type=type(exc).__name__
            )


__all__ = [
    "BYPASS",
    "CACHE_FORMAT_VERSION",
    "CACHE_HEADER",
    "CACHE_REPLAY_CHUNK_CHARS",
    "HIT",
    "MISS",
    "CacheStatus",
    "CachedResponse",
    "ExactCache",
    "chunk_for_replay",
    "is_cacheable",
    "request_hash",
]
