"""D1's restart state machine, asserted on the bytes it puts on the wire.

Every test scripts a sequence of *upstream* behaviours — a stream that flows, a
stream that dies mid-sentence, a 429 before the first token — through
``httpx.MockTransport``, and reads back the SSE frames a client would receive.
Faking at the transport layer rather than behind a fake router means the
orchestrator is driven by errors that came out of the real ``parse_error`` and by
deltas that came out of the real adapter, so the flags and frames it reacts to
are the ones production will hand it.

What these defend, in order of how expensive the bug is:

**The 200 boundary (D13).** A failure before the first byte must arrive as a JSON
error envelope, not as a 200 that says "failed" somewhere inside itself. Get it
wrong and every pre-stream fault becomes undebuggable from the client side, which
is precisely the situation the decision exists to prevent.

**A restart clears rather than splices.** ``discarded_chars`` and the cleared
buffer are the contract the frontend implements; if the server splices, the user
sees half an answer from one model welded to a full answer from another and it
reads as a model quality problem rather than a bug.

**The tokens of a discarded attempt survive.** They were really generated and
really charged against the free tier. Dropping them makes Phase 3's tracker wrong
in the exact scenario it exists for, and the miscount is invisible until a key
rate-limits earlier than predicted.

**A client disconnect is not a fault.** Someone closing a tab must not produce a
restart, a ``done``, or a persisted row.

Time is not mocked here, unlike ``test_router.py``: the interesting quantities
are byte sequences rather than latencies, and the two timeouts that *are* tested
are driven by a scripted delay measured in tens of milliseconds.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fakeredis.aioredis import FakeRedis

from app.cache import keys
from app.core.clock import FixedClock
from app.memory.canonical import CanonicalMessage
from app.providers.errors import ContentFiltered, RateLimited
from app.providers.registry import build_registry
from app.routing import router as routing
from app.routing.circuit_breaker import CircuitBreaker
from app.streaming import orchestrator
from app.streaming.orchestrator import StreamResult
from tests import provider_fixtures
from tests.unit.test_router import FLEET_CONFIG, PARAMS, PROVIDER

CONVERSATION_ID = UUID("22222222-2222-4222-8222-222222222222")

type ScriptItem = provider_fixtures.RecordedResponse | list[bytes | float | BaseException]
"""One scripted upstream attempt: a recorded JSON response, or a body script."""


# --------------------------------------------------------------------------- #
# Driving the thing
# --------------------------------------------------------------------------- #
def frame(**event: Any) -> bytes:
    """One provider-side SSE frame carrying a Groq chat-completion chunk."""
    return f"data: {json.dumps(event)}\n\n".encode()


def deltas(*texts: str) -> list[bytes]:
    """Body bytes for a stream that says these things and stops mid-sentence.

    No terminal chunk and no ``[DONE]`` — callers append either :func:`finished`
    or a fault, which is the distinction every test here turns on.
    """
    return [
        frame(choices=[{"index": 0, "delta": {"content": text}, "finish_reason": None}])
        for text in texts
    ]


def finished(*, tokens_in: int = 48, tokens_out: int = 11) -> list[bytes]:
    """The final chunk plus ``[DONE]``: a stream that ended because it was done."""
    return [
        frame(
            choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
            x_groq={
                "usage": {
                    "prompt_tokens": tokens_in,
                    "completion_tokens": tokens_out,
                    "total_tokens": tokens_in + tokens_out,
                }
            },
        ),
        b"data: [DONE]\n\n",
    ]


def stream_of(*texts: str) -> list[bytes | float | BaseException]:
    """A complete, well-behaved stream."""
    return [*deltas(*texts), *finished()]


def refused(*texts: str) -> list[bytes | float | BaseException]:
    """A stream that says these things and then stops on a safety refusal.

    The mid-stream shape of a content filter: no error body, no non-200 status,
    just a terminal chunk whose ``finish_reason`` says the model declined.
    """
    return [
        *deltas(*texts),
        frame(choices=[{"index": 0, "delta": {}, "finish_reason": "content_filter"}]),
    ]


def dies_after(*texts: str) -> list[bytes | float | BaseException]:
    """A stream that delivers these deltas and then has its connection cut.

    ``RemoteProtocolError`` rather than a mid-stream error frame because it is the
    harder case: the HTTP status was 200 long ago, so the only signal is the
    transport dying, and the adapter has to normalize it rather than let a decode
    error escape from inside a streaming response.
    """
    return [*deltas(*texts), httpx.RemoteProtocolError("peer closed the connection")]


class StreamingHandler:
    """Serves a *sequence* of upstream attempts, streaming or not.

    ``ScriptedHandler``'s streaming twin. An unscripted request is an assertion
    failure rather than a repeat of the last one: "the loop tried a fourth time"
    is exactly the bug the attempt cap exists to prevent, and silently answering
    it would hide that.
    """

    def __init__(self, *script: ScriptItem) -> None:
        self.script = list(script)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = len(self.requests) - 1
        if index >= len(self.script):
            raise AssertionError(
                f"unscripted request #{index + 1} to {request.url}; "
                f"the script holds {len(self.script)}"
            )

        item = self.script[index]
        if isinstance(item, provider_fixtures.RecordedResponse):
            return item.to_response()
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=provider_fixtures.ScriptedByteStream(item),
        )

    @property
    def calls(self) -> int:
        return len(self.requests)

    def models(self) -> list[str]:
        return [json.loads(request.content)["model"] for request in self.requests]

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


class Recorder:
    """A :class:`~app.streaming.orchestrator.StreamPersistence` that only remembers.

    Step 10's collector writes rows; this captures the value it would have been
    handed, which is what lets the persistence contract be asserted here without
    a database.
    """

    def __init__(self, *, explode: bool = False) -> None:
        self.results: list[StreamResult] = []
        self._explode = explode

    async def persist(self, result: StreamResult) -> None:
        self.results.append(result)
        if self._explode:
            raise RuntimeError("the database went away")


@pytest.fixture
def history() -> list[CanonicalMessage]:
    return provider_fixtures.canonical_history()


@pytest.fixture
def breaker(redis_client: FakeRedis, frozen_clock: FixedClock) -> CircuitBreaker:
    return CircuitBreaker(redis_client, clock=frozen_clock)


async def drive(
    handler: StreamingHandler,
    breaker: CircuitBreaker,
    history: list[CanonicalMessage],
    *,
    requested: str = "general",
    message_id: UUID | None = None,
    **kwargs: Any,
) -> list[bytes]:
    """Run one streamed turn to completion and collect the raw frames."""
    chunks: list[bytes] = []
    stream = orchestrator.stream_completion(
        registry=build_registry(client=handler.client(), config=FLEET_CONFIG),
        breaker=breaker,
        history=history,
        params=PARAMS,
        requested=requested,
        conversation_id=CONVERSATION_ID,
        message_id=message_id or uuid4(),
        **kwargs,
    )
    async for chunk in stream:
        chunks.append(chunk)
    return chunks


def parse(chunks: Sequence[bytes]) -> list[tuple[str, dict[str, Any]]]:
    """Raw frames -> ``[(event name, payload)]``, heartbeats dropped."""
    events: list[tuple[str, dict[str, Any]]] = []
    for raw in chunks:
        text = raw.decode()
        if text.startswith(":"):
            continue
        name_line, data_line = text.strip().split("\n", 1)
        events.append(
            (name_line.removeprefix("event: "), json.loads(data_line.removeprefix("data: ")))
        )
    return events


def names(chunks: Sequence[bytes]) -> list[str]:
    return [name for name, _ in parse(chunks)]


def only(chunks: Sequence[bytes], name: str) -> dict[str, Any]:
    matches = [payload for event, payload in parse(chunks) if event == name]
    assert len(matches) == 1, f"expected exactly one {name!r} event, got {len(matches)}"
    return matches[0]


def text_of(chunks: Sequence[bytes]) -> str:
    return "".join(
        payload["choices"][0]["delta"]["content"]
        for event, payload in parse(chunks)
        if event == "delta"
    )


def rate_limited() -> provider_fixtures.RecordedResponse:
    return provider_fixtures.load(PROVIDER, "rate_limited")


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #
async def test_a_clean_stream_is_meta_then_deltas_then_done(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    handler = StreamingHandler(stream_of("A gateway", " sits between", " three providers."))

    chunks = await drive(handler, breaker, history)

    assert names(chunks) == ["meta", "delta", "delta", "delta", "done"]
    assert text_of(chunks) == "A gateway sits between three providers."

    done = only(chunks, "done")
    assert done["status"] == "ok"
    assert done["attempts"] == 1
    assert done["substituted"] is False
    assert done["usage"] == {
        "prompt_tokens": 48,
        "completion_tokens": 11,
        "total_tokens": 59,
        "estimated": False,
        "wasted_tokens_out": 0,
    }


async def test_meta_carries_the_ids_the_client_will_need(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """The assistant id is minted before the row exists, because ``meta`` promises
    it with the first token and nothing is persisted until after ``done``."""
    message_id = uuid4()
    handler = StreamingHandler(stream_of("hello"))

    chunks = await drive(handler, breaker, history, message_id=message_id)
    meta = only(chunks, "meta")

    assert meta["message_id"] == str(message_id)
    assert meta["conversation_id"] == str(CONVERSATION_ID)
    assert meta["attempt"] == 1
    assert meta["model"] == "model-a"
    assert meta["requested_slot"] == "general"


async def test_nothing_follows_done(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """The terminal event makes the message final. The handler holds one script,
    so a fourth candidate being tried after a success fails loudly."""
    handler = StreamingHandler(stream_of("done and dusted"))

    chunks = await drive(handler, breaker, history)

    assert names(chunks)[-1] == "done"
    assert handler.calls == 1


# --------------------------------------------------------------------------- #
# D13 — the 200 boundary
# --------------------------------------------------------------------------- #
async def test_a_failure_before_the_first_byte_raises_instead_of_committing(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """The whole point of D13. A fully-down pool must produce a debuggable error
    envelope with a ``request_id``, not a 200 that says "failed" inside itself."""
    handler = StreamingHandler(rate_limited(), rate_limited(), rate_limited())

    with pytest.raises(routing.RoutingFailed) as failure:
        await drive(handler, breaker, history)

    assert isinstance(failure.value.error, RateLimited)
    assert failure.value.attempts == 3


async def test_a_pre_first_delta_failover_is_invisible_to_the_client(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """Nothing was shown, so there is nothing to withdraw: no ``restart``, and
    ``meta`` simply names the candidate that did answer."""
    handler = StreamingHandler(rate_limited(), stream_of("second time lucky"))

    chunks = await drive(handler, breaker, history)

    assert names(chunks) == ["meta", "delta", "done"]
    assert only(chunks, "meta")["attempt"] == 2
    assert only(chunks, "meta")["model"] == "model-b"


async def test_a_refusal_ends_the_request_on_the_first_candidate(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """Failing over a refusal would shop the same prompt around until some model
    answers it. Decided by the error's own flags, not by this module."""
    handler = StreamingHandler(refused())

    with pytest.raises(routing.RoutingFailed) as failure:
        await drive(handler, breaker, history)

    assert isinstance(failure.value.error, ContentFiltered)
    assert handler.calls == 1


async def test_a_refusal_mid_sentence_stops_rather_than_restarting(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """The bytes are already out, so it ends in-band — but it still never restarts.
    A model that changed its mind halfway is not a provider that went down."""
    handler = StreamingHandler(refused("I can help with part of "))

    chunks = await drive(handler, breaker, history)

    assert names(chunks) == ["meta", "delta", "done"]
    assert only(chunks, "done")["status"] == "failed"
    assert handler.calls == 1


# --------------------------------------------------------------------------- #
# D1 — the restart
# --------------------------------------------------------------------------- #
async def test_a_mid_stream_death_restarts_on_the_next_candidate(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    handler = StreamingHandler(
        dies_after("The gateway ", "routes across "),
        stream_of("Three providers, one interface."),
    )

    chunks = await drive(handler, breaker, history)

    assert names(chunks) == ["meta", "delta", "delta", "restart", "delta", "done"]
    assert handler.models() == ["model-a", "model-b"]

    restart = only(chunks, "restart")
    assert restart["reason"] == "provider_unavailable"
    assert restart["failed"] == {"provider": PROVIDER, "model": "model-a"}
    assert restart["next"]["model"] == "model-b"
    assert restart["attempt"] == 2
    assert restart["discarded_chars"] == len("The gateway routes across ")

    assert only(chunks, "done")["attempts"] == 2


async def test_the_restart_arrives_before_the_replacement_text(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """Ordering is the client-side contract: a delta that landed before the clear
    signal would be wiped by it, and the answer would start a word late."""
    handler = StreamingHandler(dies_after("discard me"), stream_of("keep me"))

    events = parse(await drive(handler, breaker, history))

    assert [name for name, _ in events] == ["meta", "delta", "restart", "delta", "done"]
    assert events[-2][1]["choices"][0]["delta"]["content"] == "keep me"


async def test_the_discarded_tokens_are_reported_rather_than_lost(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """They were really generated and really charged against the free tier. This
    is the number Phase 3's reconciliation cannot recover after the fact."""
    discarded = "x" * 400
    handler = StreamingHandler(dies_after(discarded), stream_of("the real answer"))

    done = only(await drive(handler, breaker, history), "done")

    assert done["usage"]["wasted_tokens_out"] == len(discarded) // 4


async def test_a_restart_never_promotes_the_candidate_that_just_died(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """Mid-generation there is no same-provider retry: a candidate that accepted a
    connection and then died has spent its turn, and a second go at it would come
    out of the same budget of three as a candidate that has not failed at all."""
    handler = StreamingHandler(dies_after("half an answer"), stream_of("a whole one"))

    await drive(handler, breaker, history)

    assert handler.models() == ["model-a", "model-b"]


async def test_three_attempts_is_the_end_of_it(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """A fourth candidate exists in the fleet and is never tried."""
    partial = "The gateway sits between a client and several free-tier providers."
    handler = StreamingHandler(dies_after(partial), rate_limited(), rate_limited())

    chunks = await drive(handler, breaker, history)
    done = only(chunks, "done")

    assert handler.calls == routing.MAX_ATTEMPTS
    assert done["status"] == "failed"
    assert done["attempts"] == 3
    assert done["partial_content"] == partial


async def test_a_trivial_partial_is_not_offered_back(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """ "Keep partial answer" next to three characters is a worse offer than none."""
    handler = StreamingHandler(dies_after("Th"), rate_limited(), rate_limited())

    done = only(await drive(handler, breaker, history), "done")

    assert done["status"] == "failed"
    assert done["partial_content"] is None


async def test_the_longest_partial_wins_not_the_last(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """The attempt that got furthest is the one worth offering to keep."""
    long_partial = "The gateway routes across three providers and discloses which one served."
    handler = StreamingHandler(
        dies_after(long_partial),
        dies_after("a much shorter second go"),
        rate_limited(),
    )

    done = only(await drive(handler, breaker, history), "done")

    assert done["partial_content"] == long_partial


# --------------------------------------------------------------------------- #
# Timeouts
# --------------------------------------------------------------------------- #
async def test_the_first_token_budget_fires_independently_of_the_idle_one(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """D13's answer to the silence it introduces. A candidate that has produced
    nothing at all is not warming up, and the idle timeout — sized for a model
    mid-generation, which has demonstrated it works — is far too patient for it.

    The stall arrives as an ordinary ``Unavailable``, so what follows it is the
    ordinary same-provider retry: nothing was streamed, so nothing was wasted by
    trying again. The assertion that matters is that the turn was rescued in
    tens of milliseconds rather than waiting out the thirty-second idle budget.
    """
    handler = StreamingHandler([5.0, *stream_of("too late")], stream_of("in time"))

    chunks = await drive(
        handler,
        breaker,
        history,
        first_token_timeout_s=0.05,
        idle_timeout_s=30.0,
    )

    assert text_of(chunks) == "in time"
    assert only(chunks, "meta")["attempt"] == 2
    assert handler.calls == 2


# --------------------------------------------------------------------------- #
# Substitution
# --------------------------------------------------------------------------- #
async def test_a_spill_out_of_the_named_slot_is_disclosed(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """D10 on the streaming path. Without the spill ``substituted`` is unreachable,
    and the provenance block that makes D1/D2 honest can never report anything."""
    handler = StreamingHandler(rate_limited(), rate_limited(), stream_of("general answered"))

    done = only(await drive(handler, breaker, history, requested="fast"), "done")

    assert done["substituted"] is True
    assert done["requested_slot"] == "fast"
    assert done["served_by"]["slot"] == "general"


# --------------------------------------------------------------------------- #
# Client disconnect — not a fault
# --------------------------------------------------------------------------- #
async def test_a_client_that_left_gets_no_done_and_leaves_no_row(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """Someone closing a tab is a person leaving. It must not look like a failure
    in the logs, must not restart, and must not persist a half-written answer."""
    handler = StreamingHandler(stream_of("one", "two", "three"))
    recorder = Recorder()
    seen = 0

    async def gone() -> bool:
        nonlocal seen
        seen += 1
        return seen > 2

    chunks = await drive(handler, breaker, history, persistence=recorder, is_disconnected=gone)

    assert "done" not in names(chunks)
    assert recorder.results == []


async def test_leaving_mid_generation_aborts_the_upstream_request(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """The harder disconnect: nothing is arriving, so the wait for the next chunk
    is still in flight and has to be cancelled and unwound in the right order —
    cancel, let it finish, *then* close the generator. Getting that backwards
    raises "asynchronous generator is already running" from inside the cleanup of
    a request that was merely abandoned."""
    handler = StreamingHandler([*deltas("thinking"), 5.0, *finished()])
    recorder = Recorder()
    seen = 0

    async def gone() -> bool:
        nonlocal seen
        seen += 1
        return seen > 2

    chunks = await drive(
        handler,
        breaker,
        history,
        persistence=recorder,
        is_disconnected=gone,
        heartbeat_interval_s=0.02,
    )

    assert "done" not in names(chunks)
    assert recorder.results == []


# --------------------------------------------------------------------------- #
# The handoff to Step 10
# --------------------------------------------------------------------------- #
async def test_the_collector_is_handed_the_assembled_answer(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """One logical message, whatever the attempt count — with the *final* serving
    model on it and the whole trail beside it."""
    message_id = uuid4()
    handler = StreamingHandler(dies_after("thrown away"), stream_of("kept ", "answer"))
    recorder = Recorder()

    await drive(handler, breaker, history, message_id=message_id, persistence=recorder)
    result = recorder.results[0]

    assert result.status == "ok"
    assert result.text == "kept answer"
    assert result.message_id == message_id
    assert result.conversation_id == CONVERSATION_ID
    assert result.spec is not None and result.spec.model == "model-b"
    assert result.attempts == 2
    assert [record.outcome for record in result.trail] == ["error", "ok"]
    assert result.wasted_tokens_out == len("thrown away") // 4
    assert result.ttft_ms is not None


async def test_a_persistence_failure_does_not_break_a_finished_stream(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """D14. The user already has their answer and there is no response left to
    turn into a 500; the honest cost is a message that will not survive a refresh,
    and it is visible in the logs rather than silent."""
    handler = StreamingHandler(stream_of("delivered"))
    recorder = Recorder(explode=True)

    chunks = await drive(handler, breaker, history, persistence=recorder)

    assert names(chunks)[-1] == "done"
    assert recorder.results  # it was called, and it did raise


async def test_a_failed_stream_still_reaches_the_collector(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """The ``requests`` row for a turn that walked three candidates is the one row
    where "what did it try?" is the entire question."""
    handler = StreamingHandler(dies_after("nothing survives"), rate_limited(), rate_limited())
    recorder = Recorder()

    await drive(handler, breaker, history, persistence=recorder)
    result = recorder.results[0]

    assert result.status == "failed"
    assert result.error_code == "rate_limited"
    assert result.attempts == 3
    assert result.wasted_tokens_out == len("nothing survives") // 4


async def test_nothing_is_persisted_when_no_byte_was_ever_sent(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """A pre-stream failure leaves through the error envelope, and the endpoint —
    which still has its request-scoped session — writes that row itself."""
    handler = StreamingHandler(rate_limited(), rate_limited(), rate_limited())
    recorder = Recorder()

    with pytest.raises(routing.RoutingFailed):
        await drive(handler, breaker, history, persistence=recorder)

    assert recorder.results == []


# --------------------------------------------------------------------------- #
# Observability
# --------------------------------------------------------------------------- #
async def test_the_attempt_counter_is_written_and_expires(
    breaker: CircuitBreaker,
    history: list[CanonicalMessage],
    redis_client: FakeRedis,
) -> None:
    """Written for observability, deliberately never read for control flow
    (ADR-015) — the in-process loop counter is authoritative."""
    message_id = uuid4()
    handler = StreamingHandler(dies_after("first go"), stream_of("second go"))

    await drive(handler, breaker, history, message_id=message_id, redis=redis_client)
    key = keys.stream_attempts(message_id)

    assert await redis_client.get(key) == "2"
    assert 0 < await redis_client.ttl(key) <= keys.STREAM_ATTEMPTS_TTL_S


async def test_a_dead_redis_does_not_interrupt_the_stream(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """ADR-010's fail-open, applied to a counter nobody reads. Losing it costs
    observability; raising on it would cost the answer."""

    class _DeadRedis:
        def __getattr__(self, _name: str) -> Any:
            async def boom(*_args: Any, **_kwargs: Any) -> Any:
                raise ConnectionError("redis is gone")

            return boom

    handler = StreamingHandler(stream_of("still fine"))

    chunks = await drive(handler, breaker, history, redis=_DeadRedis())

    assert text_of(chunks) == "still fine"


# --------------------------------------------------------------------------- #
# Keepalives
# --------------------------------------------------------------------------- #
async def test_a_gap_between_tokens_produces_a_heartbeat(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """A comment line: valid SSE, ignored by every client's ``onmessage``, and
    enough to stop a proxy reaping a connection that is merely thinking."""
    handler = StreamingHandler([*deltas("before"), 0.15, *deltas(" after"), *finished()])

    chunks = await drive(handler, breaker, history, heartbeat_interval_s=0.02)

    assert b": heartbeat\n\n" in chunks
    assert text_of(chunks) == "before after"


async def test_no_heartbeat_escapes_before_the_first_token(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """A keepalive is a byte like any other: sending one would commit the response
    to 200 and take the error envelope away from every failure after it (D13)."""
    handler = StreamingHandler([0.15, *stream_of("eventually")])

    chunks = await drive(
        handler,
        breaker,
        history,
        heartbeat_interval_s=0.02,
        first_token_timeout_s=5.0,
    )

    assert chunks[0].startswith(b"event: meta")
