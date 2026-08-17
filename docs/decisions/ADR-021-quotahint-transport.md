# ADR-021 — `QuotaHint` reaches the tracker through a contextvar, not a widened contract

**Status:** accepted · Phase 3, Step 6 · 2026-08-17
**Implements:** `phase3.md` §3 D18
**Relates to:** Contract A (`app/providers/base.py`, frozen), [ADR-020](ADR-020-quota-reservation-placement.md)
(what a hint corrects), `app/core/logging.py` (the existing contextvar this mirrors)

## Context

Every provider adapter implements `rate_limit_headers(response) -> QuotaHint | None`, and every one
is unit-tested against a recorded fixture. Nothing has ever been able to call one, because
`complete()` returns a `Completion` and `stream()` yields `StreamChunk`s — neither carries the
`httpx.Response` the rate-limit headers live on, and the `httpx.Response` itself never survives
past the end of `HttpProviderAdapter._request`. That is not an oversight left over from Phase 2; it
is what Contract A's frozen shape implies. Groq and OpenRouter publish, on every response, exactly
how much of the caller's allowance is left — ground truth the tracker's own reservation counters
can only approximate from what the gateway *thinks* it sent.

## Decision

**A module-level `ContextVar[QuotaHint | None]` in `app/providers/base.py`, published by
`_request`/`_stream_events`, drained by the router once per attempt.**

`publish_hint(hint)` is called after every response an adapter's transport layer receives —
success or error, because a 429's headers are the most informative ones the system ever sees.
`take_hint()` reads and immediately clears the variable, so a hint can never be attributed to the
attempt that follows the one that actually produced it. The router calls `take_hint()` once per
attempt, whether it succeeded or failed, and hands whatever comes back to
`QuotaTracker.apply_hint(spec, scope=scope, hint=hint)`.

**A hint corrects; it never authorizes.** `apply_hint`'s `_apply_one` computes
`used = limit − remaining` from the hint and writes it with `SET` **only when it increases** the
locally tracked counter, and never above the effective limit. A hint that would *lower* the counter
is logged and dropped.

## Why

**It does not touch Contract A.** No new protocol method, no changed signature, no field added to
`Completion` or `StreamChunk`. An adapter that never calls `publish_hint` — because its provider
sends no rate-limit headers, Gemini's case — is simply an adapter whose contextvar stays `None`
forever; nothing about it looks different from one that was never wired up at all.

**It is per-request by construction, which instance state on the adapter singleton would not be.**
The adapters are built once, in the lifespan, and serve every concurrent request the process
handles. `self._last_hint` on the adapter would be a data race with a plausible-looking name — two
concurrent requests against the same provider would clobber each other's hint, and the bug would
only show up as an occasionally-wrong counter correction, which is close to the least debuggable
failure mode this codebase has. `contextvars.ContextVar` is scoped to the async task that sets it,
which is exactly the request lifetime this needs and precisely the mechanism `app/core/logging.py`
already uses to carry `request_id` — not a new pattern the codebase has to learn twice.

**The alternative was real, and is written down rather than silently passed over.** Widening
`Completion` with a `hint: QuotaHint | None` field is cleaner to read at the call site and would
have been taken if it were available — but it is a change to a frozen contract, which CLAUDE.md
requires asking about first rather than absorbing into a step's diff. The contextvar sink gets the
same outcome without that conversation, and remains strictly worse only in readability, not in
correctness. If sign-off for the `Completion` widening is ever given, it is the better version of
this decision and should replace it outright rather than live alongside it.

**Hints correct, they never grant, because a hint is stale the instant it arrives.** By the time
the router reads a hint off a response that already happened, an unknown number of other requests
against the same `(scope, provider, model)` may have already reserved against the *current* state
of the counter — a hint is a snapshot of "remaining" as of one response, not a live value. Letting
a hint move a counter *down* — i.e., grant budget back — would let a stale snapshot overwrite a
counter that concurrent reservations have since correctly advanced, reopening exactly the
check-then-increment race the Lua script exists to close (trap 2), just moved into the reconciliation
path instead of the reservation path. Moving a counter *up* has no such hazard: it only ever makes
the tracker more conservative than it already was, which is the same direction fail-closed already
leans.

**Disambiguating which window a hint describes is a real ambiguity, not a formality.** A hint names
a resource — "requests remaining" or "tokens remaining" — not a window. When a model declares only
one window of that dimension (`rpm` alone, no `rpd`, say), there is nothing to disambiguate.  When
it declares two, `_closest_window` compares the hint's reported reset duration against each
candidate window's own reset width: a reset a few seconds out reads as the rolling window, one tens
of thousands of seconds out reads as the daily one. With no reset reported and more than one
candidate, the honest move is to apply nothing — a wrong guess here would apply a minute's worth of
usage to a day's counter or vice versa, silently.

## Consequences

- `publish_hint`/`take_hint` are the only two functions this ADR adds; both are pure contextvar
  reads and writes, so the unit suite for `providers/base.py` can assert clearing behavior (a hint
  never leaks into a second `take_hint()` call) without any Redis or network fixture at all.
- The router's per-attempt sequence gains exactly one line after every attempt, success or failure:
  `hint = base.take_hint(); if hint: await quota.apply_hint(...)`. Applying a hint is never allowed
  to be fatal — a Redis failure inside `apply_hint` is caught and logged, because by the time it
  runs the attempt it describes has already happened and there is nothing left to refuse.
- Gemini, which publishes no `QuotaHint` at all, is unaffected end to end: `take_hint()` returns
  `None`, `apply_hint` is never called, and its counters continue on the gateway's own estimate —
  exactly the "opportunistic, never required" behavior the docstring on `QuotaHint` already promised
  before this step existed to consume it.
