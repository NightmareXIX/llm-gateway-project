# ADR-029 — What a natively attached file costs, in tokens

**Status:** accepted · Phase 4, Step 9 · 2026-08-22
**Implements:** `phase4.md` §3 D27
**Relates to:** ADR-027 (why tier 1 must make *no* separate reservation, which is only safe because
of the number this ADR defines); ADR-028 (the tier chain `token_cost` is attached inside)

## Context

`memory/render.py` had named this gap in a comment since Phase 2: "Native bytes are not prompt text,
and base64 length is not a token count." Until Phase 4 Step 9, that gap was invisible because nothing
ever attached a file natively. Once tier 1 exists, it stops being theoretical: a natively attached
40-page PDF measured as the 30-character placeholder string `materialize` produces
(`[application/pdf attachment: q3.pdf]`), so the fitting step believed the document was free and the
quota reservation under-counted it by four orders of magnitude.

## Decision

**`ResolvedAttachment` gains `token_cost: int` and `pages: int | None`, computed by the lane at
resolve time from a declared per-modality rate table, and consumed in three places:**

- `fitting.py` adds the `token_cost` of every native attachment still referenced by a surviving
  message to the measured total. An `injected` attachment's cost is not added again — it is already
  inside the text `materialize` produced (trap 8).
- `render.py::materialize` keeps returning its short placeholder string unchanged — that string is a
  projection for measuring *text*, and the token cost travels beside it rather than inside it.
- `RenderReport.estimated_tokens` is `adapter.estimate_tokens(payload)` **plus** the native
  attachments' summed cost, because an estimator that counts characters in a base64 blob is worse than
  one that ignores base64 entirely (trap 9).

**The rate table lives in `perception/lane.py`**, dated to when it was read (2026-08-22):
`TOKENS_PER_TILE = 258`, Google's published per-tile rate, with a PDF page billed as one image and
Google's own tiling geometry (`IMAGE_SINGLE_TILE_EDGE`, `TILE_EDGE_MIN`/`MAX`, `TILE_EDGE_DIVISOR`)
governing how a larger image splits into tiles. `pages` is measured by `perception/local.py::measure`
(PyMuPDF for a PDF's page count, Pillow for an image's pixel dimensions) — off the event loop like
every other PyMuPDF/Pillow call in that module — because a page count has to be known *before* the
payload is built, not derived from it afterward.

**Never zero, on any failure path.** A PDF whose page count could not be read falls back to
`UNMEASURED_PDF_BYTES_PER_PAGE`-based guessing from the file's size; an image whose dimensions could
not be read falls back to `UNMEASURED_IMAGE_TILES = 4`. Both are deliberately coarse and deliberately
non-zero.

## Why

**Base64 length cannot be the input to the estimate, because the failure mode is catastrophic rather
than merely imprecise.** A 6MB PDF is roughly 8 million base64 characters; a characters-over-four
estimator turns that into a two-million-token reservation that fails closed against every candidate's
real context window (trap 9). The correct fix is not a better character-based heuristic — it is
refusing to let `estimate_tokens` see base64 at all, and computing the cost from the file's own
geometry instead.

**The rate table belongs beside the lane, not in `config/limits.yaml`, for the same reason
`limits.yaml`'s own header gives in the other direction.** `limits.yaml` holds numbers an *operator*
tunes per environment — a provider's published RPM, a headroom fraction. Google's per-tile billing
rate is not something an operator chooses; it is a fact about a provider's own pricing that changes on
Google's schedule, not the gateway's. What the two have in common, and the actual design principle, is
that a free tier's arithmetic changes and a number that lives in exactly one place — dated, so staleness
is visible — is a number that can be corrected without a hunt.

**A page count has to be known before the payload exists, which is why it travels on
`ResolvedAttachment` rather than being derived from `estimate_tokens` after the fact.** Charging per
page requires knowing the page count, and by the time `adapter.build_payload` has produced a payload,
the boundary between "this is a document" and "this is a base64 string" is already gone. Measuring
happens once, in the lane, before `build_payload` is ever called — the same place `token_cost` itself
is computed.

**Guessing high on a measurement failure costs less than guessing free.** A number that never reaches
zero preserves D27's whole reason for existing: the one failure this decision was written to prevent
is an attachment that measures as free, which is exactly the state the placeholder-string bug was in
before this step. Guessing conservatively costs a slightly early failover on an unmeasurable file;
guessing zero costs a 429 the reservation should have prevented in the first place.

**Charging cost per message, not per turn, keeps the accounting attached to what actually carries
it.** `fitting._cost` adds `native_tokens()` for the `file_refs` on *that specific message* — so a
document leaves the budget together with the message that referenced it, rather than being charged
against the turn as a whole and surviving a truncation that dropped the message carrying it.

## Consequences

- `tests/fixtures/golden_payloads/gemini_attachment.json` pins the fixed canonical history plus one
  native attachment — a committed 200-byte, 96×96 PNG (`tests/fixtures/files/tile.png`), sized so it
  is exactly one tile at the published rate and the test's arithmetic is checkable by hand.
- `extractors._estimated_tokens` deliberately keeps its own coarser size-based heuristic rather than
  importing this rate table — `lane.py` imports `extractors`, so reaching back for the table would
  close an import loop, for a number that gets reconciled against the provider's own reported usage
  seconds later at commit time anyway.
- The gap this ADR closes was real and measured, not hypothetical: a live 40-page PDF that used to
  reserve on the order of tens of tokens now reserves 10,320 (the rate table's estimate) against
  Gemini's own reported 21,297 prompt tokens for that same turn — five figures either way, the same
  order of magnitude, against the two-figure placeholder that preceded this step.
- Any future modality this gateway attaches natively (audio, video — explicitly out of scope for
  Phase 4) needs its own entry in this table before native passthrough for it can be turned on; there
  is no generic fallback that would be honest for a modality Google prices differently.
