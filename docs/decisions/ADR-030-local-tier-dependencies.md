# ADR-030 — The local tier's dependencies: PyMuPDF's licence, Tesseract detected not assumed

**Status:** accepted · Phase 4, Steps 1 and 7 · 2026-08-22
**Implements:** `phase4.md` §3 D30
**Relates to:** ADR-028 (the tier chain tier 3 sits at the bottom of); `docs/limitations.md`
(where the licence and privacy consequences are recorded for a reader who will never open this file)

## Context

Tier 3 needs two things that are not a `pip install` and done. Reading a PDF's embedded text and
rasterizing its pages for OCR both need a real PDF library; OCR itself needs a real OCR engine — and
the development plan names both by name (PyMuPDF, Tesseract) without settling the two questions that
actually determine what ships: what PyMuPDF's licence costs a project like this one, and what happens
on the very large number of machines — most developer laptops among them — that do not have
Tesseract installed.

## Decision

**PDF text layer and rasterization: PyMuPDF**, doing both jobs in one dependency and doing them fast.
It is **AGPL-3.0**, which is worth stating rather than discovering later: this project is not
distributed as a product and its source is already public, so the licence costs nothing *here* — but
a fork that closed its source would have a real problem. `pypdfium2` (BSD) is named as the swap
should that ever matter; the tier is one module, and the swap is one function.

**Image OCR: Tesseract**, shelled out to via `pytesseract`. It is a system binary, not a wheel, which
means `apt-get install tesseract-ocr tesseract-ocr-eng` in the Dockerfile's runtime stage — roughly
100MB of layer, including the English language data — and it means the binary is *absent* on a
developer's machine by default, Windows included, unless installed separately.

**OCR is optional at runtime and detected, never assumed.** `PERCEPTION_LOCAL_OCR_ENABLED` gates it;
`local.ocr_available()` probes the real binary once (`pytesseract.get_tesseract_version()`), caches
the answer for the process's lifetime, and logs `perception.ocr_unavailable` at warning level if it
is missing. With OCR unavailable, tier 3 still reads a PDF's text layer; an image falls straight to
returning `None` — which the lane reads as `FileUnreadable` — rather than to a stack trace.

**One more flag, built for the demo specifically:** `PERCEPTION_LOCAL_ONLY`. On, tier 2 is skipped
entirely and every extraction goes local, so "disable Gemini entirely, the answer still arrives,
degraded and labelled" does not require revoking a live key mid-demo.

## Why

**Runtime detection beats a build-time assumption because the two environments that matter most
disagree about the binary's presence.** The deployed image always carries `tesseract-ocr` — the
Dockerfile installs it unconditionally — but a developer running the test suite locally very often
does not have it, and should not need to install a 100MB system package just to run `make test`.
Detecting once, lazily, and degrading tier 3 rather than crashing it is what lets one test suite pass
in both places without a `skipif` scattered through every test that happens to touch OCR: **the whole
module's tests pass with Tesseract absent** (the image path skips; the PDF text-layer path, which
needs no OCR at all, does not).

**A cached probe rather than a check on every call is a correctness choice, not just a performance
one.** The binary's presence cannot change mid-process — nothing in this deployment reinstalls system
packages while serving traffic — so re-probing on every extraction would just be a wasted subprocess
spawn with no chance of a different answer.

**Stating the AGPL licence plainly, rather than treating "it's just OCR" as license-neutral, is what
this project's "be honest about the edges" principle actually means in practice.** The licence costs
nothing to *this* deployment because nothing here is distributed as a closed-source product — but
pretending the question doesn't exist would be exactly the kind of edge this project otherwise takes
care to document rather than paper over. Naming the BSD-licensed swap (`pypdfium2`) alongside the
fact, rather than after someone asks, is what makes the caveat actionable instead of merely honest.

**`PERCEPTION_LOCAL_ONLY` exists because the single most persuasive demo this phase can produce
should not require an operational risk to run.** "Every free tier is spent and the answer still
arrives, degraded and labelled" is the sentence this whole phase is built to prove true — and proving
it by actually revoking the Gemini key mid-demo is both slower to recover from and a real
availability risk for anyone else using the shared deployment at that moment. A boolean flag lets the
demo be run and re-run without touching anything Gemini can see.

## Consequences

- The Docker image roughly doubles in size once Tesseract and its language data land, which on
  Render's free build tier is a real risk of the build itself timing out — flagged explicitly in the
  Dockerfile's own comment and checked once, deliberately, before the phase's last commit rather than
  discovered on the deploy that carries it.
- `PERCEPTION_LOCAL_OCR_ENABLED=false` and a missing binary produce the *same* degraded behavior —
  a PDF's text layer still reads, an image does not — which means the flag is genuinely a kill switch
  and not merely documentation of a state the binary's absence would produce anyway.
- `docs/limitations.md` carries the licence caveat in one line, for a reader who will reasonably never
  open this ADR — the honest-edges document is where that reader looks first, and duplicating the
  fact there rather than only cross-referencing it is a deliberate redundancy, not an oversight.
- Every synchronous call PyMuPDF or Tesseract makes runs inside `asyncio.to_thread` (trap 4) — a
  decision this ADR assumes rather than re-argues, since blocking the one event loop a free-tier
  instance has for every other request on it is a correctness bug regardless of which library is
  doing the blocking.
