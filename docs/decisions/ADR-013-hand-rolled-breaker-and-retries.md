# ADR-013 — A hand-rolled circuit breaker, and hand-rolled retries

**Status:** accepted · Phase 2, Step 3 (breaker) and Step 4 (retries) · 2026-08-12
**Implements:** `phase2.md` §4 Step 3
**Relates to:** [ADR-010](ADR-010-redis-fail-open-and-readiness.md) (Redis fails open), Contract A's
error hierarchy and Contract C's `cb:{provider}:{model}` key

## Context

Phase 2 needs two resilience mechanisms that libraries exist for: a circuit breaker
(`pybreaker`, `purgatory`, `aiobreaker`) and retry-with-backoff (`tenacity`, which the project
overview's tech-stack list names outright). Writing either by hand is the kind of decision that needs
a reason attached, because "we rewrote a solved thing" is the default reading.

Both mechanisms also have to make choices the frozen contracts do not settle: what state to keep,
where, when to trip, and what happens when the store holding that state is unreachable.

## Decision

**Both are hand-rolled.** `app/routing/circuit_breaker.py` now; the same-provider retries land in
Step 4's router. No `pybreaker`, no `tenacity`.

The breaker is three-state per `(provider, model)`, with its state in the `cb:{provider}:{model}` hash
and a one-hour TTL. `FAILURE_THRESHOLD` (5) consecutive breaker-eligible failures — or a single
`RateLimited` — open it; the cooldown ladder runs 30s → 300s, doubling; a probe closes it or re-opens
it one rung up.

### Supporting choices, and why

**The state has to be shared, and a library's is not.** Every breaker library keeps state in the
process. This gateway runs multiple workers, and per-process state means each one independently
discovers that Gemini is down — paying the full round trip to learn what a sibling already knew, and
producing an outage that looks intermittent because it depends which worker took the request. Once the
state lives in Redis, the library is contributing an in-memory state machine we are not using.

**The trip rule is our error hierarchy, not an exception allowlist.** Libraries trip on exception
types. What should trip this breaker is `error.breaker_eligible` — a `ClassVar` on Contract A's
normalized hierarchy, set per class so no call site can override a single occurrence. That flag is the
whole design: `EmptyResponse` is a failure, is failover-eligible, and must **not** count, because a
free tier returning 200-with-nothing is annoying rather than broken and opening on it takes a working
provider out of rotation for the full cooldown. Encoding that in a library's allowlist means keeping
two sources of truth for one fact, and the copy will drift.

**Transitions are computed on read, never swept.** `open → half_open` follows from
`now - opened_at >= cooldown_s`. A gateway with no timer has no timer to fail, and when every instance
is equal there is no correct owner for a sweep. `half_open` is still *written* when a probe is claimed
— not needed by the code, but it means `HGETALL` during an incident shows the state the gateway is
acting on rather than something only the source reveals.

**The half-open probe is claimed with `HSETNX`, and leased by resetting `opened_at`.** `HSETNX` is
atomic, is a single round trip, and — unlike an `asyncio.Lock`, which `JwksCache` legitimately uses one
layer down — is exclusive across *instances*, which is the scope shared state requires. The lease
exists because a process that dies between claiming the probe and reporting its outcome would
otherwise hold `probe_holder` until the hash's one-hour TTL, keeping a possibly healthy provider out of
rotation. Resetting `opened_at` on claim makes "has not reported within one full cooldown" the
staleness test, and keeps the hash to the five fields Contract C documents rather than inventing a
sixth for a lease.

**The accepted cost of that lease: two callers racing to steal a *stale* probe can both get through.**
The steal is a read-then-write and is therefore not atomic. The cost is one extra request to a provider
we were about to probe anyway; the alternative — a Lua script for the whole decision — buys
exactly-once on a path that only runs after a process has already died, and would put a routing script
in the directory the repo structure designates for quota. Recorded here rather than discovered later.

**A single `RateLimited` opens immediately.** Waiting for five means four more requests against a
provider that has already said it has nothing left. The penalty for that is not another 429 — it is the
key being suspended, which takes the shared pool down for every user.

**`retry_after_s` outranks the ladder, but is capped at `COOLDOWN_MAX_S` (300s).** Capping the
provider's own hint looks like discarding ground truth, and there is a concrete reason: Contract C
fixes this hash's TTL at one hour, so a longer cooldown would have the state expire mid-cooldown and
the breaker silently close — strictly worse than re-probing early, and invisible when it happens.
A free tier answering "retry in eight hours" is a *quota* fact anyway, and quota is Phase 3's job with
a reset-window model built for it. Re-probing early costs one request.

**`HINCRBY`'s return value is the decision input.** The failure count is never read-then-written; the
threshold test uses what Redis actually stored. Two instances failing at once cannot both see four and
both write five. This is the same principle as `api_keys.touch_last_used` pushing its throttle into the
`WHERE` clause: make the check and the write one operation, in the store.

**Redis unreachable means allow.** ADR-010, applied throughout: `allows` returns permissive and
`degraded`, and `record_*` log and swallow rather than raise into a request whose outcome is already
decided. The breaker is an optimization that skips attempts we predict will fail; without the
prediction the normalized error hierarchy still protects the request, at the cost of one round trip.
ADR-010 asks for a counter alongside the warning; today that is the structured `breaker.fail_open`
event, countable in aggregation. The in-process counter belongs in `usage/metrics.py`, which Step 4
creates — noted rather than faked here.

**One log event, `breaker.transition`, with `from_state`/`to_state`.** Phase 7's chaos demo is a log
tail of this line, which only works if there is exactly one line to tail. It follows
`auth.token_rejected`'s established shape — one event name, a discriminating field. (`from` is a Python
keyword, hence the suffix.)

### On the retries

*Completed in Step 4, with one amendment.*

The same-provider retries are hand-rolled for a reason specific to this codebase rather than a general
objection to `tenacity`: a decorator wraps a coroutine, and Step 9's streaming path is an async
*generator* whose failures happen after the first token has already been yielded. A retry decorator
cannot re-enter that. The policy is also small enough to read in one screen — driven by
`retryable_same_provider`, jittered with full jitter over an exponential ceiling, and never for
`RateLimited` (that is the router's job, and hammering a 429 is how a key gets banned).

**Amendment: one retry per candidate, not two.** Written here in advance as "two jittered retries for
`Unavailable`", before the interaction with D1's three-attempt cap had been worked through. Retries and
failover draw on the same budget, so two retries make the first candidate's failure fatal to the whole
chain — the router spends attempt, retry, retry on one provider and never fails over. The reasoning and
the accompanying yield rule are in [ADR-015](ADR-015-attempt-cap.md), which supersedes this paragraph's
original count.

## Consequences

- The breaker's correctness is now our problem, and it is covered accordingly: the module is at 100%
  line and branch coverage, with the state machine driven against an injected clock rather than
  `sleep`.
- The twenty-concurrent-caller test does **not** exercise the branch where a rival claims the probe
  between our read and our `HSETNX` — coverage proved it settles the race one step earlier. That branch
  needed a deliberately interleaved test. Worth remembering the next time a concurrency test looks
  green: it may be passing one layer above the thing it names.
- A breaker that has been quiet for an hour forgets its failure count. That is deliberate — the count
  is consecutive failures, and an hour of silence is not evidence of anything — but it means a slow
  drip of failures spaced more than an hour apart will never open the circuit.
- Step 4's router is the breaker's first and only caller, reaching live traffic through Step 5's
  `BreakerDep` — one instance per request over the process-wide Redis client, since the hash is the
  state and there is nothing in-process to share. Until Step 4 it was a state machine with no caller,
  which is what let it be tested as one.
- If Phase 6 ever runs enough instances that the stale-probe steal race stops being theoretical, the
  fix is the Lua script this ADR declined — and the reasoning above is what should be re-read first.
