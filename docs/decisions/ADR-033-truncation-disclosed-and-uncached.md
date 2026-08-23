# ADR-033 — Truncation is disclosed on the wire, and a truncated turn is never cached

**Status:** accepted · Phase 5, Step 8 · 2026-08-23
**Implements:** `phase5.md` §3 D34 and D35 — one fact (a history got truncated) reaching two different
destinations, argued together because they share one premise
**Relates to:** D4 (`contracts-and-phase1.md` §1 — the locked decision that chose truncation over
summarization); ADR-023 (`request_hash`'s reasoning, which D35 deliberately does not reopen); Phase 4's
`degraded`/`extraction_tier` wire fields, whose three-hop path (`MessageMeta` → both response shapes →
the frontend indicator) this decision copies rather than reinvents

## Context

D4 chose truncating the oldest non-system messages over summarizing them, on the grounds that truncation
is testable and summarization adds a failure mode (a bad summary) truncation does not have. The cost D4
accepted, explicitly, is that a user can get an answer built on two thirds of their actual conversation.
`memory/render.py`'s own docstring already names `RenderReport.messages_dropped` as "the honest-degradation
story D4 owes" — and before this phase, that number was computed, logged, and dropped on the floor. No
response field, no stored `meta` key, and no UI element ever said so. A user reading a coherent-looking
answer had no way to know it was missing the first half of what they'd said.

A second, newly reachable problem sits directly behind the first. `cache/exact.py::request_hash` folds in
the *requested* slot and the full canonical history, deliberately, per ADR-023 — the served model is an
accident of a failover race, not part of the question asked, and that reasoning stays correct. But under
`auto`, the served model decides the context window, which decides how much of the history actually
reached the model. Two byte-identical `temperature: 0` requests can resolve to two different servers —
Gemini with its 1M-token window, or Groq with 128k — and produce one answer built on the whole thread and
one built on its last twenty turns. ADR-023's hash cannot distinguish them, so whichever answer landed in
the cache first is served to the second, structurally different request for up to `EXACT_CACHE_TTL_S`.

## Decision

**D34 — `messages_dropped` takes the same three hops `extraction_tier` already took in Phase 4, because
copying that path *is* the correct implementation.**

1. `MessageMeta.messages_dropped: int = 0`, round-tripped through `to_jsonb`/`from_jsonb` via the existing
   `_int` helper. A stored assistant row with a non-zero value means that answer was built on a partial
   history — a fact that survives closing the tab and reopening the thread tomorrow.
2. `ChatCompletionResponse.messages_dropped: int = 0`, sourced from `outcome.report.messages_dropped` on
   the non-streaming path.
3. `DoneEvent.messages_dropped: int = 0`, mirrored onto the streaming path's `_State` and read at both
   `done`-construction sites — the success path and the failure path, both, the same pair Phase 4 had to
   hit twice for `extraction_tier` (trap 8).
4. The frontend `ModelIndicator` renders it beside the existing `degraded` notice, in the same disclosure
   register — never as an error.

**An integer, not a boolean.** `RenderReport.truncated` already exists as a derived property, and the
frontend can derive the same boolean from a non-zero count if it wants one. But "148 earlier messages
omitted" and "some earlier messages were omitted" are different sentences to read, and only the first one
tells a user whether it is worth scrolling up to check what got cut.

**D35 — the write side declines to cache a turn whose history was truncated.** `is_cacheable` gains a
`truncated: bool = False` parameter, defaulted permissive for the read side and supplied by the write
side from `outcome.report.truncated` — the identical shape `degraded` already has in the same function.
The streaming write side reuses the `messages_dropped` field `Collector` already carries on `StreamResult`
rather than adding a second, parallel boolean that would carry the same fact under a different name.

## Why

**Truncation had to become visible before it could become a caching problem, because the caching problem
*is* an instance of the same missing disclosure one layer down.** A cache hit that silently replays a
truncated answer to a request that would not have been truncated is the same failure as an assistant
message that silently omits it said so — the gateway knowing a fact about its own answer's completeness
and not saying so. D34 makes the fact visible on the wire; D35 is what keeps that fact from becoming false
the moment the cache gets involved. Writing them as one ADR reflects that they are one argument, not two
unrelated fixes that happen to share a step number.

**Copying `extraction_tier`'s exact three-hop path, rather than designing a new one, is deliberate
conservatism.** Phase 4 already worked out where a disclosure field needs to land to reach a user
honestly — stored meta, both response shapes, both construction sites on the streaming path, the frontend
indicator — and got it right. A different shape for `messages_dropped` would be a second pattern to
maintain for no benefit; the same shape means a reviewer who understood `extraction_tier`'s Phase 4 PR
already understands this one.

**D35 is asymmetric on purpose, and the asymmetry is not an oversight.** Only the write side gates on
`truncated`; the read side is never given the parameter to pass. The read side cannot know whether a hit
*would* have been truncated without first rendering against a candidate it has not chosen yet — that is
exactly the chicken-and-egg problem the whole cache exists to avoid paying twice. And if an entry exists
in the cache at all, it was, by construction, written by a turn that passed the write-side gate — meaning
it was *not* truncated. A stored entry is definitionally a whole-history answer. A second gate on the read
side would therefore be dead code checking a condition that can never be true for anything actually
sitting in the cache.

**Folding the served model or its context window into `request_hash` was considered and rejected**,
because it would reverse ADR-023's own reasoning for a case ADR-023 already weighed and answered: the
served model is an accident of failover, not part of the question. The alternative — refusing to cache
anything requested under `auto` — was also rejected, because it throws away the majority of the cache's
value (most traffic is `auto`) to fix a narrow edge case (long histories crossing a small-context boundary
under `auto` specifically). Refusing to cache only the turns that were actually truncated is the smallest
change that closes the actual bug without touching either of those two working pieces of the system.

## Consequences

- `messages_dropped` is `0` on a cache hit, always — not the number the original turn recorded (trap 3).
  Nothing was rendered this turn; the original count lives on the original turn's own stored row, the same
  split `chat.py`'s `_serve_cache_hit` already makes for `extraction_tier` on a hit.
- No Alembic migration. `MessageMeta` gains one key inside its existing JSONB column, and `from_jsonb`'s
  existing leniency about absent keys is precisely what makes an old row read back as `0` rather than
  needing a backfill (trap 7) — correct, because nobody knows in hindsight whether a pre-Phase-5 turn was
  truncated, and asserting `0` for those rows would be asserting something unproven, not something true.
- A 200-message history driven into a small-context slot is now a demoable, green-tested path end to end:
  it answers, the response carries a non-zero `messages_dropped`, the outbound payload carries exactly one
  omission marker, no provider ever raises `ContextTooLong`, and the turn writes no cache entry — the
  `development-plan.md` exit criterion named literally, not merely implied.
- The two axes (`degraded`, `truncated`) refuse independently in `is_cacheable` — a document read locally
  under a small-context slot can be truncated *and* degraded at once, and either alone is sufficient to
  decline the write.
