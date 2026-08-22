"""Tier 3 — the last resort, exercised against the three committed fixtures.

Every OCR-dependent assertion monkeypatches the module's own
``_probe_tesseract``/``pytesseract.image_to_string`` seams rather than relying
on a real ``tesseract`` binary being on the machine running the suite. Two
reasons, not one:

**The done criterion is explicit.** Step 7 asks for "the whole suite passes
with Tesseract absent from the machine (the image path skips, the PDF text
path does not)" — this dev machine genuinely has no ``tesseract`` on its
``PATH``, and mocking the seam is what lets the OCR-success tests run (and
mean something) instead of being skipped outright.

**The two things worth testing are independent of the binary.** Whether tier
3 chooses the text-layer path over OCR, grades a reading ``medium`` versus
``low``, caps a long scan at ``PERCEPTION_OCR_MAX_PAGES`` and says so, and
degrades to ``None`` rather than raising — none of that is a claim about what
Tesseract's own OCR engine produces. A real end-to-end OCR run belongs in
``scripts/record_fixtures.py``-style manual verification, not the unit suite.

The three fixtures under ``tests/fixtures/files/`` are generated, not
hand-crafted — regenerate them with::

    uv run python - <<'PY'
    import io
    from pathlib import Path
    import pymupdf
    from PIL import Image, ImageDraw

    out = Path("tests/fixtures/files")

    doc = pymupdf.open()
    doc.new_page().insert_text(
        (72, 72),
        "Quarterly Report\n\n"
        "Revenue rose 12 percent year over year, driven by strong demand in the\n"
        "enterprise segment. Operating margin improved to 18 percent.",
        fontsize=11,
    )
    doc.new_page().insert_text(
        (72, 72),
        "Appendix A: Methodology\n\n"
        "Figures are unaudited and reflect management estimates as of the\n"
        "quarter's close.",
        fontsize=11,
    )
    doc.save(str(out / "text_layer.pdf"))

    scan = Image.new("L", (300, 100), 255)
    ImageDraw.Draw(scan).text((10, 40), "SCANNED INVOICE TOTAL 4210", fill=0)
    buf = io.BytesIO()
    scan.save(buf, format="PNG", optimize=True)
    doc2 = pymupdf.open()
    page = doc2.new_page()
    page.insert_image(page.rect, stream=buf.getvalue())
    doc2.save(str(out / "scanned.pdf"))

    chart = Image.new("L", (300, 120), 255)
    ImageDraw.Draw(chart).text((10, 10), "REVENUE CHART\nQ1 100  Q2 120  Q3 150", fill=0)
    chart.save(str(out / "chart.png"), optimize=True)
    PY
"""

from __future__ import annotations

from pathlib import Path

import pytesseract
import pytest

from app.perception import local

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "files"
FILE_HASH = "b" * 64


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# --------------------------------------------------------------------------- #
# The text-layer PDF: no OCR involved at all
# --------------------------------------------------------------------------- #
async def test_a_text_layer_pdf_extracts_without_touching_ocr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cheap path. If this reaches OCR at all, the threshold is miscalibrated."""

    def _must_not_be_called() -> bool:
        raise AssertionError("a text-layer PDF must not probe for Tesseract")

    monkeypatch.setattr(local, "ocr_available", _must_not_be_called)

    result = await local.extract_locally(
        file_hash=FILE_HASH, mime="application/pdf", data=_read("text_layer.pdf")
    )

    assert result is not None
    assert result.tier == "local"
    assert result.provider == "local"
    assert result.model == "local"
    assert result.confidence == "medium"
    assert result.pages == 2
    assert "Revenue rose 12 percent" in result.text
    assert "Appendix A: Methodology" in result.text
    # D28's envelope, even from the local tier.
    assert "## Summary" in result.text
    assert "## Verbatim text" in result.text


async def test_the_text_layer_summary_section_says_it_is_unavailable() -> None:
    """The envelope's shape does not depend on the tier (Step 7's plain-terms
    note): a summary is genuinely not available from the local tier, so the
    section says so rather than being omitted."""
    result = await local.extract_locally(
        file_hash=FILE_HASH, mime="application/pdf", data=_read("text_layer.pdf")
    )

    assert result is not None
    summary = result.text.split("## Structure")[0]
    assert "Not available" in summary


# --------------------------------------------------------------------------- #
# The scanned PDF: OCR, mocked for a deterministic result
# --------------------------------------------------------------------------- #
async def test_a_scanned_pdf_triggers_ocr_and_grades_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(local, "ocr_available", lambda: True)
    monkeypatch.setattr(pytesseract, "image_to_string", lambda _image: "SCANNED INVOICE TOTAL 4210")

    result = await local.extract_locally(
        file_hash=FILE_HASH, mime="application/pdf", data=_read("scanned.pdf")
    )

    assert result is not None
    assert result.confidence == "low"
    assert result.pages == 1
    assert "SCANNED INVOICE TOTAL 4210" in result.text


async def test_a_scanned_pdf_with_ocr_unavailable_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D30: with OCR unavailable, a PDF with a text layer still reads (the test
    above); a PDF that *needs* OCR to read anything produces nothing, which the
    lane turns into ``FileUnreadable`` rather than a stack trace."""
    monkeypatch.setattr(local, "ocr_available", lambda: False)

    result = await local.extract_locally(
        file_hash=FILE_HASH, mime="application/pdf", data=_read("scanned.pdf")
    )

    assert result is None


async def test_a_long_scan_ocrs_only_the_page_cap_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trap 15: an unbounded OCR of a long scan is minutes of CPU on a request
    that has to return. Built in-memory rather than committed, since it exists
    only to prove the cap — the three fixture files are the ones worth keeping
    around."""
    import pymupdf

    doc = pymupdf.open()
    for _ in range(40):
        doc.new_page()
    long_scan = doc.tobytes()
    doc.close()

    monkeypatch.setattr(local, "ocr_available", lambda: True)
    calls = 0

    def fake_ocr(_image: object) -> str:
        nonlocal calls
        calls += 1
        return f"page text {calls}"

    monkeypatch.setattr(pytesseract, "image_to_string", fake_ocr)

    result = await local.extract_locally(
        file_hash=FILE_HASH, mime="application/pdf", data=long_scan, max_ocr_pages=10
    )

    assert result is not None
    assert calls == 10
    assert result.pages == 40
    assert "first 10 of 40 pages" in result.text


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #
async def test_the_chart_png_ocrs_directly_and_grades_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(local, "ocr_available", lambda: True)
    monkeypatch.setattr(
        pytesseract, "image_to_string", lambda _image: "REVENUE CHART Q1 100 Q2 120"
    )

    result = await local.extract_locally(
        file_hash=FILE_HASH, mime="image/png", data=_read("chart.png")
    )

    assert result is not None
    assert result.confidence == "low"
    assert result.pages is None
    assert "REVENUE CHART" in result.text


async def test_an_image_with_ocr_unavailable_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one case D30 names explicitly: no text layer to fall back on, so a
    missing binary means nothing at all rather than a stack trace."""
    monkeypatch.setattr(local, "ocr_available", lambda: False)

    result = await local.extract_locally(
        file_hash=FILE_HASH, mime="image/png", data=_read("chart.png")
    )

    assert result is None


async def test_an_image_where_ocr_recovers_nothing_meaningful_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D25: tier 3 "may still produce nothing." A blank scrap of whitespace is
    not a reading, even though the OCR engine technically ran and returned 200."""
    monkeypatch.setattr(local, "ocr_available", lambda: True)
    monkeypatch.setattr(pytesseract, "image_to_string", lambda _image: "   \n\n  ")

    result = await local.extract_locally(
        file_hash=FILE_HASH, mime="image/png", data=_read("chart.png")
    )

    assert result is None


# --------------------------------------------------------------------------- #
# Real Tesseract, when this machine happens to have one
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not local.ocr_available(), reason="tesseract is not installed")
async def test_the_chart_png_ocrs_for_real_when_tesseract_is_present() -> None:
    """The one test in this module that is allowed to touch the real binary —
    guarded so its absence (this dev machine, most CI images) skips it instead
    of failing the suite, per Step 7's own done criterion."""
    result = await local.extract_locally(
        file_hash=FILE_HASH, mime="image/png", data=_read("chart.png")
    )

    assert result is not None
    assert result.confidence == "low"
    # Real OCR against a plain sans-serif render; a substring is as much as is
    # safe to assert without pinning a specific Tesseract version's output.
    assert "REVENUE" in result.text.upper()


# --------------------------------------------------------------------------- #
# OCR probing itself
# --------------------------------------------------------------------------- #
def test_ocr_available_is_false_when_the_binary_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local._probe_tesseract.cache_clear()

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(pytesseract, "get_tesseract_version", _raise)

    assert local.ocr_available() is False
    local._probe_tesseract.cache_clear()


# --------------------------------------------------------------------------- #
# An unsupported mime is a programming error, not a degraded reading
# --------------------------------------------------------------------------- #
async def test_a_mime_outside_the_allowlist_is_a_programming_error() -> None:
    """Step 3's upload allowlist closes this off before it can reach tier 3; a
    format arriving here anyway is a bug upstream, not a file to degrade on."""
    with pytest.raises(ValueError, match="no path for mime"):
        await local.extract_locally(file_hash=FILE_HASH, mime="audio/mpeg", data=b"junk")
