# ADR-010 — Redis fails open, and a readiness probe fails only on what it needs

**Status:** accepted · Phase 2, Step 2 · 2026-08-12
**Supersedes:** [ADR-009](ADR-009-readiness-probe-scope.md), in the part that scopes `/readyz` to
Postgres "for now" and defers the Redis leg to Phase 3. The fail-closed/fail-open split it records
for quota and caching stands unchanged.
**Implements:** D9 of [phase2.md](../../doc/reference/phase2.md) §3
**Relates to:** §2.3 of [contracts-and-phase1.md](../../doc/reference/contracts-and-phase1.md)
(Contract C, "Degradation")

## Context

Redis arrives a phase early. The plan scheduled it for Phase 3 alongside quota tracking, but Phase 2
Step 3's circuit breaker needs state that every instance can see — a per-process breaker would let
each worker rediscover the same dead provider independently, which is most of the value gone.

That creates a question Contract C does not answer. The contract specifies the asymmetry for quota
(**closed**) and for caching plus our own rate limiting (**open**), and says nothing about the
breaker, because the breaker was scheduled for the same phase as quota and the question never came up
separately. It arrives alone.

It also puts ADR-009 in an awkward position. That ADR scoped `/readyz` to Postgres on the explicit
reasoning that "a readiness probe that fails on an unused dependency takes the app out of rotation for
no reason", and stated that Phase 3 would add the Redis leg "at the same time as `app/cache/client.py`,
and not before". `app/cache/client.py` now exists. Taken literally, the ADR's own consequence section
says to add "one more awaited check inside the existing `asyncio.timeout` block, and one more key in
the response body" — which would make Redis able to fail the probe.

Fly reads `/readyz` for two things: gating a rollout, and deciding whether a running machine receives
traffic.

## Decision

**The breaker fails open.** An unreachable Redis means every candidate is treated as `closed` and
allowed. Log every fail-open at warning; a permanently-down Redis should be visible, not silent.

**`/readyz` reports Redis and is not decided by it.** The 200 body becomes
`{"status": "ok", "database": "ok", "redis": "ok" | "unavailable"}`. There is no Redis 503 branch.

**The Redis probe has its own bound, shorter than the database's, outside its timeout block.**
`READYZ_REDIS_TIMEOUT_S = 1.0` against `READYZ_TIMEOUT_S = 5.0`.

### Supporting choices, and why

**The breaker is an optimization, not a safety mechanism.** It skips attempts we *predict* will fail.
When the prediction is unavailable, the normalized error hierarchy still protects the request: the
provider 429s, `parse_error` classifies it, the router fails over. The cost of a missing breaker is one
wasted round trip per exhausted provider — the exact cost the gateway paid by design before Step 3
existed, and will pay again in Phase 3 for anything the quota filter cannot predict.

**This is the opposite of the quota rule, and the difference is the point.** Quota fails closed because
the penalty for blowing through a provider's published limit is not a 429 — it is the key being
suspended, which takes the shared pool down for every user. That is irreversible and external.
Breaker-open protects *our* availability, and nothing about a missing breaker state can get a key
banned. Same dependency, opposite verdict, because the question is not "is Redis important" but "what
does being wrong cost".

**Failing closed would be strictly worse than not having a breaker at all.** It turns one Redis blip
into a total outage of a gateway whose providers were all healthy — a self-inflicted failure, in a
component whose entire purpose is preventing self-inflicted failures.

**The readiness rule generalizes, which is why this supersedes rather than amends.** ADR-009 answered
"is Redis used yet?"; that question has an expiry date and it has now passed. The durable rule is:
*a readiness probe fails only on dependencies whose absence makes the instance unable to serve, and a
fail-open dependency is by construction not one.* Under that rule Postgres fails the probe because
every authenticated request touches it, Redis does not because none of them need it, and Phase 3's
quota tracker will not change the answer either — a fail-*closed* dependency makes requests refuse,
but refusing correctly is still serving, and an instance that cannot serve requests is not made better
by also being removed from the pool that reports the problem.

**Reported anyway, because an invisible outage is the failure mode.** Everything Redis does here is
either an optimization or a counter. A dead Redis produces no user-visible symptom — just a slower
gateway with a breaker that has forgotten everything — so if the probe did not say so, nothing would.

**The separate, shorter bound exists so a fail-open dependency cannot fail the probe by starvation.**
Sharing the database's `asyncio.timeout` block, as ADR-009 suggested, means a hung Redis spends the
database's budget and the probe returns `database_unavailable` — a 503 caused by Redis, reported
against Postgres, pointing the investigation at the wrong service. A `PING` is the cheapest command
Redis has; one second is already generous, and anything slower is a server that is not answering
rather than one that is busy.

**Warming Lua scripts at startup is never fatal, for the same reason.** `SCRIPT LOAD` failing means
Redis is down, and every script loads on demand through the `NOSCRIPT` path anyway. Refusing to boot
over it would convert a cache outage into a deploy outage.

## Consequences

- An instance with a dead Redis serves every request correctly and more slowly. That is the intended
  degradation, and the warning log is the only place it appears.
- A permanently-down Redis will not be caught by the orchestrator. It has to be caught by reading
  `/readyz`'s body or the `redis.unreachable` warning — worth a dashboard line in Phase 7, and worth
  saying out loud that "the probe is green" is not the same claim as "everything is working".
- Phase 3 must honour the quota half of ADR-009 (fail **closed**) on the same client that this ADR
  makes fail open. Two opposite behaviours over one connection is not an inconsistency to be tidied
  up later; it is the decision, and a future refactor that unifies them would silently pick one.
- The breaker's tests have to cover the Redis-down path explicitly (`fakeredis` cannot be asked to
  fail, so it is a stub), because a fail-open path is by definition the one that produces no symptom
  when it is wrong.
- `/readyz`'s 200 body grew a key. It is a probe, not a public API, but anything asserting on it by
  equality — the integration suite did — needs updating alongside.
