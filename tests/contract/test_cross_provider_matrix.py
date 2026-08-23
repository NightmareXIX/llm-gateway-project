"""The cross-provider golden matrix — §2.2.6, D31 (phase5.md Step 1).

The property no existing test asserts: **one fixed canonical history, driven
through :func:`app.memory.render.render`, produces the correct payload shape
for all three providers — with and without an attachment.**

``tests/unit/test_*_payload.py`` golden-tests ``adapter.build_payload``
directly, which is the right test for what those files protect (a pure
function with no dependencies) but cannot express this claim: a ``file_ref``
has no payload shape until something decides whether it is native or
injected, and that decision is render step 1, not ``build_payload``. So this
suite renders instead, through a scripted resolver
(:class:`tests.provider_fixtures.ScriptedResolver`) that answers tier 1's
question and nothing else — no database, no Redis, no ``PerceptionResolver``.

The three ``*_general`` goldens and ``gemini_attachment`` are **reused
byte-for-byte** from the per-adapter suites, not copied: if ``render()``
disagrees with a committed ``build_payload`` golden on a history that needs no
fitting, that is a real bug in the pipeline's transparency, and this matrix is
what finds it (trap 2 — the fix is to understand the disagreement, never to
re-bless or loosen the assertion). ``groq_attachment`` and
``openrouter_attachment`` are new.

Regenerate deliberately, never reflexively::

    python -m tests.contract.test_cross_provider_matrix --bless
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from app.memory import render
from app.providers.base import ProviderAdapter
from app.providers.gemini import GeminiAdapter
from app.providers.groq import GroqAdapter
from app.providers.openrouter import OpenRouterAdapter
from app.providers.types import ModelSpec
from tests import provider_fixtures as fx

pytestmark = pytest.mark.contract


@dataclass(frozen=True)
class ProviderCase:
    """One provider's slice of the matrix: how to build its adapter and spec,
    and which committed goldens its two renders must match."""

    provider: str
    adapter_class: Callable[..., ProviderAdapter]
    base_url: str
    spec: Callable[[], ModelSpec]
    general_golden: str
    attachment_golden: str
    has_top_level_system_field: bool
    """Gemini's ``system_instruction`` vs. an in-array ``role: "system"`` —
    §4.7's shape difference, asserted structurally rather than only via the
    golden diff (a golden says *that* something changed; this says *what*)."""

    def build(self) -> ProviderAdapter:
        # No live request is ever made — `render()` only calls `build_payload`
        # and `estimate_tokens`, both pure — so an unused client is enough.
        return self.adapter_class(client=httpx.AsyncClient(), base_url=self.base_url)

    def is_native_for_the_attachment(self) -> bool:
        return self.spec().supports_mime("image/png")


CASES: list[ProviderCase] = [
    ProviderCase(
        provider="gemini",
        adapter_class=GeminiAdapter,
        base_url="https://generativelanguage.googleapis.com/v1beta",
        spec=fx.gemini_spec,
        general_golden="gemini_general",
        attachment_golden="gemini_attachment",
        has_top_level_system_field=True,
    ),
    ProviderCase(
        provider="groq",
        adapter_class=GroqAdapter,
        base_url="https://api.groq.com/openai/v1",
        spec=fx.groq_spec,
        general_golden="groq_general",
        attachment_golden="groq_attachment",
        has_top_level_system_field=False,
    ),
    ProviderCase(
        provider="openrouter",
        adapter_class=OpenRouterAdapter,
        base_url="https://openrouter.ai/api/v1",
        spec=fx.openrouter_spec,
        general_golden="openrouter_general",
        attachment_golden="openrouter_attachment",
        has_top_level_system_field=False,
    ),
]

CASES_BY_PROVIDER = {case.provider: case for case in CASES}
PARAMETRIZE = pytest.mark.parametrize("case", CASES, ids=lambda c: c.provider)


def _first_message_text(case: ProviderCase, payload: dict[str, Any]) -> str:
    """The text the first user turn rendered to, whichever field it lives in."""
    if case.provider == "gemini":
        text: str = payload["contents"][0]["parts"][0]["text"]
        return text
    content: str = payload["messages"][1]["content"]
    return content


def _last_message_text(case: ProviderCase, payload: dict[str, Any]) -> str:
    """The text the final (attached-to) turn rendered to."""
    if case.provider == "gemini":
        text: str = payload["contents"][-1]["parts"][0]["text"]
        return text
    content: str = payload["messages"][-1]["content"]
    return content


def _extract_document_block(text: str) -> str:
    match = re.search(r"<document.*?</document>", text, re.DOTALL)
    assert match is not None, f"no <document> envelope in: {text!r}"
    return match.group(0)


# --------------------------------------------------------------------------- #
# The six goldens, through render() rather than build_payload
# --------------------------------------------------------------------------- #
@PARAMETRIZE
async def test_the_fixed_history_renders_to_the_committed_general_golden(
    case: ProviderCase,
) -> None:
    payload, report = await render.render(
        fx.canonical_history(), case.spec(), fx.general_params(), case.build()
    )

    assert payload == fx.read_golden(case.general_golden)
    assert report.attachments_native == 0
    assert report.attachments_injected == 0


@PARAMETRIZE
async def test_the_fixed_history_with_an_attachment_renders_to_the_committed_golden(
    case: ProviderCase,
) -> None:
    payload, report = await render.render(
        fx.canonical_history_with_attachment(),
        case.spec(),
        fx.general_params(),
        case.build(),
        resolver=fx.ScriptedResolver(),
    )

    assert payload == fx.read_golden(case.attachment_golden)
    if case.is_native_for_the_attachment():
        assert report.attachments_native == 1
        assert report.attachments_injected == 0
    else:
        assert report.attachments_native == 0
        assert report.attachments_injected == 1


# --------------------------------------------------------------------------- #
# The point of the matrix, not incidental to it (Step 1 item 3)
# --------------------------------------------------------------------------- #
@PARAMETRIZE
async def test_the_system_message_lands_in_the_right_place(case: ProviderCase) -> None:
    payload, _ = await render.render(
        fx.canonical_history(), case.spec(), fx.general_params(), case.build()
    )

    if case.has_top_level_system_field:
        assert "system_instruction" in payload
        assert all(entry["role"] != "system" for entry in payload["contents"])
    else:
        assert "system_instruction" not in payload
        assert payload["messages"][0]["role"] == "system"


@PARAMETRIZE
async def test_the_omission_marker_survives_into_every_providers_payload_text(
    case: ProviderCase,
) -> None:
    """D4's scar has no provider-native representation (phase5.md §2 table). It
    must be rendered into prose identically three times."""
    payload, _ = await render.render(
        fx.canonical_history(), case.spec(), fx.general_params(), case.build()
    )

    assert "[4 earlier messages omitted]" in _first_message_text(case, payload)


async def test_the_document_envelope_is_byte_identical_across_injected_providers() -> None:
    """§2.2.5 step 2's promise, checked across providers for the first time.

    Gemini attaches the fixture image natively and carries no envelope at all —
    excluded here, not compared against.
    """
    envelopes: dict[str, str] = {}
    for case in CASES:
        if case.is_native_for_the_attachment():
            continue
        payload, _ = await render.render(
            fx.canonical_history_with_attachment(),
            case.spec(),
            fx.general_params(),
            case.build(),
            resolver=fx.ScriptedResolver(),
        )
        envelopes[case.provider] = _extract_document_block(_last_message_text(case, payload))

    assert len(envelopes) == 2
    groq_envelope, openrouter_envelope = envelopes["groq"], envelopes["openrouter"]
    assert groq_envelope == openrouter_envelope
    assert fx.SCRIPTED_EXTRACTION_TEXT in groq_envelope


async def test_a_native_attachment_costs_measurably_more_than_no_attachment() -> None:
    """D27's tile cost, invisible to a character-based estimator (trap 9) and so
    the one thing a character-count comparison of the two Gemini goldens could
    not itself prove."""
    case = CASES_BY_PROVIDER["gemini"]
    spec, params = case.spec(), fx.general_params()

    _, general_report = await render.render(fx.canonical_history(), spec, params, case.build())
    _, attachment_report = await render.render(
        fx.canonical_history_with_attachment(),
        spec,
        params,
        case.build(),
        resolver=fx.ScriptedResolver(),
    )

    assert attachment_report.estimated_tokens > general_report.estimated_tokens


# --------------------------------------------------------------------------- #
# Regeneration
# --------------------------------------------------------------------------- #
def _bless() -> None:
    """Rewrite all six golden files from the current implementation."""

    async def _render_all() -> dict[str, dict[str, Any]]:
        payloads: dict[str, dict[str, Any]] = {}
        for case in CASES:
            general_payload, _ = await render.render(
                fx.canonical_history(), case.spec(), fx.general_params(), case.build()
            )
            attachment_payload, _ = await render.render(
                fx.canonical_history_with_attachment(),
                case.spec(),
                fx.general_params(),
                case.build(),
                resolver=fx.ScriptedResolver(),
            )
            payloads[case.general_golden] = general_payload
            payloads[case.attachment_golden] = attachment_payload
        return payloads

    for name, payload in asyncio.run(_render_all()).items():
        fx.write_golden(name, payload)


if __name__ == "__main__":  # pragma: no cover
    if "--bless" in sys.argv:
        _bless()
    else:
        print(__doc__)
