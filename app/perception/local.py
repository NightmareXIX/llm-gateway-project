"""Tier 3 — the last resort, and the one that keeps the feature alive when
every free tier is spent.

Reads a PDF's embedded text layer if there is one; rasterizes the pages and
runs Tesseract over them if there is not; OCRs an image directly. Never calls
a provider, never spends quota, and is the only tier the perception lane can
still reach with :data:`~app.config.Settings.PERCEPTION_LOCAL_ONLY` set or
every candidate in the ``perception`` slot exhausted.

**It is worse than tier 2, and it says so.** A PDF with a text layer grades
``medium`` — real text, no model read it. Anything that needed OCR grades
``low``, which is tier 3's marker alone (see ``extractors.grade``'s docstring)
and the thing that sets ``RenderReport.degraded`` three layers up.

**Everything here runs off the event loop.** PyMuPDF and Tesseract are
synchronous and CPU-bound; called directly from a coroutine they block the one
loop the process has for every other request on it (trap 4). The public
entry point, :func:`extract_locally`, is the only ``async def`` in the module
and its whole body is one ``asyncio.to_thread`` call.

**OCR is optional, not assumed.** Tesseract is a system binary, not a wheel
(D30), and is routinely absent on a developer's machine. :func:`ocr_available`
probes for it once, lazily, and a missing binary degrades the local tier
rather than crashing it: a PDF with a text layer still returns a reading, and
a PDF without one or a bare image returns ``None`` — which the lane reads as
``FileUnreadable``, the same "may still produce nothing" tier 3 has always
been allowed (D25).

**The envelope's shape does not depend on the tier.** ``extractors.py``'s four
D28 sections — summary, structure, figures, verbatim — are reused verbatim
here (:func:`_envelope`), because nothing downstream should have to know which
tier produced a reading to make sense of it. A summary genuinely is not
available from a page count and an OCR dump, so that section says so rather
than being omitted.
"""

from __future__ import annotations

import asyncio
import io
from functools import lru_cache
from typing import Final

import pymupdf
import pytesseract
from PIL import Image

from app.core.logging import get_logger
from app.perception.extractors import (
    SECTION_FIGURES,
    SECTION_STRUCTURE,
    SECTION_SUMMARY,
    SECTION_VERBATIM,
    Extraction,
)
from app.providers.types import ExtractionConfidence

logger = get_logger("app.perception")

LOCAL_PROVIDER: Final = "local"
LOCAL_MODEL: Final = "local"
"""What :class:`~app.perception.extractors.Extraction` carries for a tier-3
row instead of ``None`` — see that class's docstring for why a nullable
column is not the alternative."""

TEXT_LAYER_MIN_CHARS_PER_PAGE: Final = 40
"""Below this average, a PDF's embedded text is treated as noise — a stray
form field or a watermark, not a text layer — and the page is rasterized and
OCR'd instead. Deliberately low: a real text layer clears it by a wide
margin, and the cost of guessing wrong in this direction is a page that gets
OCR'd for no reason, not a page that gets skipped."""

RASTER_DPI: Final = 150
"""D30's rasterization resolution — enough for Tesseract to read ordinary
body text, and not so much that a ten-page cap turns into a memory problem."""

MIN_OCR_CHARS: Final = 10
"""Below this, OCR output is "nothing meaningful" (D25) rather than a thin
reading — the same distinction ``extractors.grade`` draws with
``MIN_VERBATIM_CHARS``, at a much lower bar because tier 3 has no model
grading its own confidence."""


# --------------------------------------------------------------------------- #
# OCR availability (D30)
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _probe_tesseract() -> bool:
    """Ask the binary itself, once. Cached: the answer cannot change mid-process."""
    try:
        pytesseract.get_tesseract_version()
    except Exception as exc:
        logger.warning("perception.ocr_unavailable", error=str(exc))
        return False
    return True


def ocr_available() -> bool:
    """Whether the Tesseract binary was found on this machine (D30).

    A thin public wrapper over the cached probe, so a caller — this module's
    own tier-3 functions, a future startup log line, a test — never has to
    know the probe is cached rather than free.
    """
    return _probe_tesseract()


# --------------------------------------------------------------------------- #
# The envelope (D28, shared with tier 2)
# --------------------------------------------------------------------------- #
def _envelope(*, summary: str, structure: str, figures: str, verbatim: str) -> str:
    """D28's four sections, formatted exactly as tier 2's model reply is.

    Nothing downstream should have to know which tier produced a reading —
    ``fitting.py`` truncates from the tail either way, and a summary section
    that says "not available" degrades exactly like a summary that says
    little, rather than needing a special case for a missing heading.
    """
    return (
        f"## {SECTION_SUMMARY}\n{summary}\n\n"
        f"## {SECTION_STRUCTURE}\n{structure}\n\n"
        f"## {SECTION_FIGURES}\n{figures}\n\n"
        f"## {SECTION_VERBATIM}\n{verbatim}"
    )


def _is_meaningful(text: str) -> bool:
    """ "Nothing legible" versus a thin-but-real reading (D25's tier-3 failure rule)."""
    stripped = text.strip()
    return len(stripped) >= MIN_OCR_CHARS and any(character.isalnum() for character in stripped)


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
def _extract_pdf(
    data: bytes, *, ocr_enabled: bool, max_ocr_pages: int
) -> tuple[str, ExtractionConfidence, int] | None:
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        logger.warning("perception.local_pdf_unopenable", error=str(exc))
        return None

    try:
        page_count = doc.page_count
        if page_count == 0:
            return None

        page_texts = [page.get_text() for page in doc]
        text_chars = sum(len(text.strip()) for text in page_texts)

        if text_chars / page_count >= TEXT_LAYER_MIN_CHARS_PER_PAGE:
            verbatim = "\n\n".join(text.strip() for text in page_texts if text.strip())
            body = _envelope(
                summary="Not available — this reading came from the local tier, which does "
                "not summarize; only the document's own text layer.",
                structure=f"PDF document, {page_count} page(s), read from its embedded text.",
                figures="Not analyzed by the local tier.",
                verbatim=verbatim,
            )
            return body, "medium", page_count

        # No usable text layer. Fall to rasterize-and-OCR, if OCR is even here.
        if not ocr_enabled or not ocr_available():
            logger.info(
                "perception.local_pdf_needs_ocr_but_unavailable",
                page_count=page_count,
                ocr_enabled=ocr_enabled,
            )
            return None

        pages_to_ocr = min(page_count, max_ocr_pages)
        ocr_chunks: list[str] = []
        for index in range(pages_to_ocr):
            pixmap = doc[index].get_pixmap(dpi=RASTER_DPI)
            image = Image.open(io.BytesIO(pixmap.tobytes("png")))
            ocr_chunks.append(pytesseract.image_to_string(image))

        ocr_text = "\n\n".join(chunk.strip() for chunk in ocr_chunks if chunk.strip())
        if not _is_meaningful(ocr_text):
            return None

        verbatim = ocr_text
        if page_count > pages_to_ocr:
            # Trap 15: an unbounded OCR of a long scan is minutes of CPU on a
            # request that has to return. Capped, and said so in the text.
            verbatim += (
                f"\n\n[Only the first {pages_to_ocr} of {page_count} pages were processed by "
                "OCR; the remaining pages were not read.]"
            )

        body = _envelope(
            summary="Not available — this document had no usable text layer and was read by "
            "local OCR.",
            structure=f"PDF document, {page_count} page(s), scanned (no embedded text layer).",
            figures="Not analyzed by the local tier.",
            verbatim=verbatim,
        )
        return body, "low", page_count
    finally:
        doc.close()


# --------------------------------------------------------------------------- #
# Image
# --------------------------------------------------------------------------- #
def _extract_image(data: bytes, *, ocr_enabled: bool) -> tuple[str, ExtractionConfidence] | None:
    if not ocr_enabled or not ocr_available():
        return None

    try:
        image = Image.open(io.BytesIO(data))
        text = pytesseract.image_to_string(image)
    except Exception as exc:
        logger.warning("perception.local_image_unreadable", error=str(exc))
        return None

    if not _is_meaningful(text):
        return None

    body = _envelope(
        summary="Not available — this image was read by local OCR.",
        structure="Image.",
        figures="Not analyzed by the local tier.",
        verbatim=text.strip(),
    )
    return body, "low"


# --------------------------------------------------------------------------- #
# Dispatch, and the async boundary (trap 4)
# --------------------------------------------------------------------------- #
def _extract_sync(
    *, mime: str, data: bytes, ocr_enabled: bool, max_ocr_pages: int
) -> tuple[str, ExtractionConfidence, int | None] | None:
    if mime == "application/pdf":
        return _extract_pdf(data, ocr_enabled=ocr_enabled, max_ocr_pages=max_ocr_pages)

    if mime.startswith("image/"):
        result = _extract_image(data, ocr_enabled=ocr_enabled)
        if result is None:
            return None
        text, confidence = result
        return text, confidence, None

    # The upload allowlist (Step 3) closes this off before it can happen; a
    # format reaching here is a programming error, not a file to degrade on.
    raise ValueError(f"local extraction has no path for mime {mime!r}")


async def extract_locally(
    *,
    file_hash: str,
    mime: str,
    data: bytes,
    ocr_enabled: bool = True,
    max_ocr_pages: int = 10,
) -> Extraction | None:
    """Tier 3 in full. ``None`` means the lane's last resort produced nothing.

    Every synchronous, CPU-bound step — opening the PDF, rasterizing pages,
    calling Tesseract — happens inside :func:`asyncio.to_thread` (trap 4), so
    this coroutine's only job on the event loop is to await that thread and
    shape what it returns into the same :class:`~app.perception.extractors.Extraction`
    tier 0 and tier 2 already produce, with ``provider``/``model`` set to
    :data:`LOCAL_PROVIDER`/:data:`LOCAL_MODEL` rather than left ``None``.
    """
    result = await asyncio.to_thread(
        _extract_sync, mime=mime, data=data, ocr_enabled=ocr_enabled, max_ocr_pages=max_ocr_pages
    )
    if result is None:
        logger.info("perception.local_extraction_failed", file_hash=file_hash, mime=mime)
        return None

    text, confidence, pages = result
    logger.info(
        "perception.local_extracted",
        file_hash=file_hash,
        mime=mime,
        confidence=confidence,
        pages=pages,
        chars=len(text),
    )
    return Extraction(
        file_hash=file_hash,
        text=text,
        confidence=confidence,
        tier="local",
        provider=LOCAL_PROVIDER,
        model=LOCAL_MODEL,
        pages=pages,
    )


__all__ = [
    "LOCAL_MODEL",
    "LOCAL_PROVIDER",
    "extract_locally",
    "ocr_available",
]
