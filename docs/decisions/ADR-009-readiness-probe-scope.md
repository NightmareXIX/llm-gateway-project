# ADR-009 — Readiness checks Postgres only, and what Redis being down means

**Status:** superseded in part by [ADR-010](ADR-010-redis-fail-open-and-readiness.md) · Phase 1, Step 11 · 2026-08-11
**Relates to:** §2.3 of `doc/reference/contracts-and-phase1.md` (Contract C, "Degradation")

> **Superseded in part.** Redis arrived in Phase 2 Step 2, one phase early, because the circuit
> breaker needs shared state. ADR-010 replaces the readiness half of this document: `/readyz` now
> *reports* Redis without being decided by it, and the general rule is stated in terms of what a
> dependency's absence costs rather than whether it is used yet. The consequence below that predicts
> "one more awaited check inside the existing `asyncio.timeout` block" was wrong — a fail-open
> dependency sharing that budget can fail the probe by starvation.
>
> **The fail-closed/fail-open split below stands unchanged** and is still the governing decision for
> quota, caching and rate limiting. ADR-010 extends it to the breaker (open) and explains why that is
> the opposite verdict from quota on the same connection.

## Context

Step 11 of the Phase 1 plan lists `/readyz` as checking "DB + Redis reachable". Redis is running —
in compose locally, on Upstash in production — and `REDIS_URL` is a required setting. But nothing
reads it. Quota tracking, the exact-match cache and the circuit breaker all arrive in Phase 3, and
`app/cache/client.py` does not exist yet.

Meanwhile Fly uses `/readyz` for two things: gating a rollout, and deciding whether a running
machine receives traffic.

So the literal reading of the checklist has a concrete cost. A blip at Upstash would take a healthy
instance out of rotation, and a bad enough one would block a deploy — over a dependency that no code
path touches. The outage would be entirely self-inflicted, and the symptom ("the gateway is down")
would point nowhere near the cause.

There is a second, separate question the contract raises and nothing has yet answered in writing:
once Phase 3 *does* read Redis, what should happen when it is unreachable?

## Decision

**`/readyz` checks Postgres and nothing else, for now.** Postgres is on the critical path of every
authenticated request — the JWT path upserts a `users` row, every conversation read is
ownership-scoped in SQL — so an instance that cannot reach it genuinely cannot serve. Redis, today,
has no such claim to make.

The check is bounded by `READYZ_TIMEOUT_S` (5s). An unreachable database refuses fast, but a *hung*
one would otherwise hold the probe open until the orchestrator's own timeout, which is the
"slow but not down" case this project cares about elsewhere too.

**`/healthz` never touches the database at all.** Liveness answers as long as the process is up. If
it checked Postgres, a database blip would read as a dead process and the orchestrator would restart
a perfectly healthy app — converting a recoverable dependency failure into a crash loop.

**Phase 3 adds the Redis leg**, at the same time as `app/cache/client.py`, and not before.

**When Redis is unreachable, the behaviour is asymmetric** — this is the part Contract C §2.3
explicitly asks to be written down:

- **Quota fails closed.** No counter state means no way to know what is left. Refusing the request is
  strictly better than blowing through a provider's published limit, because the penalty for that is
  not a 429 — it is the key being suspended, which takes the whole shared pool down for every user.
- **Caching fails open.** A cache miss is a slower correct answer. There is no version of "the cache
  is unavailable" that justifies refusing to answer.
- **Our own rate limiting fails open.** It protects the gateway from its own users, which is a real
  concern but a smaller one than refusing all traffic. Upstream quota tracking is the backstop that
  still fails closed underneath it.

The rule generalizes: fail closed where the failure is *irreversible and external* (a banned
provider key), fail open where it is *reversible and internal* (a slow request, a user briefly over
their own limit).

## Consequences

- The deployed service deviates from the Step 11 wording. Recorded here rather than left as a silent
  difference between the plan and the code — someone reading both should find the reason in one hop.
- `/readyz` will need revisiting in Phase 3. The seam is small: one more awaited check inside the
  existing `asyncio.timeout` block, and one more key in the response body. The 503 branch, the error
  envelope and the "log the exception, return only the `request_id`" behaviour are already built and
  tested.
- Readiness cannot detect a *degraded* instance — one reachable but slow. Out of scope for Phase 1;
  latency-based routing is in the stretch backlog and would be the mechanism.
- Because `/readyz` gates rollouts, a bad `DATABASE_URL` secret fails the deploy rather than shipping
  a broken version. Worth breaking once on purpose to confirm.
- The fail-closed/fail-open split is a decision recorded ahead of the code that implements it. Phase
  3 has to honour it or amend this ADR; what it must not do is decide it again by accident, one
  `except` block at a time.
