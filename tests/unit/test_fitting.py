"""D4 — budgeting and truncation (§2.2.5 steps 3 and 4).

Drives ``fit`` directly rather than through ``render``, with hand-built histories
sized against a deliberately tiny budget. A test that needed a real 131k-token
context window to trigger truncation would need a megabyte of fixture text and
would take a second to run; the algorithm does not care how big the numbers are.

The document cases exercise Phase 4's path today. Nothing resolves an attachment
until the perception lane exists, but the truncation logic that will act on those
attachments is written now, and untested code written now is the same as no code
written now.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest

import tests.provider_fixtures as fx
from app.memory.canonical import (
    CanonicalMessage,
    ContentBlock,
    MessageMeta,
    OmissionMarkerBlock,
    Role,
    file_ref_block,
    omission_marker,
    text_block,
)
from app.memory.fitting import (
    MIN_DOCUMENT_CHARS,
    FitResult,
    estimate_text_tokens,
    fit,
    input_budget,
)
from app.memory.render import materialize
from app.memory.summarize import summarize_range
from app.providers.errors import ContextTooLong
from app.providers.groq import GroqAdapter
from app.providers.types import GenParams, ModelSpec, ResolvedAttachment

CONVERSATION_ID = UUID("22222222-2222-4222-8222-222222222222")
CREATED = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)

ASSISTANT_META = MessageMeta(provider_used="groq", model_used="llama-3.3-70b-versatile")


def _spec(*, context_window: int = 131072, max_output_tokens: int = 32768) -> ModelSpec:
    return ModelSpec(
        slot="general",
        provider="groq",
        model="llama-3.3-70b-versatile",
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        supports_streaming=True,
        supports_vision=False,
        supports_pdf=False,
        supports_system_field=False,
        max_file_bytes=None,
        priority=0,
    )


def _message(seq: int, role: Role, content: list[ContentBlock]) -> CanonicalMessage:
    return CanonicalMessage(
        id=UUID(int=seq),
        conversation_id=CONVERSATION_ID,
        role=role,
        content=content,
        meta=ASSISTANT_META if role == "assistant" else MessageMeta(),
        created_at=CREATED + timedelta(seconds=seq),
        seq=seq,
    )


def _turns(count: int, *, system: bool = True, chars: int = 400) -> list[CanonicalMessage]:
    """``system`` plus ``count`` user/assistant pairs of predictable size."""
    messages: list[CanonicalMessage] = []
    if system:
        messages.append(_message(0, "system", [text_block("S" * chars)]))

    for index in range(count):
        seq = len(messages)
        messages.append(_message(seq, "user", [text_block(f"q{index} " + "u" * chars)]))
        messages.append(_message(seq + 1, "assistant", [text_block(f"a{index} " + "x" * chars)]))

    return messages


def _fit(
    messages: list[CanonicalMessage],
    attachments: list[ResolvedAttachment] | None = None,
    *,
    budget: int,
    strategy: str = "truncate",
) -> FitResult:
    return fit(
        messages,
        attachments or [],
        budget=budget,
        spec=_spec(),
        project=materialize,
        strategy=strategy,  # type: ignore[arg-type]
    )


def _markers(message: CanonicalMessage) -> list[OmissionMarkerBlock]:
    return [block for block in message.content if block["type"] == "omission_marker"]


# --------------------------------------------------------------------------- #
# Step 3 — the budget
# --------------------------------------------------------------------------- #
def test_the_budget_reserves_only_the_output_the_caller_asked_for() -> None:
    """A 512-token request should not have 32k of history thrown away to reserve
    output room nothing was going to use."""
    spec = _spec()

    budget = input_budget(spec, GenParams(max_tokens=512))

    assert budget == 131072 - 512 - int(131072 * 0.05)


def test_the_budget_reserves_the_models_ceiling_when_no_cap_was_asked_for() -> None:
    spec = _spec()

    budget = input_budget(spec, GenParams())

    assert budget == 131072 - 32768 - int(131072 * 0.05)


def test_a_max_tokens_above_the_models_ceiling_does_not_inflate_the_reservation() -> None:
    """`build_payload` clamps the request to the ceiling, so the budget must
    reserve the clamped figure and not the one that was asked for."""
    spec = _spec()

    assert input_budget(spec, GenParams(max_tokens=999_999)) == input_budget(spec, GenParams())


# --------------------------------------------------------------------------- #
# Step 4 — the fast path
# --------------------------------------------------------------------------- #
def test_a_history_that_fits_is_returned_untouched() -> None:
    history = _turns(3)

    result = _fit(history, budget=100_000)

    assert result.messages == history
    assert result.messages_dropped == 0
    assert result.documents_truncated == 0
    assert result.input_tokens > 0


def test_fitting_never_mutates_the_history_it_was_given() -> None:
    """Phase 2's router hands one history to up to three attempts; the second must
    not inherit the first's truncation."""
    history = _turns(6)
    before = [list(message.content) for message in history]

    _fit(history, budget=700)

    assert [list(message.content) for message in history] == before


# --------------------------------------------------------------------------- #
# Step 4 — dropping turns
# --------------------------------------------------------------------------- #
def test_oldest_turns_are_dropped_and_the_system_and_last_question_survive() -> None:
    history = _turns(6)

    result = _fit(history, budget=700)

    assert result.messages_dropped > 0
    assert result.messages[0].role == "system"
    assert result.messages[0].content == history[0].content
    assert result.messages[-1] == history[-1]
    assert result.input_tokens <= 700


def test_turns_are_dropped_in_whole_pairs() -> None:
    """An assistant answer whose question is gone reads as a non-sequitur rather
    than as missing context."""
    history = _turns(6)

    result = _fit(history, budget=700)

    body = [message for message in result.messages if message.role != "system"]
    assert body[0].role == "user"
    for earlier, later in itertools.pairwise(body):
        assert not (earlier.role == "assistant" and later.role == "assistant")


def test_exactly_one_omission_marker_records_the_gap() -> None:
    history = _turns(6)

    result = _fit(history, budget=700)

    marked = [message for message in result.messages if _markers(message)]
    assert len(marked) == 1
    (marker,) = _markers(marked[0])
    assert marker["omitted_count"] == result.messages_dropped
    assert marker["reason"] == "context_truncation"


def test_the_marker_lands_on_the_first_surviving_turn_not_the_system_prompt() -> None:
    """The system message is an instruction, not part of the transcript; a gap
    marker inside it reads as part of the instruction."""
    history = _turns(6)

    result = _fit(history, budget=700)

    assert not _markers(result.messages[0])
    assert _markers(result.messages[1])


def test_a_marker_on_a_dropped_message_is_carried_forward() -> None:
    """A conversation truncated once and truncated again. Losing the old marker
    with the message that held it would tell the model the history is more
    complete than it is."""
    history = _turns(6)
    history[1] = _message(1, "user", [omission_marker(4), *history[1].content])

    result = _fit(history, budget=700)

    assert history[1] not in result.messages, "the message holding the old marker was dropped"
    marked = [message for message in result.messages if _markers(message)]
    assert len(marked) == 1
    (marker,) = _markers(marked[0])
    assert marker["omitted_count"] == result.messages_dropped + 4


def test_a_marker_on_a_surviving_message_is_merged_rather_than_duplicated() -> None:
    """Two markers in one message describe the same gap twice and tell the model
    nothing their sum does not."""
    history = _turns(6)
    history[9] = _message(9, "user", [omission_marker(4), *history[9].content])

    result = _fit(history, budget=700)

    assert result.messages[1].seq == 9, "the message holding the old marker survived"
    marked = [message for message in result.messages if _markers(message)]
    assert len(marked) == 1
    (marker,) = _markers(marked[0])
    assert marker["omitted_count"] == result.messages_dropped + 4


def test_a_history_with_no_system_message_still_truncates() -> None:
    history = _turns(6, system=False)

    result = _fit(history, budget=700)

    assert result.messages_dropped > 0
    assert result.messages[-1] == history[-1]
    assert result.input_tokens <= 700


# --------------------------------------------------------------------------- #
# Step 4 — when nothing can be dropped
# --------------------------------------------------------------------------- #
def test_a_single_message_larger_than_the_budget_raises_context_too_long() -> None:
    history = [_message(0, "user", [text_block("z" * 40_000)])]

    with pytest.raises(ContextTooLong) as caught:
        _fit(history, budget=500)

    assert caught.value.limit_tokens == 500
    assert caught.value.failover_eligible is False


def test_the_pinned_pair_alone_exceeding_the_budget_raises() -> None:
    """Everything droppable is gone and the system prompt plus the question still
    do not fit — the one case D4 cannot rescue."""
    history = _turns(4, chars=2_000)

    with pytest.raises(ContextTooLong):
        _fit(history, budget=600)


# --------------------------------------------------------------------------- #
# Step 4 — documents (Phase 4's path, tested now)
# --------------------------------------------------------------------------- #
def _document(file_hash: str, *, chars: int, name: str = "q3.pdf") -> ResolvedAttachment:
    return ResolvedAttachment(
        file_hash=file_hash,
        filename=name,
        mime="application/pdf",
        size_bytes=chars,
        mode="injected",
        text="D" * chars,
        confidence="high",
    )


def _history_with_documents(*hashes: str) -> list[CanonicalMessage]:
    return [
        _message(0, "system", [text_block("Be terse.")]),
        _message(
            1,
            "user",
            [
                *(
                    file_ref_block(
                        file_hash=file_hash, filename="q3.pdf", mime="application/pdf", bytes=8192
                    )
                    for file_hash in hashes
                ),
                text_block("Summarise these."),
            ],
        ),
    ]


def test_a_document_is_truncated_when_dropping_messages_cannot_help() -> None:
    history = _history_with_documents("aaa")
    attachments = [_document("aaa", chars=8_000)]

    result = _fit(history, attachments, budget=600)

    assert result.documents_truncated == 1
    assert result.messages_dropped == 0
    assert result.input_tokens <= 600

    truncated = result.attachments[0].text
    assert truncated is not None
    assert len(truncated) < 8_000
    assert truncated.startswith("D" * MIN_DOCUMENT_CHARS)
    assert "truncated" in truncated


def test_the_largest_document_is_cut_first() -> None:
    history = _history_with_documents("small", "large")
    attachments = [_document("small", chars=1_200), _document("large", chars=9_000)]

    result = _fit(history, attachments, budget=800)

    by_hash = {attachment.file_hash: attachment for attachment in result.attachments}
    assert by_hash["large"].text != "D" * 9_000
    assert by_hash["small"].text == "D" * 1_200


def test_truncation_rewrites_the_attachment_and_never_the_stored_block() -> None:
    """Invariant 6: a ``file_ref`` holds a hash and nothing else. Truncation is a
    per-request rendering decision that must not reach Postgres."""
    history = _history_with_documents("aaa")
    attachments = [_document("aaa", chars=8_000)]

    result = _fit(history, attachments, budget=600)

    (block,) = [b for b in result.messages[1].content if b["type"] == "file_ref"]
    assert block == history[1].content[0]
    assert attachments[0].text == "D" * 8_000


def test_a_document_that_cannot_be_cut_far_enough_still_raises() -> None:
    """Cutting stops at the floor; below it a fragment is misleading rather than
    degraded, and there is nothing honest left to send."""
    history = _history_with_documents("aaa")
    attachments = [_document("aaa", chars=8_000)]

    with pytest.raises(ContextTooLong):
        _fit(history, attachments, budget=40)


def test_a_native_attachment_is_never_truncated() -> None:
    """Slicing a PDF in half produces a corrupt file, not a shorter one."""
    history = _history_with_documents("aaa")
    native = ResolvedAttachment(
        file_hash="aaa",
        filename="q3.pdf",
        mime="application/pdf",
        size_bytes=8192,
        mode="native",
        data=b"%PDF-1.7",
    )

    with pytest.raises(ContextTooLong):
        _fit(history, [native], budget=1)

    assert native.data == b"%PDF-1.7"


# --------------------------------------------------------------------------- #
# §2.2.7 — the strategy switch
# --------------------------------------------------------------------------- #
def test_the_summarize_strategy_is_a_loud_seam_not_a_silent_fallback() -> None:
    with pytest.raises(NotImplementedError, match=r"2\.2\.7"):
        _fit(_turns(6), budget=700, strategy="summarize")


async def test_the_generator_behind_that_strategy_is_loud_too() -> None:
    """The other half of the seam. ``fit`` refuses to *choose* summarization;
    :func:`summarize_range` refuses to *perform* it. A stub that quietly returned
    ``""`` would let a caller ship a payload whose history had vanished, with
    nothing anywhere raising."""
    adapter = GroqAdapter(
        client=fx.client_raising(httpx.ConnectError("the seam makes no requests")),
        base_url="https://api.groq.com/openai/v1",
    )

    with pytest.raises(NotImplementedError, match="TRUNCATE"):
        await summarize_range(_turns(4), covers_seq=(1, 4), adapter=adapter, spec=_spec())


# --------------------------------------------------------------------------- #
# The estimator
# --------------------------------------------------------------------------- #
def test_the_estimator_is_characters_over_four() -> None:
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("a" * 400) == 100
