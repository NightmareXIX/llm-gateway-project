"""The render pipeline — §2.2.5's six steps end to end.

The load-bearing test here is the first one: rendering the fixed six-message
history through the whole pipeline must produce *exactly* the payload the
conformance suite already blesses. That is what makes "every payload comes out of
``render``" a safe rule rather than a risky one — Step 8's endpoint can route
through the pipeline knowing the pipeline is transparent when nothing needs
dropping.

Everything else here is about the seams: that step 1 refuses rather than shrugs,
that step 0's invariant check runs before a provider is told anything, and that
the report says what actually happened.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.memory.canonical import (
    CanonicalMessage,
    FileRefBlock,
    InvariantViolation,
    MessageMeta,
    file_ref_block,
    text_block,
)
from app.memory.render import (
    NoAttachments,
    RenderReport,
    document_envelope,
    materialize,
    render,
)
from app.providers.errors import ContextTooLong
from app.providers.groq import GroqAdapter
from app.providers.types import GenParams, ModelSpec, ResolvedAttachment
from tests import provider_fixtures as fx

GOLDEN_NAME = "groq_general"


@pytest.fixture
def adapter() -> GroqAdapter:
    return GroqAdapter(
        client=fx.client_raising(httpx.ConnectError("render does not make requests")),
        base_url="https://api.groq.com/openai/v1",
    )


def _spec(*, context_window: int = 131072, max_output_tokens: int = 32768) -> ModelSpec:
    return ModelSpec(
        slot="general",
        provider="groq",
        model="openai/gpt-oss-120b",
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        supports_streaming=True,
        supports_vision=False,
        supports_pdf=False,
        supports_system_field=False,
        max_file_bytes=None,
        priority=0,
    )


@pytest.fixture
def spec() -> ModelSpec:
    return _spec()


def _params() -> GenParams:
    return GenParams(temperature=0.2, max_tokens=512, top_p=0.9, stop=["</done>"])


def _message(seq: int, role: Any, content: list[Any]) -> CanonicalMessage:
    return CanonicalMessage(
        id=UUID(int=seq),
        conversation_id=UUID(int=99),
        role=role,
        content=content,
        meta=MessageMeta(provider_used="groq") if role == "assistant" else MessageMeta(),
        created_at=datetime(2026, 8, 10, tzinfo=UTC),
        seq=seq,
    )


class StubResolver:
    """Stands in for Phase 4's perception lane."""

    def __init__(self, *attachments: ResolvedAttachment) -> None:
        self._attachments = list(attachments)
        self.calls: list[tuple[int, str]] = []

    async def resolve(self, refs: list[FileRefBlock], spec: ModelSpec) -> list[ResolvedAttachment]:
        self.calls.append((len(refs), spec.model))
        return self._attachments


# --------------------------------------------------------------------------- #
# Transparency — the pipeline is a no-op when nothing needs dropping
# --------------------------------------------------------------------------- #
async def test_the_pipeline_produces_exactly_the_committed_golden_payload(
    adapter: GroqAdapter, spec: ModelSpec
) -> None:
    """§2.2.6. If this ever diverges from ``test_groq_payload``'s assertion on the
    same file, then a payload's contents depend on whether the caller went through
    the pipeline — and one of the two call paths is wrong."""
    payload, report = await render(fx.canonical_history(), spec, _params(), adapter)

    assert payload == fx.read_golden(GOLDEN_NAME)
    assert report.messages_dropped == 0
    assert report.documents_truncated == 0
    assert report.truncated is False


async def test_the_report_carries_the_adapters_own_estimate(
    adapter: GroqAdapter, spec: ModelSpec
) -> None:
    """Measured on the finished payload, not on the canonical text fitting sized —
    it is the number Phase 3 reserves quota against."""
    history = fx.canonical_history()

    payload, report = await render(history, spec, _params(), adapter)

    assert report.estimated_tokens == adapter.estimate_tokens(payload)
    assert report.estimated_tokens > 0


async def test_a_phase_1_render_reports_no_attachments_and_no_degradation(
    adapter: GroqAdapter, spec: ModelSpec
) -> None:
    _, report = await render(fx.canonical_history(), spec, _params(), adapter)

    assert report.attachments_native == 0
    assert report.attachments_injected == 0
    assert report.degraded is False


async def test_rendering_does_not_mutate_the_history_it_was_given(
    adapter: GroqAdapter, spec: ModelSpec
) -> None:
    """Phase 2's router renders the same history up to three times."""
    history = fx.canonical_history()
    before = [list(message.content) for message in history]

    await render(history, _spec(context_window=100, max_output_tokens=16), _params(), adapter)

    assert [list(message.content) for message in history] == before
    assert len(history) == 6


# --------------------------------------------------------------------------- #
# Step 0 — the invariants
# --------------------------------------------------------------------------- #
async def test_a_history_that_breaks_an_invariant_never_reaches_the_adapter(
    adapter: GroqAdapter, spec: ModelSpec
) -> None:
    """History arrives from a JSONB column. The renderer is the last place that
    can notice before a provider is told about it."""
    history = [
        _message(0, "user", [text_block("hello")]),
        _message(1, "assistant", [text_block("hi")]),
        _message(2, "assistant", [text_block("hi again")]),
    ]

    with pytest.raises(InvariantViolation) as caught:
        await render(history, spec, _params(), adapter)

    assert caught.value.invariant == 3


# --------------------------------------------------------------------------- #
# Step 1 — attachment resolution
# --------------------------------------------------------------------------- #
async def test_a_file_ref_in_phase_1_is_a_loud_failure_not_a_silent_drop(
    adapter: GroqAdapter, spec: ModelSpec
) -> None:
    """Resolving to nothing would answer a question about a document the model
    never saw, and the user could not tell that from a bad answer."""
    history = [
        _message(
            0,
            "user",
            [
                file_ref_block(
                    file_hash="abc123", filename="q3.pdf", mime="application/pdf", bytes=8192
                ),
                text_block("Summarise this."),
            ],
        )
    ]

    with pytest.raises(NotImplementedError, match="Phase 4"):
        await render(history, spec, _params(), adapter)


async def test_the_resolver_sees_every_reference_and_the_target_model(
    adapter: GroqAdapter, spec: ModelSpec
) -> None:
    """The same stored ``file_ref`` is native for Gemini and injected for Groq, so
    resolution is per-request against the answering model."""
    attachment = ResolvedAttachment(
        file_hash="abc123",
        filename="q3.pdf",
        mime="application/pdf",
        size_bytes=8192,
        mode="injected",
        text="Revenue rose 12%.",
        confidence="low",
    )
    resolver = StubResolver(attachment)
    history = [
        _message(
            0,
            "user",
            [
                file_ref_block(
                    file_hash="abc123", filename="q3.pdf", mime="application/pdf", bytes=8192
                ),
                text_block("Summarise this."),
            ],
        )
    ]

    payload, report = await render(history, spec, _params(), adapter, resolver=resolver)

    assert resolver.calls == [(1, "openai/gpt-oss-120b")]
    assert report.attachments_injected == 1
    assert report.attachments_native == 0
    assert report.degraded is True, "low-confidence extraction is the local-OCR path"
    assert '<document name="q3.pdf"' in payload["messages"][0]["content"]


async def test_an_empty_history_renders_without_touching_the_resolver(
    adapter: GroqAdapter, spec: ModelSpec
) -> None:
    resolver = StubResolver()

    payload, report = await render([], spec, _params(), adapter, resolver=resolver)

    assert payload["messages"] == []
    assert report.estimated_tokens >= 0


async def test_the_default_resolver_accepts_a_history_with_no_files() -> None:
    assert await NoAttachments().resolve([], _spec()) == []


# --------------------------------------------------------------------------- #
# Steps 3 and 4 — budget and fitting, through the pipeline
# --------------------------------------------------------------------------- #
async def test_a_history_over_the_budget_is_truncated_and_reported(
    adapter: GroqAdapter, spec: ModelSpec
) -> None:
    payload, report = await render(
        fx.canonical_history(),
        _spec(context_window=100, max_output_tokens=16),
        _params(),
        adapter,
    )

    assert report.messages_dropped > 0
    assert report.truncated is True
    assert len(payload["messages"]) < 6
    assert payload["messages"][0]["role"] == "system"
    assert "earlier messages omitted" in payload["messages"][1]["content"]


async def test_a_model_whose_output_ceiling_swallows_its_window_fails_at_render(
    adapter: GroqAdapter,
) -> None:
    """A configuration bug, caught with a message naming the file to fix rather
    than as a mystifying provider 400."""
    with pytest.raises(ContextTooLong, match=r"providers\.yaml"):
        await render(
            fx.canonical_history(),
            _spec(context_window=8_192, max_output_tokens=8_192),
            GenParams(),
            adapter,
        )


# --------------------------------------------------------------------------- #
# Materialization and the shared envelope
# --------------------------------------------------------------------------- #
def test_the_envelope_is_the_one_the_adapters_emit() -> None:
    """One definition of the delimiter, shared by every provider. An adapter with
    its own copy could drift while its own golden file stayed green."""
    attachment = ResolvedAttachment(
        file_hash="abc123",
        filename="q3.pdf",
        mime="application/pdf",
        size_bytes=8192,
        mode="injected",
        text="Revenue rose 12% quarter over quarter.",
        confidence="high",
    )

    envelope = document_envelope(attachment)

    assert envelope.startswith('<document name="q3.pdf" source="extracted" confidence="high">')
    assert envelope.endswith("</document>")
    assert "Revenue rose 12% quarter over quarter." in envelope


def test_the_envelope_omits_confidence_when_there_is_none() -> None:
    attachment = ResolvedAttachment(
        file_hash="abc123",
        filename="q3.pdf",
        mime="application/pdf",
        size_bytes=8192,
        mode="injected",
        text="…",
    )

    assert document_envelope(attachment).startswith('<document name="q3.pdf" source="extracted">')


def test_materialize_measures_what_the_adapter_will_send(adapter: GroqAdapter) -> None:
    """The projection is a tape measure, not a second renderer — but a tape
    measure that disagreed with the thing it measures would make every budget
    wrong. For a text-only history the two must agree exactly."""
    history = fx.canonical_history()
    payload = adapter.build_payload(history, _spec(), GenParams(), [])

    projected = [materialize(message, {}) for message in history]

    assert projected == [message["content"] for message in payload["messages"]]


def test_materialize_is_lenient_about_an_unresolved_reference() -> None:
    """Step 1 owns resolution and the adapter raises on the same condition. A
    tape measure that raised would put the error message on the wrong problem."""
    message = _message(
        0,
        "user",
        [
            file_ref_block(
                file_hash="abc123", filename="q3.pdf", mime="application/pdf", bytes=8192
            ),
            text_block("Summarise this."),
        ],
    )

    assert materialize(message, {}) == "Summarise this."


def test_a_native_attachment_is_measured_as_a_placeholder_not_as_prompt_text() -> None:
    """Base64 length is not a token count; providers bill multimodal input on
    their own terms."""
    attachment = ResolvedAttachment(
        file_hash="abc123",
        filename="q3.pdf",
        mime="application/pdf",
        size_bytes=8192,
        mode="native",
        data=b"%PDF-1.7" * 1000,
    )
    message = _message(
        0,
        "user",
        [file_ref_block(file_hash="abc123", filename="q3.pdf", mime="application/pdf", bytes=8192)],
    )

    projected = materialize(message, {"abc123": attachment})

    assert projected == "[application/pdf attachment: q3.pdf]"


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #
def test_an_empty_report_claims_nothing_was_lost() -> None:
    assert RenderReport().truncated is False
    assert RenderReport(documents_truncated=1).truncated is True
    assert RenderReport(messages_dropped=1).truncated is True
