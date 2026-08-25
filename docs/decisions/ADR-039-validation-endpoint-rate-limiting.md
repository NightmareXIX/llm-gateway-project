# ADR-039 — Rate-limiting the key-validation endpoint

**Status:** accepted · Phase 6, Step 3 · 2026-08-26
**Implements:** `phase6.md` §3 D43 (`project-overview.md` §9.8)
**Relates to:** [ADR-022](ADR-022-our-own-rate-limiting.md) (the limiter this reuses, and the key
format this does *not* change), [ADR-007](ADR-007-auth-model.md) (keyed on `user_id`)

## Context

§9.8: "Rate-limit the key-validation endpoint itself, since it's an obvious target for abuse."
`development-plan.md` suggests 5/hour/user.

The abuse is specific and worth naming: `POST /v1/provider-keys` makes a **live upstream call on a
caller-supplied credential** before it stores anything (§9.2). That turns the gateway into a free,
authenticated oracle for testing whether a stolen API key is valid — against three providers, with
our IP reputation and our rate limits absorbing the cost.

`deps.RateLimiter` already has everything needed: a two-bucket sliding window, a refund on rejection,
fail-open, and a solved-for `Retry-After`. What it did not have is an hourly window —
`keys.GatewayWindow` was `Literal["rpm", "rpd"]`, and `RATE_LIMIT_WINDOW_S` mapped exactly those two.

## Decision

**Add `"rph"` to `GatewayWindow` and `3600` to `RATE_LIMIT_WINDOW_S`, and give `RateLimiter` a second
entry point** — `enforce_one(user_id, window, limit)` — which the two provider-key routes that reach
a network call with `("rph", 5)`. The chat path's `enforce(principal)` is untouched.

**The limit is a constant in `api/keys.py`, not YAML.** `limits.yaml`'s `gateway:` block is per-tier
user *throughput policy*; an anti-abuse floor on one endpoint is neither per-tier nor throughput, and
putting it there would invite a "plus tier gets 50 key checks an hour" that nobody wants.

## Why

**`enforce_one` takes its ceiling as an argument rather than consulting the tier table**, which is
the whole reason it is a second entry point and not a flag on the first. `enforce` answers "may this
user make another chat request, given their tier"; `enforce_one` answers "may this user hit *this*
endpoint again". Two questions, and the refund-and-raise logic they share is factored into a private
`_raise_if_over` so the duplication is in neither of them.

**Five an hour is generous for the real use and hostile to the abuse.** A person adding keys for
three providers, mistyping one, and re-checking a broken row still fits. A script enumerating stolen
keys does not.

**Fail open, with the rest of `RateLimiter`** (ADR-022). A Redis blip must not make the settings page
permanently unusable; the endpoint behind it still validates before storing, so the worst case is the
pre-Phase-6 behaviour.

**This is not a Contract C key-format change, and that was worth checking rather than assuming.**
`rl:{user_id}:{window}:{window_start}` is unchanged — a third legal value in an existing segment is
what that segment is *for*, and ADR-022 amended the format precisely so it could carry more than one
window. No new builder, no new format, nothing to migrate. Recorded here so the next reader does not
have to re-derive that the question was considered. (Contract C *did* gain one key this phase, for a
different reason entirely — see ADR-036.)

## Consequences

- `GET /v1/provider-keys` and `DELETE /v1/provider-keys/{provider}` are **not** limited. Neither
  reaches a provider, and rate-limiting a list read makes the settings page feel broken while
  protecting nothing — the same call ADR-022 made about the conversation list.
- The 429 is the ordinary `TooManyRequests` envelope with `Retry-After` in delta-seconds, so
  `ProviderKeysSection` renders it as a **wait** rather than a failure, reusing `ErrorState`'s own
  rounding. The wording is this endpoint's own ("Too many key checks"), because "every model is at
  its limit" is a sentence about a different thing.
- A user's chat throughput and their key-check budget share no counter: `rl:{u}:rpm`, `rl:{u}:rpd`
  and `rl:{u}:rph` are three keys, and a unit test asserts `enforce_one` does not move the ones
  `enforce` reads.
- `RATE_LIMIT_ENABLED=false` removes this too — the limiter is not constructed and the route sees
  `None`, the same shape every other switch in this codebase has.
