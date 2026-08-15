# ADR-011 — A named slot's failover chain spills into the rest of the fleet

**Status:** accepted · Phase 2, Step 4 · 2026-08-12
**Implements:** `phase2.md` §3 D10
**Relates to:** D2 (a specific exhausted slot fails over silently, then discloses),
[ADR-014](ADR-014-latency-ranked-auto.md) (which sorts the chain this produces),
[ADR-015](ADR-015-attempt-cap.md) (which bounds how much of it is walked)

## Context

D2 settles what happens when the slot a client explicitly asked for is exhausted: fail over silently,
then disclose which model actually served the request. `selection.candidates()`'s Phase 1 docstring
says a named slot expands to "that slot's own candidates".

Those two rules agree right up to the moment a slot's *every* candidate fails, and then they do not.
`general` and `fast` each carry three candidates today; if all three of `fast`'s are rate-limited, the
docstring's reading says the request fails while three healthy models sit one slot over.

## Decision

**Spill.** The named slot's candidates come first, in priority order. Behind them comes the rest of the
fleet — every routable candidate across every slot in config order — minus what is already in the list.
The attempt cap, not the chain length, is what stops the walk (ADR-015).

The de-duplication is on `(provider, model)`, never on provider alone.

## Why

**Without the spill, `substituted` is unreachable.** This is the decisive argument and it is
structural, not aesthetic. `_is_substitution` compares `requested_slot` to `served_slot`. If the chain
can only ever contain candidates from one slot, those two strings cannot differ, so the field is
`False` by construction — and with it the response's provenance block, which the contracts call the
load-bearing honesty mechanism that makes D1 and D2 defensible rather than merely convenient. A design
that makes an always-emitted field structurally incapable of ever being true has not implemented that
field; it has decorated it.

**It is what D2 actually promises.** "Silently fail over, then disclose" describes substitution across
slots. Failing over *within* a slot is not substitution — the client asked for `fast` and got `fast` —
and needs no disclosure at all.

**De-duplicating on the pair is not a detail.** Free-tier limits are per-model: two Groq models are two
independent budgets and two independent breakers. Collapsing on provider would drop the second, third
and fourth candidates of every chain the moment the first one shared their provider, which reads in
production as "failover stopped working after the first model" and is invisible in a chain that happens
to alternate providers.

## Alternatives considered

**Stop at the slot boundary and return D2's structured error** (`slot_unavailable`, `retry_after_s`,
`suggestion: "auto"`) — the development plan's original choice, before D2 overrode it. It respects
explicit intent most literally, and it is the one option that makes "the user picked `fast` and got
`fast` or nothing" true. Rejected because the contracts already overrode it: D2 chose availability plus
disclosure over refusal plus honesty, on the grounds that a working answer labelled "served by Gemini
Flash instead" is a better product than an error with a suggestion the user has to act on. Re-litigating
that here would be relitigating a locked decision.

**Spill only into `auto`'s highest-priority candidate**, so a named slot degrades to one substitute
rather than the whole fleet. Rejected as a distinction without a mechanism: the attempt cap already
bounds the walk at three, so "the whole fleet" and "one substitute plus the cap" differ only in which
of them is written down. Bounding by the cap keeps one rule instead of two.

## Consequences

- `substituted` becomes reachable for the first time. Step 5 wired it through: a request for `fast`
  answered by `general` now returns `substituted: true`, and
  `test_a_rate_limited_slot_is_substituted_and_says_so` is an assertion that could not have been written
  before this decision. The frontend's `ModelIndicator` gets the same field in Step 11.
- A client that asked for `fast` can be served by `general`'s model. That is D2's intent, and the
  response says so on every message; a client that genuinely needs one model must pin, not name a slot.
- The chain is now usually the whole fleet, which makes the attempt cap the operative limit on how long
  a failing request takes. ADR-015 owns that bound, and the two must be read together.
- `selection.candidates()` stays pure and stays a table test. The spill is list arithmetic, not policy
  that needs a runtime.
