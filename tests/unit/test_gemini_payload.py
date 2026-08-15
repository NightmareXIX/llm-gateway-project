"""``build_payload`` — purity, shape, and the committed golden file.

§2.2.6 makes the case: rendering is the one place a provider switch can silently
corrupt a conversation. A golden file turns "the payload changed" from something
noticed in production into a diff in a pull request. Gemini is where that matters
most, because its body agrees with the other two providers on nothing.

The golden includes ``_model``, which is gateway-internal rather than part of the
wire body. That is deliberate: the golden pins the whole return value of
``build_payload``, which is exactly what ``complete`` receives, and hiding the key
would leave the thing ``complete`` depends on unpinned.
``test_gemini_adapter.py`` is where the key is proven never to reach the wire.

Regenerate deliberately, never reflexively::

    python -m tests.unit.test_gemini_payload --bless

If a golden diff is ever confusing, the fix is to understand it, not to bless it.
"""

from __future__ import annotations

import sys

import httpx
import pytest

from app.memory.canonical import CanonicalMessage, MessageMeta, text_block
from app.providers.gemini import MODEL_FIELD, GeminiAdapter
from app.providers.types import GenParams, ModelSpec, ResolvedAttachment
from tests import provider_fixtures as fx

GOLDEN_NAME = "gemini_general"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
MODEL = "gemini-3.6-flash"


def _spec() -> ModelSpec:
    return ModelSpec(
        slot="general",
        provider="gemini",
        model=MODEL,
        context_window=1048576,
        max_output_tokens=65536,
        supports_streaming=True,
        supports_vision=True,
        supports_pdf=True,
        supports_system_field=True,
        max_file_bytes=20000000,
        priority=1,
    )


@pytest.fixture
def adapter() -> GeminiAdapter:
    return GeminiAdapter(client=httpx.AsyncClient(), base_url=BASE_URL)


@pytest.fixture
def spec() -> ModelSpec:
    return _spec()


def _params() -> GenParams:
    return GenParams(temperature=0.2, max_tokens=512, top_p=0.9, stop=["</done>"])


# --------------------------------------------------------------------------- #
# The golden file (§2.1.5 case 1, §2.2.6)
# --------------------------------------------------------------------------- #
def test_the_fixed_history_renders_to_the_committed_payload(
    adapter: GeminiAdapter, spec: ModelSpec
) -> None:
    payload = adapter.build_payload(fx.canonical_history(), spec, _params(), [])

    assert payload == fx.read_golden(GOLDEN_NAME)


def test_build_payload_is_pure(adapter: GeminiAdapter, spec: ModelSpec) -> None:
    """§2.1.5 case 7. Two calls, identical output — no clock, no randomness, no
    accumulated state on the adapter."""
    history = fx.canonical_history()
    params = _params()

    first = adapter.build_payload(history, spec, params, [])
    second = adapter.build_payload(history, spec, params, [])

    assert first == second


def test_build_payload_does_not_mutate_its_arguments(
    adapter: GeminiAdapter, spec: ModelSpec
) -> None:
    """The router hands the same history to up to three attempts. A builder that
    edited it in place would make attempt two render something attempt one never
    saw — and with three providers in the pool, attempt two is usually a different
    adapter entirely."""
    history = fx.canonical_history()
    before = [list(message.content) for message in history]
    params = _params()

    adapter.build_payload(history, spec, params, [])

    assert [list(message.content) for message in history] == before
    assert params.stop == ["</done>"]


# --------------------------------------------------------------------------- #
# Shape — the four things that differ from every OpenAI-shaped provider
# --------------------------------------------------------------------------- #
def test_the_system_prompt_is_hoisted_out_of_the_message_array(
    adapter: GeminiAdapter, spec: ModelSpec
) -> None:
    """Gemini's half of the branch Groq's adapter deliberately does not carry.

    The system prompt becomes a top-level ``system_instruction`` and leaves
    ``contents`` entirely — six canonical messages become five.
    """
    payload = adapter.build_payload(fx.canonical_history(), spec, GenParams(), [])

    assert payload["system_instruction"] == {
        "parts": [{"text": "You are a terse assistant. Answer in one sentence."}]
    }
    assert len(payload["contents"]) == 5
    assert all(entry["role"] != "system" for entry in payload["contents"])


def test_a_history_with_no_system_message_omits_the_field_entirely(
    adapter: GeminiAdapter, spec: ModelSpec
) -> None:
    """An empty ``system_instruction`` is a 400, not a no-op."""
    history = [message for message in fx.canonical_history() if message.role != "system"]

    payload = adapter.build_payload(history, spec, GenParams(), [])

    assert "system_instruction" not in payload
    assert len(payload["contents"]) == 5


def test_the_assistant_role_is_renamed_to_model(adapter: GeminiAdapter, spec: ModelSpec) -> None:
    """Trap 10, pinned. Sending ``"assistant"`` earns a 400 that reads like a
    payload problem because it is one, and the golden file is what catches it
    before a live call does."""
    payload = adapter.build_payload(fx.canonical_history(), spec, GenParams(), [])

    roles = [entry["role"] for entry in payload["contents"]]
    assert roles == ["user", "model", "user", "model", "user"]
    # Scoped to the role fields rather than the whole payload: the system prompt
    # in the frozen history happens to contain the word "assistant", so a
    # stringified-payload check here would pass or fail on prose.
    assert "assistant" not in roles


def test_message_text_lives_in_parts(adapter: GeminiAdapter, spec: ModelSpec) -> None:
    """One part per message, not one part per content block.

    Gemini concatenates ``parts`` with no separator, so a part-per-block payload
    would render ``[4 earlier messages omitted]Recap: what did we…``.
    """
    payload = adapter.build_payload(fx.canonical_history(), spec, GenParams(), [])

    first_user = payload["contents"][0]
    assert len(first_user["parts"]) == 1
    assert first_user["parts"][0]["text"].startswith("[4 earlier messages omitted]\n\n")


def test_the_omission_marker_is_rendered_into_the_prompt(
    adapter: GeminiAdapter, spec: ModelSpec
) -> None:
    """D4's scar has no provider-native representation. Dropping it would leave
    the model unable to tell missing context from a topic never raised."""
    payload = adapter.build_payload(fx.canonical_history(), spec, GenParams(), [])

    assert "[4 earlier messages omitted]" in payload["contents"][0]["parts"][0]["text"]


def test_generation_params_are_nested_and_renamed(adapter: GeminiAdapter, spec: ModelSpec) -> None:
    """None of the knobs are top-level, and two of them are spelled differently."""
    payload = adapter.build_payload(fx.canonical_history(), spec, _params(), [])

    assert payload["generationConfig"] == {
        "temperature": 0.2,
        "maxOutputTokens": 512,
        "topP": 0.9,
        "stopSequences": ["</done>"],
    }
    for leaked in ("temperature", "max_tokens", "maxOutputTokens", "top_p", "stop"):
        assert leaked not in payload


def test_unset_params_are_omitted_rather_than_sent_as_null(
    adapter: GeminiAdapter, spec: ModelSpec
) -> None:
    payload = adapter.build_payload(fx.canonical_history(), spec, GenParams(), [])

    config = payload["generationConfig"]
    assert config == {"temperature": 1.0}


def test_there_is_no_stream_field_at_any_level(adapter: GeminiAdapter, spec: ModelSpec) -> None:
    """Gemini expresses streaming by endpoint, and an unrecognized top-level field
    is a 400 — so ``params.stream`` is unrepresentable in this body. Its absence
    next to Groq's ``"stream": false`` is the point, not an omission."""
    payload = adapter.build_payload(fx.canonical_history(), spec, GenParams(stream=True), [])

    assert "stream" not in payload
    assert "stream" not in payload["generationConfig"]


def test_max_output_tokens_is_clamped_to_what_the_model_can_produce(
    adapter: GeminiAdapter, spec: ModelSpec
) -> None:
    """Turning a config-knowable 400 into a round-trip is a wasted request."""
    params = GenParams(max_tokens=999_999)

    payload = adapter.build_payload(fx.canonical_history(), spec, params, [])

    assert payload["generationConfig"]["maxOutputTokens"] == spec.max_output_tokens


def test_the_model_travels_out_of_band_and_comes_from_the_spec(
    adapter: GeminiAdapter, spec: ModelSpec
) -> None:
    """Gemini names the model in the URL, and ``complete`` only receives the
    payload — so ``build_payload`` leaves it under a gateway-internal key rather
    than on the adapter, where two concurrent requests would cross wires."""
    payload = adapter.build_payload(fx.canonical_history(), spec, GenParams(), [])

    assert payload[MODEL_FIELD] == MODEL
    assert "model" not in payload
    assert "general" not in str(payload)


# --------------------------------------------------------------------------- #
# Attachments (Phase 4's seam, exercised now)
# --------------------------------------------------------------------------- #
def _history_with_file(file_hash: str) -> list[CanonicalMessage]:
    from datetime import UTC, datetime
    from uuid import UUID

    from app.memory.canonical import file_ref_block

    return [
        CanonicalMessage(
            id=UUID(int=1),
            conversation_id=UUID(int=99),
            role="user",
            content=[
                file_ref_block(
                    file_hash=file_hash, filename="q3.pdf", mime="application/pdf", bytes=8192
                ),
                text_block("Summarise the revenue table."),
            ],
            meta=MessageMeta(),
            created_at=datetime(2026, 8, 10, tzinfo=UTC),
            seq=0,
        )
    ]


def test_an_extracted_document_is_wrapped_in_the_same_envelope_groq_uses(
    adapter: GeminiAdapter, spec: ModelSpec
) -> None:
    """§2.2.5 step 2, and the reason ``document_envelope`` is shared rather than
    copied per adapter: delimiting extracted text so the model can tell document
    content from user instruction is a decision every provider must make
    identically, and a per-adapter copy would let one drift while its own golden
    file stayed green."""
    attachment = ResolvedAttachment(
        file_hash="abc123",
        filename="q3.pdf",
        mime="application/pdf",
        size_bytes=8192,
        mode="injected",
        text="Revenue rose 12% quarter over quarter.",
        confidence="high",
    )

    payload = adapter.build_payload(_history_with_file("abc123"), spec, GenParams(), [attachment])

    text = payload["contents"][0]["parts"][0]["text"]
    assert '<document name="q3.pdf" source="extracted" confidence="high">' in text
    assert "Revenue rose 12% quarter over quarter." in text
    assert "</document>" in text
    assert "Summarise the revenue table." in text


def test_an_unresolved_file_ref_is_a_loud_failure_not_a_silent_drop(
    adapter: GeminiAdapter, spec: ModelSpec
) -> None:
    """Dropping it would answer a question about a document the model never saw."""
    with pytest.raises(ValueError, match="unresolved"):
        adapter.build_payload(_history_with_file("abc123"), spec, GenParams(), [])


def test_gemini_refuses_to_embed_a_file_natively_yet(
    adapter: GeminiAdapter, spec: ModelSpec
) -> None:
    """The one branch this adapter deliberately leaves unbuilt.

    Gemini *can* read a PDF natively via ``inline_data`` — unlike Groq, which
    cannot — but no ``file_ref`` can exist before ``POST /v1/files`` does, which is
    Phase 4. Building it now would ship an untested path presenting itself as a
    capability. The refusal names both the capability and the phase, so the failure
    explains itself.
    """
    attachment = ResolvedAttachment(
        file_hash="abc123",
        filename="q3.pdf",
        mime="application/pdf",
        size_bytes=8192,
        mode="native",
        data=b"%PDF-1.7",
    )

    with pytest.raises(NotImplementedError, match="inline_data") as caught:
        adapter.build_payload(_history_with_file("abc123"), spec, GenParams(), [attachment])

    assert "Phase 4" in str(caught.value)


# --------------------------------------------------------------------------- #
# Regeneration
# --------------------------------------------------------------------------- #
def _bless() -> None:
    """Rewrite the golden file from the current implementation."""
    adapter = GeminiAdapter(client=httpx.AsyncClient(), base_url=BASE_URL)
    payload = adapter.build_payload(fx.canonical_history(), _spec(), _params(), [])
    path = fx.GOLDEN_ROOT / f"{GOLDEN_NAME}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fx.dump_golden(payload), encoding="utf-8")
    print(f"wrote {path}")


if __name__ == "__main__":  # pragma: no cover
    if "--bless" in sys.argv:
        _bless()
    else:
        print(__doc__)
