# ADR-020 — The reservation is the filter, made inside the loop

**Status:** accepted · Phase 3, Step 5 · 2026-08-17
**Implements:** `phase3.md` §3 D17
**Relates to:** [ADR-015](ADR-015-attempt-cap.md) (the attempt-cap rule this extends to quota
skips), [ADR-019](ADR-019-quota-window-model.md) (what a reservation actually spends),
[ADR-021](ADR-021-quotahint-transport.md) (what corrects a reservation's estimate after the fact)

## Context

The development plan's Phase 3 task 3 reads "filter candidates by remaining quota *before*
attempting" — which sounds like a pass over the candidate list that removes exhausted entries
before the failover loop ever sees them. There are two ways to build that, and the difference is
not stylistic:

1. A separate pre-filter: ask each candidate "do you have quota?", build a thinned list, then hand
   it to the loop that attempts each one in order.
2. The reservation *inside* the loop: ask the question and spend the answer in the same atomic
   operation, per candidate, immediately before the attempt.

`selection.candidates()` is pure — it builds the chain from config and D11's latency ranking, with
no I/O and no Redis dependency. Where the quota question attaches to that chain decides everything
about correctness under concurrency.

## Decision

**The reservation is the filter. It happens inside the failover loop, per candidate, and
`selection.candidates()` stays exactly as pure as it was.** The loop's order becomes breaker →
`render` → `quota.reserve(...)` → `attempts += 1` → attempt → `commit`/`release`.

Two things move relative to the pre-Phase-3 loop, and both are deliberate:

- **`render` before `reserve`.** The reservation needs a token estimate, and the only trustworthy
  one before the call is `RenderReport.estimated_tokens` — `adapter.estimate_tokens` measured on
  the finished payload. Rendering a candidate that then gets skipped costs local CPU and no round
  trip, which is the entire saving D17 exists to bank.
- **`attempts += 1` moves from the top of the loop to immediately before `adapter.complete` /
  `adapter.stream`.** A candidate rejected by its own quota check never left the process, so under
  ADR-015's definition of an attempt ("a request that left the process") it was never one.

## Why

**A separate pre-filter is the exact race Contract C's Lua script exists to close.** "Check, then
build a filtered list, then attempt" is a read followed by a write across two round trips — under
any concurrency, the filter would pass a candidate that the reservation then has to reject anyway,
because another request's reservation landed in between. The whole reason `reserve.lua` exists as
one atomic script rather than a Redis pipeline (trap 2) is that a check and an increment separated
by *any* gap can both succeed for two callers who together overspend the limit. A pre-filter
reintroduces that gap one layer up, in application code instead of a pipeline, which does not make
it safe — it makes it harder to see.

**A quota rejection costs no attempt, for the identical reason a breaker skip does not
(ADR-015).** Three exhausted candidates at the head of a chain would otherwise spend the whole
attempt budget of three without a single request leaving the process, while a genuinely healthy
provider sat at position four, never reached. `AttemptOutcome` gains `skipped_quota` alongside
`skipped_breaker`; `RouterOutcome.attempts` counts neither.

**Moving the counter changes one observable number, and it is the more honest one.** A
`ContextTooLong` raised by `render()` itself (the `budget <= 0` misconfiguration branch) used to
report `attempts: 1`; it now reports `attempts: 0`, matching what the all-skipped path already
reports for a chain that never made a real request. The re-fit loop stays bounded by its own
`refit_used` counter, not by the attempt cap, so this move changes what gets *reported*, not what
gets *permitted*.

**Commit, release, and the case in between is where a naive implementation gets it wrong.** Three
outcomes, not two:

- **Success** → `commit(reservation, tokens_in=actual, tokens_out=actual)`. The token windows move
  by the *difference* between what was reserved and what was really spent, in either direction —
  an underestimate corrects up, an overestimate corrects down.
- **Failed after the request left the process** → `commit` with whatever was really generated —
  zero on the non-streaming path, the D1 wasted-token estimate for a stream that died mid-sentence.
  **The request counters are never given back here.** The provider counted the request against its
  own RPM/RPD the instant it accepted the connection; a 429 the gateway provoked is still a request
  the provider metered, and pretending otherwise makes the local counter permanently optimistic
  relative to the one that actually governs the key (trap 6).
- **Never left the process** — a render failure after the reservation succeeded, or a candidate
  abandoned before the call for any other reason — → `release`, which subtracts everything the
  reservation recorded, request windows included. This is the only outcome that gives the request
  counters back, because it is the only one where the provider never saw anything.

**A reservation that expires mid-flight commits nothing, on purpose, in the safe direction.**
`RESERVATION_TTL_S` is 120 seconds and a long stream can outlive it; `commit.lua` no-ops when the
reservation hash is already gone and the tracker logs `quota.reservation_expired`. Over-counting —
leaving the original estimate standing — is the safe failure: the alternative, applying the
actual-vs-estimate delta blind, would double-count a stream a `release` had already refunded on a
different code path, or apply a correction to a counter a completely different request now owns the
same hash slot for.

**Rendering before reserving, restated as a cost claim.** "A 429 you predicted is a round trip you
didn't spend" is only true if the thing spent predicting it is cheap. `RenderReport` is pure CPU —
resolve, materialize, budget, fit, `build_payload` — no I/O of its own, so a candidate skipped after
render cost nothing that mattered. Reversing the order (reserve first, render only if reserved)
would save nothing, since the reservation needs render's own token estimate to reserve against in
the first place — there is no ordering where render is skippable and the estimate still exists.

## Consequences

- `selection.candidates()` needed **no changes** for this step — it was already pure, and staying
  that way is what lets `GET /v1/models` (Step 7) reuse it directly to build the same chain the
  router would walk, without standing up a request.
- D11's latency sort still runs on the list quota has already thinned inside the loop, not on an
  unfiltered list quota then vetoes — the fix for the standing "ranking leans toward whichever
  provider is nearest its ceiling" caveat (ADR-014) falls out of the ordering for free, because the
  reservation and the sort were never in tension to begin with (trap 10).
- The router's unit suite asserts the concrete numbers: a chain of three exhausted candidates
  reaches a fourth with `outcome.attempts == 1`; a mid-stream abort commits non-zero tokens against
  the failed candidate specifically, not against whichever candidate serves the eventual restart.
- `quota=None` (enforcement off, or a caller that never wires the tracker in) skips this entire
  section of the loop — the same shape as `metrics=None` — so every router test written before this
  step stayed meaningful without a rewrite.
