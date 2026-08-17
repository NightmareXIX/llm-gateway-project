# ADR-023 — Exact-cache identity, scope, and disclosure

**Status:** accepted · Phase 3, Step 9 · 2026-08-17
**Implements:** `phase3.md` §3 D19
**Relates to:** Contract C (`cache:exact:{sha256}`, no scope segment — frozen),
[ADR-022](ADR-022-our-own-rate-limiting.md) (the other cache-adjacent asymmetry: this fails open on
both read and write, same as that limiter does)

## Context

D5 already settled the shape: cache non-streaming responses directly, assemble a stream after
`done` and replay a hit as a synthetic stream. Building it raises four questions Contract C's key
format does not answer on its own: what exactly gets hashed, when caching is skipped even though
the request looks otherwise cacheable, whether the cache is scoped per user or global, and how a
hit discloses itself to a client that needs to know the difference between "fast because cached"
and "fast because lucky."

## Decision

**What is hashed (`cache/exact.py::request_hash`).** `sha256` over a canonical JSON serialization
of: a cache-format version integer, the **requested** slot, the full canonical history (role,
`seq`, content blocks verbatim, in order), and `temperature`/`max_tokens`/`top_p`/`stop`.
`sort_keys=True` makes the encoding deterministic across dict-key insertion order without
reordering the history list itself — the list's order is where the real identity lives.

**When it is skipped (`is_cacheable`, shared verbatim by both the read and write sides).**
`temperature > 0` — a cached creative answer is worse than a fresh one, since identical inputs are
not expected to reproduce a high-value output. Any history carrying a `file_ref` anywhere, at any
role. A response that would carry `degraded: true`.

**Scope: global, no per-user segment.** Contract C's key is `cache:exact:{sha256}` and stays that
way.

**Disclosure: `X-Cache` with three values — `HIT`, `MISS`, `BYPASS` — on both the streaming and
non-streaming path.**

## Why

**The requested slot is hashed; the served model is not.** `auto` and a named slot are different
questions even when today's routing happens to resolve them identically — a client that asked for
`auto` and one that asked for a specific slot should not silently share a cache entry just because
the answer came out the same this time. The model that actually *answered*, by contrast, is not
part of the question at all: whoever answers has to be able to serve a cache hit for it, and folding
`served_by` into the hash would mean a hit's existence depends on which candidate happened to win a
particular failover race — an accident of provider availability, not a property of the question
asked.

**The version integer, not a key migration, is what invalidates the whole namespace.** Any future
change to what gets folded into the hash — a new generation parameter, a change to how content
blocks serialize — bumps `CACHE_FORMAT_VERSION` and every old key stops matching on its own, aging
out via the existing TTL rather than needing an explicit purge.

**`file_ref` is excluded because its meaning can change underneath a hash that does not cover it.**
Phase 4's extraction confidence is not yet part of the canonical schema's hashable surface, and a
cached answer to "what does this PDF say" would silently outlive a re-extraction that corrected a
wrong OCR read. Excluding any history containing a `file_ref` is the honest scope line until Phase
4 gives extraction confidence a place in the hash to begin with — narrower than strictly necessary
today, but the alternative is a cache that can serve a stale answer with no way to tell.

**Global scope is the right call, not merely the frozen key's default.** The cache's identity *is*
its content — two users who sent byte-identical canonical histories asked the literal same
question, generation knobs included. Scoping by user would require adding a segment Contract C does
not have, and would destroy the hit rate that makes exact-match caching worth having in a
free-tier-constrained system in the first place: two users independently asking "what is the
capital of France" at `temperature: 0` is exactly the redundant round trip this feature exists to
eliminate. The residual disclosure — "someone else asked this exact thing recently" — is not
recoverable from a hit in any more specific way than that, and is worth exactly the one sentence it
gets in `docs/limitations.md` rather than a design change.

**Three `X-Cache` values, not two, because `MISS` alone is ambiguous in exactly the way that gets
debugged.** "Why did this not cache?" needs to distinguish "first time this exact question was
asked" from "this can never be cached, temperature is 0.7" — collapsing both into `MISS` would make
every non-deterministic request look like a cold cache on every single call, which is
indistinguishable from a cache that silently is not working at all.

**A hit still writes a message row and a `requests` row, with `served_by` naming the original
model.** Some model really did produce those words the first time, and disclosing that honestly —
rather than inventing a "cache" pseudo-provider — keeps `provider_used`/`model_used` meaningful on
every assistant message without a special case. `meta.attempts` is `0` on a hit, meaning no provider
was attempted *this* turn — a value the frontend's `attempts > 1` substitution marker already
treats as unremarkable. The `requests` row carries `cache_hit=true` with `tokens_in`/`tokens_out`
of `0`: the row answers "what did this cost," and a hit costs nothing.

**Read and write share one predicate because a disagreement between them is invisible until it is a
production incident.** If the write side considered a response cacheable that the read side would
then refuse to look up — or the reverse — the result is either a cache that fills with entries
nothing ever hits, or one that silently serves something it should have refused to write. One
function, called from both places, makes that class of bug impossible rather than merely unlikely.
`degraded` is the one input only the write side can supply — nothing is knowable about a response's
own degradation before it exists — so the read side calls the same predicate with the default
(`False`) rather than needing a second function.

## Consequences

- `CachedResponse` is a deliberately smaller shape than `ChatCompletionResponse` — no
  `request_id`, no `message_id` — because both of those name the call that is *reusing* the cached
  text, not the one that produced it, and serializing them into the cache would make every replay
  carry an identity that belongs to someone else's original request.
- A hash-stability golden test pins a committed hash for a fixed canonical history: any change to
  what `request_hash` folds in is caught as a diff against a committed value, forcing a deliberate
  `CACHE_FORMAT_VERSION` bump rather than a silent shift in what old cache entries mean.
- `chunk_for_replay` breaks near whitespace rather than at an exact character count, specifically so
  a replayed delta never splits a word in a way a real provider's own chunk boundary would not —
  the synthetic stream's event *sequence* is asserted byte-comparable in shape to a real one (same
  event names, same field names), not byte-identical in content, since content length is what the
  chunking is deliberately approximate about.
- No artificial delay on replay. A fake typing effect would be a lie about where the answer came
  from; `X-Cache: HIT` is the honest signal that the response arrived faster than a live one would.
