"""D6/D47 — the idempotency store: claim a key, replay an answer, release a failure.

A client that retries ``POST /v1/chat/completions`` after a timeout should not
pay for a second completion, and should not end up with two assistant turns in
one thread. D6 is the promise; this module is the mechanism, and Step 6 is the
wiring that puts it in front of the router.

**Why this lives beside :mod:`app.cache.exact` rather than in
``cache/client.py``.** Same reason the exact cache does: it is a policy over
Redis, not a Redis client. Nothing here knows about FastAPI, and nothing here
imports the chat schemas — :func:`fingerprint` takes a structural
:class:`IdempotentRequest`, which ``ChatCompletionRequest`` satisfies by having
the fields, so the dependency points from the endpoint at this module and never
back.

**The value is an envelope, not a bare ``request_id``.** §2.3 and D6 both
literally say the key maps to a request id, and a request id cannot reconstruct
a body — "the replay returns the stored response" is the behaviour D6 actually
asks for. So :class:`IdempotencyEnvelope` carries the state, the fingerprint,
the claiming request's id, whether the original was streamed, and (once done)
the response body itself. **This is not a Contract C amendment**: §2.3 froze the
key *format*, and ``idem:{user_id}:{idem_key}`` is unchanged. The last two
phases both amended Contract C with sign-off, and a reader should be able to
tell "amended again" from "did not need to be".

**The order is the whole design** (D47), and it lives at the call site rather
than here: the claim is issued *before* the cache read, before quota, and before
routing. Two concurrent identical retries that both reach a provider are exactly
what D6 exists to prevent, and only ``SET NX`` issued first prevents them.

**The fingerprint is what makes a replay safe.** A key is a client's label, not
a promise; Stripe's semantics — same key, different body, hard error — are the
only honest reading, because silently answering a different question under a
reused key is worse than either answering it or refusing. :func:`fingerprint`
therefore folds in everything the answer depends on, ``stream`` included: a
streaming retry that replayed a non-streaming body would be a protocol error
dressed as a feature.

**Fails open, per Contract C.** Any Redis error logs once and degrades to
today's behaviour — :meth:`IdempotencyStore.claim` returns :class:`Claimed`,
:meth:`IdempotencyStore.complete` and :meth:`IdempotencyStore.release` shrug.
This is caching's rule (and D20's), not quota's fail-closed rule (D15), and for
the same reason: nothing is being *spent* by proceeding, so refusing to answer
because a cache is down is a worse failure than answering twice.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol, runtime_checkable
from uuid import UUID

from redis.asyncio import Redis

from app.cache import keys
from app.core.logging import get_logger

logger = get_logger("app.cache.idempotency")

FINGERPRINT_VERSION: Final = 1
"""Bumped on any change to what :func:`fingerprint` folds in. A bump invalidates
every stored envelope at once — every stored fingerprint stops matching, so a
retry reads as :class:`FingerprintMismatch` rather than as a replay of an answer
computed under different rules."""

IDEMPOTENCY_HEADER: Final = "Idempotency-Key"

REPLAY_HEADER: Final = "X-Idempotent-Replay"
"""``X-Cache`` and this are different facts (trap 11). A replay of a request that
was originally served from the exact cache carries both, and that is correct: one
says where the *original* answer came from, the other says this call did not
compute anything at all."""

MAX_KEY_LENGTH: Final = 255
"""A client-chosen key becomes a Redis key segment. ``keys._segment`` already
refuses empty and whitespace-bearing segments; the length cap belongs beside it
rather than only in the endpoint, so the 400 a client gets and the key this
module would have built agree on what is acceptable."""

IN_FLIGHT: Final = "in_flight"
DONE: Final = "done"

EnvelopeState = Literal["in_flight", "done"]


@runtime_checkable
class IdempotentMessage(Protocol):
    """One turn, as :func:`fingerprint` needs to see it.

    Read-only properties rather than plain attributes, so a concrete
    ``list[InputMessage]`` satisfies ``Sequence[IdempotentMessage]`` — a mutable
    protocol attribute is invariant and would not.
    """

    @property
    def role(self) -> str: ...

    @property
    def content(self) -> str: ...

    @property
    def file_refs(self) -> Sequence[str]: ...


@runtime_checkable
class IdempotentRequest(Protocol):
    """The part of a chat request that decides what the answer would be.

    Structural on purpose: ``ChatCompletionRequest`` satisfies it without this
    module importing it, which is what keeps ``cache/`` free of a dependency on
    ``schemas/``.
    """

    @property
    def model(self) -> str: ...

    @property
    def messages(self) -> Sequence[IdempotentMessage]: ...

    @property
    def conversation_id(self) -> UUID | None: ...

    @property
    def temperature(self) -> float: ...

    @property
    def max_tokens(self) -> int | None: ...

    @property
    def top_p(self) -> float | None: ...

    @property
    def stop(self) -> Sequence[str]: ...

    @property
    def stream(self) -> bool: ...


def fingerprint(body: IdempotentRequest, *, user_id: str | UUID) -> str:
    """``sha256`` over the canonicalized request — everything the answer depends on.

    The same serialization discipline as :func:`app.cache.exact.request_hash`:
    ``sort_keys`` for an encoding that does not depend on dict-key insertion
    order, the tightest separators, and every generation knob folded in verbatim
    rather than normalized, so two bodies differing in any of them are two
    questions.

    Three inclusions are worth naming. ``conversation_id``, because "add this
    turn to thread A" and the byte-identical "add it to thread B" are different
    requests with different histories behind them. ``file_refs``, because an
    attachment is content (D24) and a retry that dropped one is asking something
    else. And ``stream``, because a streaming retry replaying a non-streaming
    body would hand the client a JSON object on a channel it is parsing as SSE.

    ``user_id`` is folded in even though ``idem:{user_id}:{idem_key}`` is already
    user-scoped: it costs nothing, and it makes a fingerprint identify a request
    completely rather than only relative to the key it was found under.
    """
    payload = {
        "v": FINGERPRINT_VERSION,
        "user_id": str(user_id),
        "slot": body.model,
        "conversation_id": str(body.conversation_id) if body.conversation_id else None,
        "messages": [
            {
                "role": message.role,
                "content": message.content,
                "file_refs": list(message.file_refs),
            }
            for message in body.messages
        ],
        "temperature": body.temperature,
        "max_tokens": body.max_tokens,
        "top_p": body.top_p,
        "stop": list(body.stop),
        "stream": body.stream,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class IdempotencyEnvelope:
    """What is stored at ``idem:{user_id}:{idem_key}``.

    ``response`` is a serialized ``ChatCompletionResponse`` body, present only in
    the ``done`` state — this module never builds it and never validates it, it
    only carries it, which is what keeps the chat schemas out of ``cache/``.
    """

    state: EnvelopeState
    fingerprint: str
    request_id: str
    stream: bool
    response: dict[str, Any] | None = None

    cache_status: str | None = None
    """The ``X-Cache`` value the *original* request returned, carried so a replay
    can repeat it (Step 6, trap 11).

    ``X-Cache`` and :data:`REPLAY_HEADER` are different facts, and a replay
    computes neither: it recomputes no cache key and attempts no candidate, so
    the only honest thing it can say about provenance is what the original said.
    A replay of a turn that was itself a cache hit therefore carries ``HIT`` and
    ``X-Idempotent-Replay: true`` together. ``None`` on an ``in_flight``
    envelope, where there is no answer to have a provenance yet."""

    def to_json(self) -> str:
        """Written in full, defaults included — the rule
        :meth:`app.memory.canonical.MessageMeta.to_jsonb` already follows, so a
        reader never has to know which version wrote a value to interpret it."""
        return json.dumps(
            {
                "state": self.state,
                "fingerprint": self.fingerprint,
                "request_id": self.request_id,
                "stream": self.stream,
                "response": self.response,
                "cache_status": self.cache_status,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> IdempotencyEnvelope:
        """Rebuild a stored envelope, tolerating unknown keys and raising on junk.

        Lenient in one direction only, exactly like ``MessageMeta.from_jsonb``: a
        key written by a newer version is ignored rather than fatal, but a wrong
        *type* is a bug and raises :class:`ValueError` — which
        :meth:`IdempotencyStore.claim` catches and treats as a fail-open claim
        rather than propagating to a caller who cannot do anything about it.
        """
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f"idempotency envelope must be an object, got {type(data).__name__}")

        state = data.get("state")
        if state not in (IN_FLIGHT, DONE):
            raise ValueError(f"idempotency envelope has an unknown state: {state!r}")

        stored_fingerprint = data.get("fingerprint")
        if not isinstance(stored_fingerprint, str):
            raise ValueError("idempotency envelope's fingerprint must be a string")

        request_id = data.get("request_id")
        if not isinstance(request_id, str):
            raise ValueError("idempotency envelope's request_id must be a string")

        stream = data.get("stream", False)
        if not isinstance(stream, bool):
            raise ValueError("idempotency envelope's stream must be a boolean")

        response = data.get("response")
        if response is not None and not isinstance(response, dict):
            raise ValueError("idempotency envelope's response must be an object or null")

        cache_status = data.get("cache_status")
        if cache_status is not None and not isinstance(cache_status, str):
            raise ValueError("idempotency envelope's cache_status must be a string or null")

        return cls(
            state=state,
            fingerprint=stored_fingerprint,
            request_id=request_id,
            stream=stream,
            response=response,
            cache_status=cache_status,
        )


@dataclass(frozen=True, slots=True)
class Claimed:
    """This request owns the key and must finish it — with
    :meth:`IdempotencyStore.complete` on success and
    :meth:`IdempotencyStore.release` on *every* failure path (trap 3). A claim
    left in flight locks that key for a day, and the client's retry — the exact
    thing D6 exists to serve — would come back as a 409."""


@dataclass(frozen=True, slots=True)
class Replay:
    """The key is done and the fingerprint matches: return the stored body, make
    no provider call, spend no quota, write no message rows."""

    envelope: IdempotencyEnvelope


@dataclass(frozen=True, slots=True)
class InFlight:
    """The same request is being served right now by a call that has not
    finished. 409 with ``Retry-After: 1`` — the answer is coming, just not here."""

    envelope: IdempotencyEnvelope


@dataclass(frozen=True, slots=True)
class FingerprintMismatch:
    """The key has already been used for a *different* request. Stripe's
    semantics: a hard 409 in both the ``in_flight`` and the ``done`` state,
    because silently answering a different question under a reused key is the
    failure mode this check exists to prevent."""

    envelope: IdempotencyEnvelope


ClaimResult = Claimed | Replay | InFlight | FingerprintMismatch
"""Four outcomes, one per row of D47's table."""


class IdempotencyStore:
    """Reads and writes ``idem:{user_id}:{idem_key}``. Fails open, always.

    Constructed per request over the process-wide Redis client, like
    :class:`app.cache.exact.ExactCache`: it holds no state of its own — the key
    is the state, shared by every worker and every instance.

    ``user_id`` is a keyword on each method rather than a constructor argument
    because the store is built by ``deps.get_idempotency_store`` from the Redis
    client alone; resolving a principal there would need the auth dependency
    chain that ``deps`` deliberately cannot import (see ``get_credentials``).
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def claim(
        self,
        key: str,
        *,
        user_id: str | UUID,
        fingerprint: str,
        request_id: str,
        stream: bool,
    ) -> ClaimResult:
        """One ``SET NX EX``, and on collision one ``GET``.

        ``NX`` does the real work: it is atomic, so two concurrent identical
        retries produce exactly one :class:`Claimed` and the loser reads the
        winner's envelope. A ``GET``-then-``SET`` would let both through, which
        is the entire failure D6 exists to prevent.

        Fails open: any Redis error, and any envelope this version cannot parse,
        returns :class:`Claimed` — the request is then served exactly as it would
        have been before this module existed.
        """
        envelope = IdempotencyEnvelope(
            state=IN_FLIGHT,
            fingerprint=fingerprint,
            request_id=request_id,
            stream=stream,
        )
        redis_key = keys.idempotency(user_id, key)

        try:
            claimed = await self._redis.set(
                redis_key, envelope.to_json(), nx=True, ex=keys.IDEMPOTENCY_TTL_S
            )
            if claimed:
                return Claimed()
            raw = await self._redis.get(redis_key)
        except Exception as exc:
            logger.warning(
                "idempotency.claim_failed", error=str(exc), error_type=type(exc).__name__
            )
            return Claimed()

        if raw is None:
            # The holder released, or the key expired, between the SET and the
            # GET. Proceeding is right: nobody owns the key now, and a second
            # SET NX here would only narrow a race it cannot close.
            return Claimed()

        try:
            stored = IdempotencyEnvelope.from_json(raw)
        except ValueError as exc:
            logger.warning("idempotency.corrupt", error=str(exc), error_type=type(exc).__name__)
            return Claimed()

        if stored.fingerprint != fingerprint:
            return FingerprintMismatch(stored)
        if stored.state == DONE:
            return Replay(stored)
        return InFlight(stored)

    async def complete(
        self,
        key: str,
        *,
        user_id: str | UUID,
        fingerprint: str,
        request_id: str,
        stream: bool,
        response: dict[str, Any],
        cache_status: str | None = None,
    ) -> None:
        """Turn this request's claim into a replayable answer.

        An unconditional ``SET`` with a fresh TTL rather than a compare-and-set:
        the claim already established ownership, and the replay window is a
        24-hour promise about the *answer*, which only exists now. Shrugs on a
        Redis error — the request itself succeeded, and failing it here would
        turn a working answer into a 500 over a bookkeeping write.
        """
        envelope = IdempotencyEnvelope(
            state=DONE,
            fingerprint=fingerprint,
            request_id=request_id,
            stream=stream,
            response=response,
            cache_status=cache_status,
        )
        try:
            await self._redis.set(
                keys.idempotency(user_id, key), envelope.to_json(), ex=keys.IDEMPOTENCY_TTL_S
            )
        except Exception as exc:
            logger.warning(
                "idempotency.complete_failed", error=str(exc), error_type=type(exc).__name__
            )

    async def release(self, key: str, *, user_id: str | UUID) -> None:
        """Give the key back after a failure, so the client's retry really retries.

        Trap 3: a 502 that leaves an ``in_flight`` envelope behind locks that key
        for 24 hours and answers the retry with a 409. Unconditional for the same
        reason ``complete`` is — the claim established ownership, and a failure
        path is the worst possible place to add a new way to raise.
        """
        try:
            await self._redis.delete(keys.idempotency(user_id, key))
        except Exception as exc:
            logger.warning(
                "idempotency.release_failed", error=str(exc), error_type=type(exc).__name__
            )


@dataclass(frozen=True, slots=True)
class ClaimTicket:
    """One claim, held by whoever has to finish it.

    Step 6 needs the same six values in two places — the endpoint, which
    completes a non-streaming turn and releases every failure it can see, and
    :class:`~app.streaming.collector.Collector`, which owns the far end of a
    streamed one long after the endpoint has returned. Passing six parallel
    arguments through that seam is how the fingerprint written on ``complete``
    ends up disagreeing with the one ``claim`` stored; a ticket is one object
    that cannot come apart.

    Holding it is a *duty*, not a convenience: whoever has one must call exactly
    one of :meth:`complete` or :meth:`release` (trap 3). A ticket that is dropped
    leaves an ``in_flight`` envelope behind for a day, and answers the client's
    retry — the exact thing D6 exists to serve — with a 409.
    """

    store: IdempotencyStore
    key: str
    user_id: str | UUID
    fingerprint: str
    request_id: str
    stream: bool

    async def complete(self, *, response: dict[str, Any], cache_status: str | None = None) -> None:
        """This request's answer, stored for the retry that may never come."""
        await self.store.complete(
            self.key,
            user_id=self.user_id,
            fingerprint=self.fingerprint,
            request_id=self.request_id,
            stream=self.stream,
            response=response,
            cache_status=cache_status,
        )

    async def release(self) -> None:
        """Give the key back, so the client's retry really retries."""
        await self.store.release(self.key, user_id=self.user_id)


__all__ = [
    "DONE",
    "FINGERPRINT_VERSION",
    "IDEMPOTENCY_HEADER",
    "IN_FLIGHT",
    "MAX_KEY_LENGTH",
    "REPLAY_HEADER",
    "ClaimResult",
    "ClaimTicket",
    "Claimed",
    "EnvelopeState",
    "FingerprintMismatch",
    "IdempotencyEnvelope",
    "IdempotencyStore",
    "IdempotentMessage",
    "IdempotentRequest",
    "InFlight",
    "Replay",
    "fingerprint",
]
