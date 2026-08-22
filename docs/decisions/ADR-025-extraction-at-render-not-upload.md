# ADR-025 — Extraction runs at render time, not at upload time

**Status:** accepted · Phase 4, Step 1 (decided) / Step 8 (landed) · 2026-08-22
**Implements:** `phase4.md` §3 D22
**Relates to:** Contract B invariant 6 (`app/memory/canonical.py`) — the rule this decision exists to
honor; [ADR-020](ADR-020-quota-reservation-placement.md) (the reserve-before-attempt discipline
`perception/lane.py` reuses); ADR-028 (the tier chain this seam runs inside)

## Context

`project-overview.md` §4.5 reads like an upload-time pipeline — a file arrives, the perception lane
runs, extracted text sits waiting for whichever model needs it. Contract B invariant 6 says the
opposite: a `file_ref` stores only the hash, and extraction "is resolved at render time from
`file_extractions`, so improving your extractor retroactively improves old conversations." The two
readings imply different systems, and the contracts doc wins by the project's own standing rule — but
saying so is not the same as working out what it costs.

## Decision

**`POST /v1/files` stores bytes and metadata and nothing else. The perception lane runs inside render
step 1, per request, per candidate**, memoized per instance on `(file_hash, native_wanted)` so a
turn's own failover does not re-pay for what it already read.

## Why

**Tier 1 cannot exist at upload time.** Native passthrough asks "does the model about to answer read
this MIME, and does it fit under its size cap?" — a question with no answer until a request names a
model. Extracting at upload would mean extracting a document Gemini was about to read directly,
spending the scarcest budget in the fleet on a reading nobody needed.

**Invariant 6's payoff is retroactive improvement, and that only works if nothing is frozen in.** A
better prompt, a bigger extraction model, a fixed bug — all of it improves every stored conversation
the next time it renders, because the message itself carries only a hash. Extracting at upload would
freeze the extraction into a moment the way a cached HTTP response freezes a page: correct when
written, silently stale afterward, with no signal that anything changed.

**The failure surface is smaller.** An upload that only writes bytes either succeeds or does not.
Extracting inline would give the endpoint a partial-success state — bytes stored, extraction failed —
and somebody has to decide what status that is and who retries it. `POST /v1/files` avoids the
question entirely by never being in a position to ask it.

**What this costs, stated plainly rather than absorbed.** The first turn that asks about a document
pays the extraction inside the chat request — on a large PDF, a real multi-second Gemini call sitting
in front of the answering call. Two things make that survivable rather than merely accepted: the
`cache` tier turns it into a once-per-document cost instead of a per-turn one, and the streaming
path's `meta` frame is emitted before the lane runs, so a client waiting on a slow extraction is shown
*why* it is waiting rather than staring at silence. Both are recorded in `docs/limitations.md` rather
than left for a user to discover.

**Memoization is not an optimization here, it is the thing that keeps the decision correct.** Render
runs once per candidate and up to three times per turn under D1's failover. Without a per-request
memo keyed on `(file_hash, native_wanted)`, a spill from Groq to Gemini would re-extract a document
that had been extracted forty milliseconds earlier — and the second extraction is the one that finds
the daily budget gone. The memo key carries the native flag, not just the hash, because the same
stored `file_ref` genuinely resolves differently for two candidates within one turn: Gemini may take
the bytes, Groq never can.

## Consequences

- `POST /v1/files` has no partial-success state to define, document, or test — it is the endpoint
  Step 3 built it as, unchanged by anything Milestone B added later.
- `PerceptionResolver` must be constructed per request (`deps.get_resolver`) and never shared or
  cached across requests — a resolver whose memo outlived its request would silently serve one user's
  extraction lock wait to another's turn.
- A document with a badly-extracted first reading is not stuck that way. The next code deploy that
  improves the extractor, or a `local` row upgraded by a later healthy `llm` call
  (`db/repo/extractions.py`'s upsert guard), reaches every conversation that ever referenced those
  bytes, not just new ones.
- The first turn about any document is measurably slower than every turn after it. This is a real,
  user-visible cost of the decision, not a benchmark artifact — worth calling out plainly in a demo
  rather than letting it look like a bug.
