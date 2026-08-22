# ADR-028 — The four-tier chain, and why only the last tier's failure surfaces

**Status:** accepted · Phase 4, Step 8 · 2026-08-22
**Implements:** `phase4.md` §3 D25 (chain order and failure cost) and D28 (the extraction envelope's
section order)
**Relates to:** ADR-025 (why the chain runs at render time at all); ADR-027 (tier 2's budget);
[ADR-011](ADR-011-named-slot-spill.md) (the answer lane's own "every failure but the last logs and
falls through" pattern, which this chain mirrors one layer down)

## Context

The overview and the development plan both describe roughly the same three-or-four-step fallback —
native, extract, local OCR — but neither pins the order tier 0 (cache) sits at relative to tier 1
(native), what each tier costs when it fails, or which failures are allowed to end the whole turn
rather than just that tier's attempt. Those are the questions `perception/lane.py` cannot be written
correctly without answering first.

## Decision

**Four tiers, walked in this order, and one rule governing all of them but the last:**

| Tier | Name | Condition | Cost |
|---|---|---|---|
| 0 | `cache` | `extract:{hash}` in Redis, else `file_extractions` in Postgres | none |
| 1 | `native` | `spec.supports_mime(mime)` **and** `size <= spec.max_file_bytes` | the answering model's own reservation, via `token_cost` |
| 2 | `llm` | a `perception`-slot candidate has budget and a closed breaker | the perception lane's fenced half (ADR-027) |
| 3 | `local` | always attempted; may still produce nothing | CPU, in a thread |

**Every tier failure but the last logs and falls through. Only tier 3's failure surfaces**, as
`core.errors.FileUnreadable` (422, `code="file_unreadable"`), naming the file, and the turn is not
written.

**Tier 0 beats tier 1, with one qualification the phase plan implies rather than states.** A stored
`llm` reading wins outright over a native passthrough. A stored `local` reading does not win outright
— it is held back as a *fallback*, tried only after tier 2 has had its own chance to produce (and
persist) something better. Without that qualification, one reading taken during a Gemini outage would
serve every later turn about that document, and `db/repo/extractions.py`'s "upgrade a `local` row,
never downgrade an `llm` one" clause could never fire.

**Tier 2 is a chain, not a single call.** The `perception` slot's candidates are walked in
capability order under the same breaker and quota gates the answer lane applies to itself.
`ContentFiltered` stops the chain outright rather than moving to the next candidate — failing over
would just launder a refusal, the identical reasoning `routing/router.py` applies to the answer lane.

**The extraction envelope is four labelled sections, summary first** (D28):

```
## Summary
## Structure
## Figures and tables
## Verbatim text
```

## Why

**Every tier but the last degrading rather than failing is the whole design philosophy in one rule.**
"Always degrade, never just fail" (`project-overview.md` §3) is a slogan until a concrete failure mode
tests it — a storage outage, a PDF PyMuPDF cannot open, a Gemini 5xx — and this chain is where it is
either true or decorative. `_guarded` enforces it in one place rather than four separate `try` blocks
scattered across the chain, specifically so the rule cannot be half-applied by a future tier added
without matching discipline.

**Only tier 3's failure is allowed to surface, because it is the only one that means something real:
the gateway cannot answer the question that was asked.** An empty text layer plus OCR that recovered
nothing is not a transient hiccup one more retry would fix — it is the honest end of every option the
system has. Answering anyway, from a document nobody actually read, is the single worst behavior the
design could produce; a 422 naming the file is the alternative that keeps the disclosure honest.

**Cache beats native because the reading is free and not meaningfully worse.** The text a tier-2 call
already produced came out of the same model that would otherwise read the bytes directly — spending
tokens in a context window to re-derive a reading that already exists is waste, not accuracy. The one
case this is the wrong call — an image question about layout or color, which extraction summarized
away — is a real cost, and it is recorded in `docs/limitations.md` rather than hidden.

**A `local` reading does not get the same trust a `llm` reading does, and the chain reflects that
directly in its ordering rather than in a confidence field alone.** `extractors.grade` never returns
`low` — `low` is exclusively tier 3's marker, which is what lets `RenderReport.degraded` mean "this
answer rests on OCR or a bare text layer" without also meaning "this answer rests on a stored reading
that happens to be a little thin." Letting a `local` row win outright over a real chance at tier 2
would mean an outage's degraded reading quietly outliving the outage, for every future user who
references those bytes — the opposite of what invariant 6's retroactive-improvement guarantee
promises.

**The extraction prompt's section order is a fitting-step decision solved in the prompt instead.**
`fitting.py` truncates an oversized document from the tail, keeping the head — a rule that predates
this phase and was never going to change for it. A summary placed last would be the first thing lost
on exactly the documents large enough to need truncating. Putting the summary first means a 200-page
report cut to 4,000 tokens keeps its summary, its shape, and its figures, and loses only the verbatim
body it could never have fit anyway — with zero change to the truncation algorithm itself, because
teaching a size-based truncator to parse document structure would be solving the same problem twice.

## Consequences

- A turn whose first-tried candidate is text-only, and whose document defeats tiers 0, 2 and 3, fails
  even when a later candidate in the same turn's `auto` chain would have read it natively — rendering
  happens per attempt, so the lane resolving for Groq has no way to know Gemini is next. Recorded as a
  known edge case rather than solved: in practice this means an image on a host with no Tesseract, or
  a scan OCR genuinely could not read, both of which are worth failing loudly on regardless.
- `_envelope`'s four-section shape is shared verbatim between `extractors.py` (tier 2) and `local.py`
  (tier 3, `_envelope` there), so nothing downstream needs to know which tier produced a reading to
  make sense of it — a summary that is "not available" from the local tier degrades exactly like a
  thin one, rather than needing a special case for a missing heading.
- `document_envelope` in `memory/render.py` is unchanged by this phase; the four sections live
  *inside* it. Raw concatenation was never on the table — a document is data the model reads, not an
  instruction it obeys, and a PDF containing "ignore your previous instructions" is not hypothetical
  (trap 11).
