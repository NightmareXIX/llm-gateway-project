"""``app/streaming/collector.py`` — Step 10 — against a real Postgres.

The orchestrator (Step 9) is exercised entirely through ``httpx.MockTransport``
in ``tests/unit/test_orchestrator.py``, with a ``Recorder`` standing in for
persistence so those tests can assert on the *shape* of a
:class:`~app.streaming.orchestrator.StreamResult` without a database. This
module is the other half: given a result, does :class:`Collector` write the
rows Step 10 promises — one ``messages`` row on success, none on failure, and
a ``requests`` row either way, with the attempt trail and the wasted tokens
intact.

No HTTP, no SSE, no router — a :class:`~app.streaming.orchestrator.StreamResult`
is built by hand for each case, the same way the endpoint (once it is wired to
streaming) will receive one from the orchestrator.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal
from app.db.models import Conversation, Message, Request, User
from app.providers.types import ModelSpec, Usage
from app.routing.router import AttemptRecord
from app.streaming.collector import Collector
from app.streaming.orchestrator import StreamResult

pytestmark = pytest.mark.integration

PROVIDER = "groq"
MODEL = "llama-3.3-70b-versatile"


def spec(*, slot: str = "general", provider: str = PROVIDER, model: str = MODEL) -> ModelSpec:
    return ModelSpec(
        slot=slot,
        provider=provider,
        model=model,
        context_window=131072,
        max_output_tokens=8192,
        supports_streaming=True,
        supports_vision=False,
        supports_pdf=False,
        supports_system_field=False,
        max_file_bytes=None,
        priority=0,
    )


def result(
    *,
    conversation_id: UUID,
    message_id: UUID | None = None,
    status: Literal["ok", "failed"] = "ok",
    text: str = "a gateway sits between three providers",
    served_spec: ModelSpec | None = None,
    requested_slot: str = "general",
    substituted: bool = False,
    attempts: int = 1,
    trail: tuple[AttemptRecord, ...] = (),
    error_code: str | None = None,
    wasted_tokens_out: int = 0,
) -> StreamResult:
    return StreamResult(
        conversation_id=conversation_id,
        message_id=message_id or uuid4(),
        requested_slot=requested_slot,
        spec=served_spec,
        text=text,
        usage=Usage(tokens_in=42, tokens_out=11, estimated=False),
        finish_reason="stop",
        status=status,
        error_code=error_code,
        substituted=substituted,
        attempts=attempts,
        trail=trail,
        latency_ms=812,
        ttft_ms=120,
        wasted_tokens_out=wasted_tokens_out,
        degraded=False,
    )


@pytest.fixture
def session_factory(
    db_session: AsyncSession,
) -> Callable[[], Any]:
    """A session "factory" over the test's single transactional session.

    ``Collector`` takes a factory rather than a session (D14), so its own tests
    need something that answers to ``factory()`` the way
    ``async_sessionmaker`` does. It hands back the *same* ``db_session`` every
    time rather than opening a second connection — this suite's isolation
    already comes from the outer transaction that fixture wraps everything in,
    and a genuinely separate connection would not see uncommitted rows the test
    itself just wrote with ``user_factory``/``conversation_factory``.
    """

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield db_session

    return factory


@pytest.fixture
def collector_for(
    session_factory: Callable[[], Any],
) -> Callable[[Principal], Collector]:
    def make(principal: Principal) -> Collector:
        return Collector(session_factory, principal=principal)

    return make


def _principal(user: User) -> Principal:
    return Principal(user_id=user.id, auth_method="session", api_key_id=None, tier="free")


async def _messages(session: AsyncSession, conversation_id: UUID) -> list[Message]:
    rows = await session.execute(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.seq)
    )
    return list(rows.scalars().all())


async def _requests(session: AsyncSession, user_id: UUID) -> list[Request]:
    rows = await session.execute(select(Request).where(Request.user_id == user_id))
    return list(rows.scalars().all())


# --------------------------------------------------------------------------- #
# Success
# --------------------------------------------------------------------------- #
async def test_a_successful_stream_writes_one_message_and_one_request(
    db_session: AsyncSession,
    user_factory: Callable[..., Any],
    conversation_factory: Callable[..., Any],
    collector_for: Callable[[Principal], Collector],
) -> None:
    user = await user_factory()
    conversation: Conversation = await conversation_factory(user=user)
    message_id = uuid4()
    trail = (
        AttemptRecord(n=1, slot="general", provider=PROVIDER, model=MODEL, outcome="ok"),
    )

    await collector_for(_principal(user)).persist(
        result(
            conversation_id=conversation.id,
            message_id=message_id,
            served_spec=spec(),
            trail=trail,
        )
    )

    messages = await _messages(db_session, conversation.id)
    assert len(messages) == 1
    assert messages[0].id == message_id
    assert messages[0].role == "assistant"
    assert messages[0].content == [
        {"type": "text", "text": "a gateway sits between three providers"}
    ]
    assert messages[0].meta["provider_used"] == PROVIDER
    assert messages[0].meta["model_used"] == MODEL
    assert messages[0].meta["slot_used"] == "general"

    requests = await _requests(db_session, user.id)
    assert len(requests) == 1
    assert requests[0].status == "ok"
    assert requests[0].conversation_id == conversation.id
    assert requests[0].provider == PROVIDER
    assert requests[0].model == MODEL
    assert requests[0].attempts == [
        {
            "n": 1,
            "slot": "general",
            "provider": PROVIDER,
            "model": MODEL,
            "outcome": "ok",
            "error_code": None,
            "latency_ms": 0,
            "wasted_tokens_out": 0,
            "breaker": "closed",
        }
    ]


async def test_the_final_serving_model_is_what_lands_on_the_message(
    db_session: AsyncSession,
    user_factory: Callable[..., Any],
    conversation_factory: Callable[..., Any],
    collector_for: Callable[[Principal], Collector],
) -> None:
    """A restarted turn is one logical message — the model that actually finished
    it, not the one the client saw named first."""
    user = await user_factory()
    conversation = await conversation_factory(user=user)

    await collector_for(_principal(user)).persist(
        result(
            conversation_id=conversation.id,
            served_spec=spec(model="llama-3.1-8b-instant"),
            requested_slot="general",
            substituted=True,
            attempts=2,
            trail=(
                AttemptRecord(n=1, slot="general", provider=PROVIDER, model=MODEL, outcome="error"),
                AttemptRecord(
                    n=2,
                    slot="general",
                    provider=PROVIDER,
                    model="llama-3.1-8b-instant",
                    outcome="ok",
                ),
            ),
            wasted_tokens_out=17,
        )
    )

    messages = await _messages(db_session, conversation.id)
    assert messages[0].meta["model_used"] == "llama-3.1-8b-instant"
    assert messages[0].meta["substituted"] is True
    assert messages[0].meta["attempts"] == 2
    assert messages[0].meta["wasted_tokens_out"] == 17

    requests = await _requests(db_session, user.id)
    assert requests[0].wasted_tokens_out == 17
    assert requests[0].substituted is True
    assert len(requests[0].attempts) == 2


async def test_a_persisted_stream_survives_a_refresh(
    db_session: AsyncSession,
    user_factory: Callable[..., Any],
    conversation_factory: Callable[..., Any],
    collector_for: Callable[[Principal], Collector],
) -> None:
    """Step 10's own definition of done: the row is there on a fresh read, not
    just on the ``AsyncSession`` the collector happened to write it with."""
    user = await user_factory()
    conversation = await conversation_factory(user=user)
    message_id = uuid4()

    await collector_for(_principal(user)).persist(
        result(conversation_id=conversation.id, message_id=message_id, served_spec=spec())
    )

    from app.db.repo import messages as messages_repo

    history = await messages_repo.list_for_conversation(
        db_session, conversation_id=conversation.id, user_id=user.id
    )
    assert [message.id for message in history] == [message_id]


# --------------------------------------------------------------------------- #
# Failure — invariant 4
# --------------------------------------------------------------------------- #
async def test_a_failed_stream_writes_no_message_row(
    db_session: AsyncSession,
    user_factory: Callable[..., Any],
    conversation_factory: Callable[..., Any],
    collector_for: Callable[[Principal], Collector],
) -> None:
    """Invariant 4: an empty or half-written generation is an error, not a
    stored message. Only the ``requests`` row records that the turn happened."""
    user = await user_factory()
    conversation = await conversation_factory(user=user)

    await collector_for(_principal(user)).persist(
        result(
            conversation_id=conversation.id,
            status="failed",
            served_spec=spec(),
            error_code="rate_limited",
            attempts=3,
            trail=(
                AttemptRecord(n=1, slot="general", provider=PROVIDER, model=MODEL, outcome="error"),
                AttemptRecord(n=2, slot="general", provider=PROVIDER, model=MODEL, outcome="error"),
                AttemptRecord(n=3, slot="general", provider=PROVIDER, model=MODEL, outcome="error"),
            ),
            wasted_tokens_out=9,
        )
    )

    assert await _messages(db_session, conversation.id) == []

    requests = await _requests(db_session, user.id)
    assert len(requests) == 1
    assert requests[0].status == "error"
    assert requests[0].error_code == "rate_limited"
    assert requests[0].provider == PROVIDER
    assert requests[0].model == MODEL
    assert requests[0].wasted_tokens_out == 9
    assert len(requests[0].attempts) == 3


async def test_a_failure_with_every_candidate_skipped_records_no_provider(
    db_session: AsyncSession,
    user_factory: Callable[..., Any],
    conversation_factory: Callable[..., Any],
    collector_for: Callable[[Principal], Collector],
) -> None:
    """``spec`` is ``None`` when every candidate was skipped on an open breaker —
    the one case with nothing to name. ``NULL``, not a placeholder string."""
    user = await user_factory()
    conversation = await conversation_factory(user=user)

    await collector_for(_principal(user)).persist(
        result(
            conversation_id=conversation.id,
            status="failed",
            served_spec=None,
            error_code="provider_unavailable",
        )
    )

    requests = await _requests(db_session, user.id)
    assert requests[0].provider is None
    assert requests[0].model is None
    assert requests[0].served_slot is None
