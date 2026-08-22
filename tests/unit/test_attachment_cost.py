"""D27 — what a natively attached file costs, and how that number is arrived at.

Two halves, both of which have to hold for the third consumer (the quota
reservation) to mean anything:

**The measurement** — :func:`app.perception.local.measure` — reads a page count
off a real PDF and pixel dimensions off a real image, and returns an empty
:class:`~app.perception.local.Measurement` rather than raising on anything it
cannot open. Nothing here needs Tesseract; measuring is not reading.

**The rate table** — :func:`app.perception.lane.native_token_cost` — turns that
into tokens at Google's published per-tile rate, with a documented fallback for
each thing the measurement can fail to learn. The one number that must never
appear is zero: an attachment that measures as free is precisely the failure
D27 exists to end, and the whole reason the payload's own length is not allowed
anywhere near this arithmetic (trap 9).

The 40-page case is Step 9's own done-criterion, written as a test: a native
40-page PDF has to reserve a five-figure token count rather than the two-figure
one ``materialize``'s thirty-character placeholder used to produce.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from app.memory.canonical import file_ref_block
from app.perception.lane import (
    TOKENS_PER_TILE,
    UNMEASURED_IMAGE_TILES,
    _native_wanted,
    native_token_cost,
)
from app.perception.local import Measurement, measure
from app.providers.types import ModelSpec
from tests import provider_fixtures as fx

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "files"
TEXT_LAYER_PDF = (FIXTURE_DIR / "text_layer.pdf").read_bytes()
CHART_PNG = (FIXTURE_DIR / "chart.png").read_bytes()


def _pdf_of(pages: int) -> bytes:
    """A synthetic PDF of a given length — the measurement's only real input."""
    doc = pymupdf.open()
    for index in range(pages):
        doc.new_page().insert_text((72, 72), f"page {index + 1}", fontsize=11)
    data: bytes = doc.tobytes()
    doc.close()
    return data


def _spec(*, max_file_bytes: int | None) -> ModelSpec:
    return ModelSpec(
        slot="general",
        provider="gemini",
        model="gemini-3.6-flash",
        context_window=1048576,
        max_output_tokens=65536,
        supports_streaming=True,
        supports_vision=True,
        supports_pdf=True,
        supports_system_field=True,
        max_file_bytes=max_file_bytes,
        priority=0,
    )


# --------------------------------------------------------------------------- #
# The measurement
# --------------------------------------------------------------------------- #
async def test_a_pdf_measures_as_its_page_count() -> None:
    assert (await measure(mime="application/pdf", data=TEXT_LAYER_PDF)).pages == 2


async def test_an_image_measures_as_its_pixel_dimensions() -> None:
    measured = await measure(mime="image/png", data=CHART_PNG)

    assert (measured.width, measured.height) == (300, 120)
    assert measured.pages is None


async def test_a_file_that_cannot_be_opened_measures_as_nothing_rather_than_raising() -> None:
    """The caller's fallback is a coarser estimate, not a failed turn — and
    tier 1 is about to hand these same bytes to a model that will form its own
    opinion about whether they parse."""
    assert await measure(mime="application/pdf", data=b"not a pdf") == Measurement()


async def test_an_unknown_mime_measures_as_nothing() -> None:
    assert await measure(mime="text/plain", data=b"hello") == Measurement()


# --------------------------------------------------------------------------- #
# The rate table
# --------------------------------------------------------------------------- #
async def test_a_forty_page_pdf_costs_a_five_figure_token_count() -> None:
    """Step 9's done criterion. The placeholder this replaces measured the same
    document at about eight tokens."""
    data = _pdf_of(40)
    measured = await measure(mime="application/pdf", data=data)

    cost = native_token_cost(mime="application/pdf", size_bytes=len(data), measurement=measured)

    assert measured.pages == 40
    assert cost == 40 * TOKENS_PER_TILE
    assert 10_000 <= cost < 100_000


def test_a_small_image_is_one_tile() -> None:
    """96x96 is under Gemini's 384px threshold, which is what makes the
    attachment golden's arithmetic checkable by hand."""
    cost = native_token_cost(
        mime="image/png",
        size_bytes=len(fx.attachment_bytes()),
        measurement=Measurement(width=96, height=96),
    )

    assert cost == TOKENS_PER_TILE


def test_a_large_image_is_charged_per_tile() -> None:
    small = native_token_cost(
        mime="image/png", size_bytes=1, measurement=Measurement(width=384, height=384)
    )
    large = native_token_cost(
        mime="image/png", size_bytes=1, measurement=Measurement(width=2048, height=1536)
    )

    assert small == TOKENS_PER_TILE
    assert large > small
    assert large % TOKENS_PER_TILE == 0


@pytest.mark.parametrize(
    ("mime", "measurement"),
    [
        ("application/pdf", Measurement()),
        ("image/png", Measurement()),
        ("image/png", Measurement(width=0, height=0)),
    ],
)
def test_an_unmeasured_file_is_never_free(mime: str, measurement: Measurement) -> None:
    """The fallbacks. Guessing high costs a slightly early failover; guessing
    zero tells the reservation a request costs nothing."""
    assert native_token_cost(mime=mime, size_bytes=6_000_000, measurement=measurement) > 0


def test_an_unmeasured_pdf_is_charged_from_its_size() -> None:
    tiny = native_token_cost(mime="application/pdf", size_bytes=1, measurement=Measurement())
    big = native_token_cost(mime="application/pdf", size_bytes=6_000_000, measurement=Measurement())

    assert tiny == TOKENS_PER_TILE
    assert big > tiny


def test_an_unmeasured_image_is_charged_at_the_documented_tile_guess() -> None:
    cost = native_token_cost(mime="image/png", size_bytes=900_000, measurement=Measurement())

    assert cost == UNMEASURED_IMAGE_TILES * TOKENS_PER_TILE


def test_the_cost_never_reads_the_bytes() -> None:
    """The whole of trap 9 in one assertion: two files of wildly different byte
    lengths and the same page count cost the same."""
    thin = native_token_cost(
        mime="application/pdf", size_bytes=1_000, measurement=Measurement(pages=3)
    )
    fat = native_token_cost(
        mime="application/pdf", size_bytes=6_000_000, measurement=Measurement(pages=3)
    )

    assert thin == fat == 3 * TOKENS_PER_TILE


# --------------------------------------------------------------------------- #
# Tier 1's gate — a file too big for the model never becomes a native cost
# --------------------------------------------------------------------------- #
def test_a_file_over_the_models_cap_never_reaches_tier_one() -> None:
    ref = file_ref_block(
        file_hash="a" * 64, filename="q3.pdf", mime="application/pdf", bytes=30_000_000
    )

    assert _native_wanted(ref, _spec(max_file_bytes=20_000_000)) is False
    assert _native_wanted(ref, _spec(max_file_bytes=None)) is True


def test_a_mime_the_model_cannot_read_never_reaches_tier_one() -> None:
    ref = file_ref_block(file_hash="a" * 64, filename="q3.pdf", mime="application/pdf", bytes=1_000)
    text_only = ModelSpec(
        slot="fast",
        provider="groq",
        model="openai/gpt-oss-120b",
        context_window=131072,
        max_output_tokens=32768,
        supports_streaming=True,
        supports_vision=False,
        supports_pdf=False,
        supports_system_field=False,
        max_file_bytes=None,
        priority=0,
    )

    assert _native_wanted(ref, text_only) is False
