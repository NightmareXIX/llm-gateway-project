"""Contract B's six invariants, plus the parsing that guards the DB boundary.

Pure unit tests — no database, no fixtures, no clock. That is the whole reason
``app/memory/canonical.py`` does no I/O: the rules that decide whether a
conversation is well-formed are the ones most worth exhausting, and here they
cost milliseconds to exhaust.

Each invariant gets a passing case and a violating case, and every violation is
asserted to carry its *number* from §2.2.3 — a test that only checked "something
raised" would still pass if the wrong rule fired.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from app.memory.canonical import (
    SCHEMA_VERSION,
    CanonicalMessage,
    ContentBlock,
    InvariantViolation,
    MessageMeta,
    Role,
    file_ref_block,
    omission_marker,
    parse_content,
    parse_meta,
    parse_role,
    text_block,
    validate,
    with_content,
)

CONVERSATION_ID = uuid4()
CREATED_AT = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)

ASSISTANT_META = MessageMeta(
    provider_used="groq",
    model_used="llama-3.3-70b-versatile",
    slot_used="llm2",
    requested_slot="auto",
    tokens_in=120,
    tokens_out=40,
)


def message(
    *,
    seq: int,
    role: Role,
    content: list[ContentBlock] | None = None,
    meta: MessageMeta | None = None,
) -> CanonicalMessage:
    """A well-formed message, with the provenance invariant 5 demands.

    ``meta`` defaults to the right thing for the role so a test about ordering
    does not have to restate the provenance rule to stay valid.
    """
    if meta is None:
        meta = ASSISTANT_META if role == "assistant" else MessageMeta()
    return CanonicalMessage(
        id=uuid4(),
        conversation_id=CONVERSATION_ID,
        role=role,
        content=content if content is not None else [text_block(f"message {seq}")],
        meta=meta,
        created_at=CREATED_AT,
        seq=seq,
    )


def well_formed_history() -> list[CanonicalMessage]:
    """System prompt, then two complete turns. The baseline every test mutates."""
    return [
        message(seq=0, role="system"),
        message(seq=1, role="user"),
        message(seq=2, role="assistant"),
        message(seq=3, role="user"),
        message(seq=4, role="assistant"),
    ]


def violation(history: list[CanonicalMessage]) -> InvariantViolation:
    """Assert the history is rejected and hand back the exception to inspect."""
    with pytest.raises(InvariantViolation) as excinfo:
        validate(history)
    return excinfo.value


# --------------------------------------------------------------------------- #
# The baseline
# --------------------------------------------------------------------------- #
def test_well_formed_history_validates() -> None:
    validate(well_formed_history())


def test_empty_history_validates() -> None:
    """A conversation with no messages yet is not malformed, just new."""
    validate([])


def test_history_without_a_system_message_validates() -> None:
    """Invariant 1 caps system messages at one; it does not require one.

    Seq 0 being a user message is the normal case for a conversation started
    without a system prompt, and reading invariant 1 as "seq 0 is the system
    message" is the natural misreading.
    """
    validate([message(seq=0, role="user"), message(seq=1, role="assistant")])


# --------------------------------------------------------------------------- #
# Invariant 1 — at most one system message, always at seq 0
# --------------------------------------------------------------------------- #
def test_second_system_message_is_rejected() -> None:
    history = well_formed_history()
    history[3] = message(seq=3, role="system")
    assert violation(history).invariant == 1


def test_system_message_after_seq_zero_is_rejected() -> None:
    history = [message(seq=0, role="user"), message(seq=1, role="system")]
    assert violation(history).invariant == 1


# --------------------------------------------------------------------------- #
# Invariant 2 — seq unique and gap-free
# --------------------------------------------------------------------------- #
def test_gap_in_seq_is_rejected() -> None:
    history = [
        message(seq=0, role="user"),
        message(seq=2, role="assistant"),  # 1 is missing
    ]
    assert violation(history).invariant == 2


def test_duplicate_seq_is_rejected() -> None:
    history = [
        message(seq=0, role="user"),
        message(seq=1, role="assistant"),
        message(seq=1, role="user"),
    ]
    assert violation(history).invariant == 2


def test_history_not_starting_at_zero_is_rejected() -> None:
    assert violation([message(seq=1, role="user")]).invariant == 2


def test_out_of_order_history_is_rejected_not_silently_sorted() -> None:
    """Sorting here would hide the bug that produced the ordering."""
    history = [
        message(seq=0, role="user"),
        message(seq=2, role="user"),
        message(seq=1, role="assistant"),
    ]
    assert violation(history).invariant == 2


# --------------------------------------------------------------------------- #
# Invariant 3 — alternation, asymmetrically
# --------------------------------------------------------------------------- #
def test_consecutive_user_messages_are_tolerated() -> None:
    """Someone sends a follow-up before the answer lands. That is not corruption."""
    validate(
        [
            message(seq=0, role="user"),
            message(seq=1, role="user"),
            message(seq=2, role="assistant"),
        ]
    )


def test_consecutive_assistant_messages_are_rejected() -> None:
    """Every assistant turn answers a user turn; two in a row means one was lost."""
    history = [
        message(seq=0, role="user"),
        message(seq=1, role="assistant"),
        message(seq=2, role="assistant"),
    ]
    assert violation(history).invariant == 3


# --------------------------------------------------------------------------- #
# Invariant 4 — content is never empty
# --------------------------------------------------------------------------- #
def test_empty_content_is_rejected() -> None:
    history = [message(seq=0, role="user", content=[])]
    assert violation(history).invariant == 4


def test_parse_content_rejects_an_empty_array() -> None:
    with pytest.raises(InvariantViolation) as excinfo:
        parse_content([])
    assert excinfo.value.invariant == 4


def test_parse_content_rejects_a_non_array() -> None:
    with pytest.raises(InvariantViolation) as excinfo:
        parse_content({"type": "text", "text": "not wrapped in a list"})
    assert excinfo.value.invariant == 4


# --------------------------------------------------------------------------- #
# Invariant 5 — provenance on assistant messages, absent on user messages
# --------------------------------------------------------------------------- #
def test_assistant_without_provider_used_is_rejected() -> None:
    history = [
        message(seq=0, role="user"),
        message(seq=1, role="assistant", meta=MessageMeta(model_used="llama-3.3-70b")),
    ]
    assert violation(history).invariant == 5


def test_user_message_claiming_a_provider_is_rejected() -> None:
    """The half of invariant 5 that is easy to forget.

    Without it a user message can inherit the previous assistant's provenance,
    which quietly double-counts tokens in the usage numbers.
    """
    history = [message(seq=0, role="user", meta=MessageMeta(provider_used="groq"))]
    assert violation(history).invariant == 5


def test_system_message_needs_no_provenance() -> None:
    validate([message(seq=0, role="system", meta=MessageMeta())])


# --------------------------------------------------------------------------- #
# Invariant 6 — a file_ref holds only the hash
# --------------------------------------------------------------------------- #
def test_file_ref_block_round_trips() -> None:
    block = file_ref_block(
        file_hash="a" * 64, filename="q3.pdf", mime="application/pdf", bytes=91_234
    )
    assert parse_content([block]) == [block]


@pytest.mark.parametrize("smuggled", ["extracted", "text", "data", "content"])
def test_file_ref_carrying_extracted_content_is_rejected(smuggled: str) -> None:
    """The invariant's actual failure mode, and the reason for the extra-key check.

    A ``file_ref`` that caches its own extraction freezes it forever — improving
    the extractor would then stop improving old conversations, which is the one
    property §2.2.3 names as the reason for the rule.
    """
    block: dict[str, Any] = {
        "type": "file_ref",
        "file_hash": "a" * 64,
        "filename": "q3.pdf",
        "mime": "application/pdf",
        "bytes": 91_234,
        smuggled: "…the entire text of the PDF…",
    }
    with pytest.raises(InvariantViolation) as excinfo:
        parse_content([block])
    assert excinfo.value.invariant == 6


def test_file_ref_missing_a_field_is_rejected() -> None:
    block = {"type": "file_ref", "file_hash": "a" * 64, "filename": "q3.pdf", "bytes": 1}
    with pytest.raises(InvariantViolation, match="mime"):
        parse_content([block])


# --------------------------------------------------------------------------- #
# Block parsing
# --------------------------------------------------------------------------- #
def test_text_block_round_trips() -> None:
    assert parse_content([text_block("hello")]) == [{"type": "text", "text": "hello"}]


def test_omission_marker_round_trips() -> None:
    marker = omission_marker(7)
    assert parse_content([marker]) == [marker]
    assert marker["reason"] == "context_truncation"


@pytest.mark.parametrize("reserved", ["tool_call", "tool_result", "summary"])
def test_reserved_block_types_are_rejected_loudly(reserved: str) -> None:
    """Reserved, not merely unimplemented.

    A renderer that silently skipped an unknown block would drop a tool call from
    a payload and produce a subtly wrong answer instead of an obvious error.
    """
    with pytest.raises(InvariantViolation, match="reserved"):
        parse_content([{"type": reserved, "whatever": 1}])


def test_unknown_block_type_is_rejected() -> None:
    with pytest.raises(InvariantViolation, match="unknown content block type"):
        parse_content([{"type": "image_url", "url": "https://example.com/a.png"}])


def test_block_without_a_type_is_rejected() -> None:
    with pytest.raises(InvariantViolation, match="string 'type'"):
        parse_content([{"text": "no type key"}])


def test_block_field_of_the_wrong_type_is_rejected() -> None:
    with pytest.raises(InvariantViolation, match=r"text\.text must be str"):
        parse_content([{"type": "text", "text": 42}])


def test_omission_marker_with_an_unknown_reason_is_rejected() -> None:
    with pytest.raises(InvariantViolation, match="reason"):
        parse_content([{"type": "omission_marker", "omitted_count": 3, "reason": "vibes"}])


def test_omission_marker_must_omit_something() -> None:
    with pytest.raises(InvariantViolation, match="at least 1"):
        parse_content(
            [{"type": "omission_marker", "omitted_count": 0, "reason": "context_truncation"}]
        )


def test_boolean_is_not_accepted_where_an_integer_is_required() -> None:
    """``bool`` subclasses ``int``; for a byte count it is never what was meant."""
    with pytest.raises(InvariantViolation, match="bytes"):
        parse_content(
            [
                {
                    "type": "file_ref",
                    "file_hash": "a" * 64,
                    "filename": "q3.pdf",
                    "mime": "application/pdf",
                    "bytes": True,
                }
            ]
        )


def test_parse_role_rejects_an_unknown_role() -> None:
    assert parse_role("assistant") == "assistant"
    with pytest.raises(InvariantViolation, match="unknown role"):
        parse_role("tool")


# --------------------------------------------------------------------------- #
# MessageMeta
# --------------------------------------------------------------------------- #
def test_meta_round_trips_through_jsonb() -> None:
    meta = MessageMeta(
        provider_used="gemini",
        model_used="gemini-flash",
        slot_used="llm1",
        requested_slot="llm2",
        substituted=True,
        attempts=2,
        tokens_in=812,
        tokens_out=340,
        wasted_tokens_out=96,
        degraded=True,
        extraction_tier="llm",
        messages_dropped=42,
    )
    assert parse_meta(meta.to_jsonb()) == meta


def test_default_meta_round_trips_with_every_key_present() -> None:
    """Written in full, not sparsely.

    A reader should never need to know which app version wrote a row to tell
    "false" apart from "not recorded yet".
    """
    stored = MessageMeta().to_jsonb()
    assert set(stored) == {
        "provider_used",
        "model_used",
        "slot_used",
        "requested_slot",
        "substituted",
        "attempts",
        "tokens_in",
        "tokens_out",
        "wasted_tokens_out",
        "degraded",
        "extraction_tier",
        "messages_dropped",
    }
    assert parse_meta(stored) == MessageMeta()


def test_meta_tolerates_missing_and_unknown_keys() -> None:
    """Version skew in both directions stays readable; wrong types do not."""
    meta = parse_meta({"provider_used": "groq", "invented_in_phase_9": "ignored"})
    assert meta.provider_used == "groq"
    assert meta.attempts == 1
    assert meta.substituted is False


def test_meta_without_messages_dropped_reads_back_as_zero() -> None:
    """D34, trap 7: a row written before this field existed is not backfilled —
    nobody knows whether that turn was truncated, so the honest reading is the
    default, not a guess."""
    meta = parse_meta({"provider_used": "groq"})
    assert meta.messages_dropped == 0


def test_meta_rejects_a_wrongly_typed_value() -> None:
    with pytest.raises(InvariantViolation, match="attempts"):
        parse_meta({"attempts": "two"})


def test_meta_rejects_an_unknown_extraction_tier() -> None:
    with pytest.raises(InvariantViolation, match="extraction_tier"):
        parse_meta({"extraction_tier": "psychic"})


def test_empty_meta_column_parses_to_defaults() -> None:
    """``messages.meta`` defaults to ``'{}'::jsonb`` in the schema."""
    assert parse_meta({}) == MessageMeta()


# --------------------------------------------------------------------------- #
# Helpers on the message itself
# --------------------------------------------------------------------------- #
def test_text_concatenates_only_text_blocks() -> None:
    msg = message(
        seq=0,
        role="user",
        content=[
            text_block("look at "),
            file_ref_block(file_hash="a" * 64, filename="q3.pdf", mime="application/pdf", bytes=10),
            text_block("this"),
        ],
    )
    assert msg.text() == "look at this"
    assert [block["filename"] for block in msg.file_refs()] == ["q3.pdf"]


def test_with_content_leaves_the_original_untouched() -> None:
    """The render path rewrites content without touching what is stored."""
    original = message(seq=0, role="user")
    rewritten = with_content(original, [text_block("rendered")])
    assert original.content == [text_block("message 0")]
    assert rewritten.content == [text_block("rendered")]
    assert rewritten.id == original.id


def test_schema_version_defaults_to_the_current_one() -> None:
    assert message(seq=0, role="user").schema_version == SCHEMA_VERSION
