"""``build_payload`` — purity, shape, and the committed golden file.

§2.2.6 makes the case: rendering is the one place a provider switch can silently
corrupt a conversation. A golden file turns "the payload changed" from something
noticed in production into a diff in a pull request.

This file deliberately mirrors ``test_groq_payload.py`` almost line for line, and
the near-duplication is the finding rather than a smell: OpenRouter and Groq
genuinely agree on the request body and disagree on nearly everything else. The
two goldens sitting side by side are what show that — and what would catch it if
one adapter's payload quietly drifted from the other's.

Regenerate deliberately, never reflexively::

    python -m tests.unit.test_openrouter_payload --bless

If a golden diff is ever confusing, the fix is to understand it, not to bless it.
"""

from __future__ import annotations

import sys

import httpx
import pytest

from app.memory.canonical import CanonicalMessage, MessageMeta, text_block
from app.providers.openrouter import OpenRouterAdapter
from app.providers.types import GenParams, ModelSpec, ResolvedAttachment
from tests import provider_fixtures as fx

GOLDEN_NAME = "openrouter_general"
BASE_URL = "https://openrouter.ai/api/v1"
MODEL = "nvidia/nemotron-3-super-120b-a12b:free"


def _spec() -> ModelSpec:
    return ModelSpec(
        slot="general",
        provider="openrouter",
        model=MODEL,
        context_window=262144,
        max_output_tokens=262144,
        supports_streaming=True,
        supports_vision=False,
        supports_pdf=False,
        supports_system_field=False,
        max_file_bytes=None,
        priority=2,
    )


@pytest.fixture
def adapter() -> OpenRouterAdapter:
    return OpenRouterAdapter(client=httpx.AsyncClient(), base_url=BASE_URL)


@pytest.fixture
def spec() -> ModelSpec:
    return _spec()


def _params() -> GenParams:
    return GenParams(temperature=0.2, max_tokens=512, top_p=0.9, stop=["</done>"])


# --------------------------------------------------------------------------- #
# The golden file (§2.1.5 case 1, §2.2.6)
# --------------------------------------------------------------------------- #
def test_the_fixed_history_renders_to_the_committed_payload(
    adapter: OpenRouterAdapter, spec: ModelSpec
) -> None:
    payload = adapter.build_payload(fx.canonical_history(), spec, _params(), [])

    assert payload == fx.read_golden(GOLDEN_NAME)


def test_build_payload_is_pure(adapter: OpenRouterAdapter, spec: ModelSpec) -> None:
    """§2.1.5 case 7. Two calls, identical output — no clock, no randomness, no
    accumulated state on the adapter. Notable here because this is the one adapter
    that *holds* a clock; it must stay nowhere near payload construction."""
    history = fx.canonical_history()
    params = _params()

    first = adapter.build_payload(history, spec, params, [])
    second = adapter.build_payload(history, spec, params, [])

    assert first == second


def test_build_payload_does_not_mutate_its_arguments(
    adapter: OpenRouterAdapter, spec: ModelSpec
) -> None:
    """The router hands the same history to up to three attempts. A builder that
    edited it in place would make attempt two render something attempt one never
    saw."""
    history = fx.canonical_history()
    before = [list(message.content) for message in history]
    params = _params()

    adapter.build_payload(history, spec, params, [])

    assert [list(message.content) for message in history] == before
    assert params.stop == ["</done>"]


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #
def test_the_system_prompt_stays_in_the_message_array(
    adapter: OpenRouterAdapter, spec: ModelSpec
) -> None:
    """OpenRouter is OpenAI-shaped: `supports_system_field` is False, so there is
    no top-level system field to populate. Gemini's adapter is where that branch
    lives."""
    payload = adapter.build_payload(fx.canonical_history(), spec, GenParams(), [])

    assert "system" not in payload
    assert "system_instruction" not in payload
    assert payload["messages"][0]["role"] == "system"


def test_the_assistant_role_is_left_alone(adapter: OpenRouterAdapter, spec: ModelSpec) -> None:
    """The mirror image of Gemini's rename, asserted so the two files disagree in
    writing rather than by accident."""
    payload = adapter.build_payload(fx.canonical_history(), spec, GenParams(), [])

    roles = [message["role"] for message in payload["messages"]]
    assert roles == ["system", "user", "assistant", "user", "assistant", "user"]


def test_the_omission_marker_is_rendered_into_the_prompt(
    adapter: OpenRouterAdapter, spec: ModelSpec
) -> None:
    """D4's scar has no provider-native representation. Dropping it would leave
    the model unable to tell missing context from a topic never raised."""
    payload = adapter.build_payload(fx.canonical_history(), spec, GenParams(), [])

    assert "[4 earlier messages omitted]" in payload["messages"][1]["content"]


def test_generation_params_are_omitted_rather_than_sent_as_null(
    adapter: OpenRouterAdapter, spec: ModelSpec
) -> None:
    payload = adapter.build_payload(fx.canonical_history(), spec, GenParams(), [])

    assert "max_tokens" not in payload
    assert "top_p" not in payload
    assert "stop" not in payload
    assert payload["temperature"] == 1.0
    assert payload["stream"] is False


def test_max_tokens_is_clamped_to_what_the_model_can_produce(
    adapter: OpenRouterAdapter, spec: ModelSpec
) -> None:
    """Turning a config-knowable 400 into a round-trip is a wasted request."""
    params = GenParams(max_tokens=999_999)

    payload = adapter.build_payload(fx.canonical_history(), spec, params, [])

    assert payload["max_tokens"] == spec.max_output_tokens


def test_the_free_suffix_reaches_the_wire_verbatim(
    adapter: OpenRouterAdapter, spec: ModelSpec
) -> None:
    """Trap 9 at the adapter layer.

    ``…:free`` and the same name without the suffix are different models with
    different billing, so a well-meant normalization here would silently route a
    project whose entire premise is "runs on free tiers" to the paid variant. The
    config test asserts the YAML still declares it; this one asserts nothing
    between the YAML and the socket eats it.
    """
    payload = adapter.build_payload(fx.canonical_history(), spec, GenParams(), [])

    assert payload["model"] == MODEL
    assert payload["model"].endswith(":free")
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


def test_an_extracted_document_is_wrapped_in_a_delimited_envelope(
    adapter: OpenRouterAdapter, spec: ModelSpec
) -> None:
    """§2.2.5 step 2. Delimited rather than concatenated so the model can tell
    document content from user instruction — the cheapest injection mitigation
    available, and the difference between summarising a PDF and obeying it."""
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

    content = payload["messages"][0]["content"]
    assert '<document name="q3.pdf" source="extracted" confidence="high">' in content
    assert "Revenue rose 12% quarter over quarter." in content
    assert "</document>" in content
    assert "Summarise the revenue table." in content


def test_an_unresolved_file_ref_is_a_loud_failure_not_a_silent_drop(
    adapter: OpenRouterAdapter, spec: ModelSpec
) -> None:
    """Dropping it would answer a question about a document the model never saw."""
    with pytest.raises(ValueError, match="unresolved"):
        adapter.build_payload(_history_with_file("abc123"), spec, GenParams(), [])


def test_openrouter_refuses_to_embed_a_file_natively(
    adapter: OpenRouterAdapter, spec: ModelSpec
) -> None:
    """The declared OpenRouter models are text-only. Some of its upstreams are not,
    and building for them before ``capability_registry`` can say which is how a
    request ends up sending image bytes to a text model."""
    attachment = ResolvedAttachment(
        file_hash="abc123",
        filename="q3.pdf",
        mime="application/pdf",
        size_bytes=8192,
        mode="native",
        data=b"%PDF-1.7",
    )

    with pytest.raises(NotImplementedError, match="perception lane"):
        adapter.build_payload(_history_with_file("abc123"), spec, GenParams(), [attachment])


# --------------------------------------------------------------------------- #
# Regeneration
# --------------------------------------------------------------------------- #
def _bless() -> None:
    """Rewrite the golden file from the current implementation."""
    adapter = OpenRouterAdapter(client=httpx.AsyncClient(), base_url=BASE_URL)
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
