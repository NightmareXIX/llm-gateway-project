# ADR-022 — Our own rate limiting, and the key-format amendment it forced

**Status:** accepted · Phase 3, Step 10 · 2026-08-17
**Implements:** `phase3.md` §3 D20
**Relates to:** Contract C (`app/cache/keys.py`), [ADR-007](ADR-007-auth-model.md) (one user, one
budget), [ADR-017](ADR-017-render-as-deploy-target.md) and D15 (quota fails *closed*, this fails open),
D16 (why the provider counters cannot have the window this one gets)

## Context

Every limit built so far protects providers from the gateway. None protects the gateway from its own
users: one enthusiastic script can drain a shared free tier that everybody else is on, and no upstream
counter distinguishes "the pool is spent" from "one caller spent it".

Contract C reserves `rl:{user_id}:{window_start}` for this, and `limits.yaml` has carried a `gateway:`
block since Step 1. What was undecided is the window algorithm, what the counter is keyed on, what
happens when Redis is unreachable, and which endpoints it applies to.

## Decision

**A two-bucket sliding window, keyed on `user_id`, limits from YAML, failing open, on the chat endpoint
only.**

- **Sliding, not fixed.** `count = previous × (1 − elapsed) + current`, which is
  `quota/windows.py::sliding_count`. This is the one key in Contract C that carries a `window_start`
  segment, so the interpolation is available here and nowhere else.
- **`user_id`, never `api_key_id`** (D7, ADR-007). `api_key_id` stays on the `requests` row for
  attribution and pays for nothing.
- **Limits live in `config/limits.yaml`** under `gateway:`, keyed by `users.tier`. The risk register's
  standing rule: every limit in this system is configuration.
- **Fails open.** A Redis error, or a tier the YAML does not describe, lets the request through with a
  warning.
- **429 through `core.errors.TooManyRequests`** — `code = "rate_limited"`, which `_CODE_BY_STATUS`
  already reserved for that status, with `Retry-After` in delta-seconds.
- **`POST /v1/chat/completions` only.**
- **A rejected request is refunded.** The counter is incremented first and given back when the answer
  is no.

**And the amendment: the key gains a window segment.**
`rl:{user_id}:{window_start}` becomes `rl:{user_id}:{rpm|rpd}:{window_start}`. This is a change to a
frozen contract, made with sign-off rather than absorbed silently.

## Why

**One key cannot address two windows.** D20 enforces `rpm` *and* `rpd`. A daily bucket always starts on
a multiple of 86,400, which is also a valid minute boundary — so under the original format the two
buckets are the *same Redis key* at every midnight UTC, one request increments it twice, and a user's
minute allowance is silently halved for sixty seconds a day. The alternatives were to enforce only the
per-minute limit (leaving a slow all-day drain unprotected, which is the case the shared pool most needs
covering) or to offset the daily bucket's numbering so it cannot collide (no contract change, but the
key stops being self-describing and the next reader has to reverse-engineer why a bucket id is off by
one). Nothing had ever written the old format, so the amendment cost no migration — only this record.

**Sliding here, fixed there, and the contrast is the point.** The provider-side counters accept a fixed
window's twofold boundary overshoot because Contract C gives them no `window_start` to interpolate from
and the frozen key schema is worth more than the precision (D16). Our own limiter has the segment, so
it has no excuse: a fixed window would hand a caller a full fresh allowance the instant the minute
rolled over, which is exactly the burst the limit exists to stop.

**Fail open, which is the opposite of quota's rule and not inconsistent with it.** Quota fails closed
because blowing through a provider's published limit gets a key banned, and a revoked credential is a
provider lost permanently that no failover recovers (D15). This limit protects our own capacity from
our own users. Refusing every request because a counter is unreachable converts a Redis blip into a
total outage — trading a real failure for a hypothetical one.

**The refund is what makes it a limit rather than a lockout.** Counting the rejected request keeps
inflating the bucket a hammering client is waiting on, so the interpolated count never falls back under
the limit and the caller can never return, however long they wait. Incrementing first and refunding on
refusal keeps `INCR`'s atomicity — a read-then-write is the check-then-increment race Contract C
mandates Lua to avoid elsewhere — without that consequence.

**`Retry-After` is solved for, not guessed.** Under a sliding window nothing "resets", so the honest
number is the instant the arithmetic first admits *another* request — which includes the one the caller
is about to make. A header whose expiry produces a second 429 is worse than no header at all, and the
unit suite asserts exactly that: wait the advertised seconds, and the retry succeeds.

**Reads are not limited.** Generation is what spends a free tier. Rate-limiting the conversation list
makes the UI feel broken and protects nothing.

## Consequences

- `keys.rate_limit` takes three arguments, and `keys.GatewayWindow` is a separate type from
  `keys.QuotaWindow` — the gateway counts requests per user and never tokens, so a `tpm` cannot reach
  a key that has no meaning for it.
- Two credentials belonging to one user share one budget, including a session token and an API key
  used concurrently. That is ADR-007's rule showing through, and there is a test for it.
- A tier missing from `limits.yaml` is unlimited rather than blocked. It is our config gap, and the
  caller should not pay for it — but it is silent apart from a `rate_limit.unknown_tier` warning.
- The limiter lives in `app/deps.py` rather than being a standalone dependency that authenticates:
  `app.auth.dependency` imports `app.deps` for `SessionDep`, so a `Depends(get_principal)` inside
  `deps.py` would be an import cycle. `api/v1/chat.py` composes the two into `RateLimitDep`, which is
  also where the "chat endpoint only" scope becomes visible.
- Four Redis commands per request are added to the ~four this phase already added. Upstash's free tier
  has a command-per-day ceiling; Step 11 checks the headroom against a day of real use.
- `RATE_LIMIT_ENABLED=false` removes the protection entirely, for load-testing the gateway itself. It
  is the same shape as `QUOTA_ENFORCEMENT` and `CACHE_EXACT_ENABLED`: the object is not constructed,
  and the endpoint sees `None`.
