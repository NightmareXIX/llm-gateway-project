# ADR-018 — Quota fails closed, and what that does to `/readyz`

**Status:** accepted · Phase 3, Step 11 · 2026-08-17
**Implements:** `phase3.md` §3 D15
**Supersedes:** the readiness half of [ADR-010](ADR-010-redis-fail-open-and-readiness.md) for the
one dependency quota actually needs; ADR-010's breaker section stands unchanged.
**Relates to:** Contract C ("Degradation"), [ADR-017](ADR-017-render-as-deploy-target.md) (the
platform the readiness verdict now runs on), [ADR-022](ADR-022-our-own-rate-limiting.md) (fails
*open*, deliberately the opposite)

## Context

Contract C states the asymmetry plainly: "fail closed on quota, fail open on caching and your own
rate limiting." ADR-010 already built the fail-open half, for the breaker, and its readiness
argument generalized to a durable rule: *a readiness probe fails only on dependencies whose
absence makes the instance unable to serve, and a fail-open dependency is by construction not
one.* Quota is not fail-open. The rule does not need a new rule to answer the question — it flips
the answer for the one dependency this phase adds a use for.

`quota/tracker.py::QuotaTracker.reserve` already does the closed half: every Redis exception
inside it returns `QuotaDecision(allowed=False, degraded=True)`, and the router treats that
exactly like an exhausted candidate — a `skipped_quota` trail entry, next candidate (D17). With
Redis down, every candidate in every chain answers the same way, so the chain always empties and
`_all_skipped` raises `RateLimited`. What Steps 3–10 left undone is the second half of the same
sentence: `app/main.py`'s `/readyz` reported Redis without ever being decided by it, which was the
correct answer before this phase and stopped being the correct answer the moment `reserve` started
depending on it.

## Decision

**Closed at the candidate is closed at the request, and closed at the request is closed at
`/readyz` — but only while `QUOTA_ENFORCEMENT` is true.**

`/readyz` now reads `get_settings().QUOTA_ENFORCEMENT` after probing Redis. If Redis is
unreachable *and* enforcement is on, the probe returns 503 with `code="redis_unavailable"` — a new
branch alongside the existing `database_unavailable` one, not a replacement for it. If enforcement
is off, the tracker is never constructed, nothing depends on Redis for a decision, and ADR-010's
original verdict applies exactly as it always has: reported, not decided by.

No new setting. `QUOTA_ENFORCEMENT` already exists (Step 1) as the kill switch for the whole
reservation mechanism; `/readyz` reading the same flag it already gates the tracker's construction
on is what keeps an instance with enforcement off from failing readiness on a dependency it is
provably not using.

## Why

**One code path, no special case.** The alternative — a bespoke "is quota actually going to fail
every request" check inside `/readyz` — would have to re-derive what `reserve` already knows: that
without Redis, every window lookup fails, every candidate is skipped, every chain empties. Reading
the same boolean the tracker's own construction reads is cheaper and cannot drift from it.

**The reasoning that makes this the opposite of the breaker's rule.** The breaker's *absence*
costs one wasted round trip — the normalized error hierarchy still catches what it would have
predicted, so a missing breaker degrades gracefully into the Phase 2 behavior. Quota's absence
costs nothing less than the thing free-tier aggregation is built on: a key that gets banned for
sustained over-limit traffic is not a degraded gateway, it is a gateway with one fewer provider,
permanently, and no amount of failover recovers a revoked credential. An instance that cannot see
its own counters is not "slower," it is one bad request away from a `RateLimited` at best and an
un-metered burst at a provider's actual ceiling at worst — and `/readyz` refusing to certify that
instance as ready is the only lever that keeps a load balancer from routing traffic into it.

**Refusing correctly is still serving, which is what makes this consistent with ADR-010's rule
rather than an exception to it.** `reserve`'s fail-closed path does not crash the process — every
chat request still gets a normalized `RateLimited` with an honest `Retry-After`. But an instance
that answers every chat request with the same 429 is not usefully different, from a fleet's
perspective, from one that is down: it occupies a rotation slot, receives traffic, and produces
nothing a client can use. ADR-010's own rule — "fails only on dependencies whose absence makes the
instance unable to serve" — answers this the moment quota's dependency on Redis is stated plainly:
the instance cannot serve, so the probe says so.

**The consequence ADR-017 already named, now realized on purpose.** ADR-017's Render migration
observed that "a fail-open dependency able to fail this probe would now manufacture a restart
loop," in defense of keeping Redis out of the probe entirely. That argument is unchanged for the
breaker — a fail-open dependency should never fail a readiness check. It was never true for quota,
which is fail-closed by design; this ADR is the decision ADR-017 was implicitly leaving for Phase
3 to make, not a contradiction of it. A restart loop during a sustained Upstash outage is the
accepted cost, spelled out in `docs/limitations.md`: it converts an invisible failure (every
request 429s, dashboards look "up") into a visible one (the instance cycles, an operator notices).

## Consequences

- `/readyz`'s 200 body is unchanged in shape (`{"status": "ok", "database": "ok", "redis": "ok" |
  "unavailable"}`); the new branch is a 503 that never reaches that body, mirroring how the
  database leg already works. Nothing that parses the 200 body needs to change.
- With `QUOTA_ENFORCEMENT=True` (the default) and Redis down: `/healthz` still answers 200 (the
  process is alive), `/readyz` answers 503, and every `POST /v1/chat/completions` answers 429
  `rate_limited` rather than hanging or 500ing. Three symptoms, one root cause, each honest about
  its own layer.
- `QUOTA_ENFORCEMENT=False` with Redis dead reproduces Phase 2's behavior exactly: `/readyz` stays
  green, the router fails over reactively on a real 429 from the provider instead of never
  reserving at all. This is the escape hatch D15 names explicitly — cheaper than a revert when the
  thing under investigation is the quota system itself.
- On Render (ADR-017), a `/readyz` 503 pauses traffic after 15s of consecutive failures and
  restarts the instance after 60s. A single-instance free deployment restarting during a Redis
  outage produces a visible gap in service rather than a silent one — noted in
  `docs/limitations.md` as the accepted trade, not a bug to chase.
- `tests/integration/test_health.py` splits its Redis-leg coverage in two: enforcement-off tests
  keep asserting ADR-010's original fail-open verdict (unreachable, hung, and starvation-avoidance
  cases all still 200), and a new enforcement-on block asserts the 503 for both an outright refusal
  and a timed-out probe, plus one test that a *healthy* Redis with enforcement on still reads 200 —
  the flag must not make the probe pessimistic about a dependency that is actually up.
