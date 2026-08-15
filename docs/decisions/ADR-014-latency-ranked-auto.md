# ADR-014 — `auto` is ranked by measured latency, seeded by config order

**Status:** accepted · Phase 2, Step 4 · 2026-08-12
**Implements:** `phase2.md` §3 D11 — pulled forward from `development-plan.md` §7's stretch backlog
**Relates to:** [ADR-011](ADR-011-named-slot-spill.md) (which builds the list this sorts),
[ADR-015](ADR-015-attempt-cap.md) (the cap that bounds the walk), Contract C (frozen, which is why this
table is not in Redis)

## Context

`auto` is a sentinel, not a slot, so it has to become a concrete ordered list. The obvious answer is
config order: slots in `providers.yaml` declaration order, each slot's candidates in priority order,
flattened and de-duplicated. That answer is correct, free, and already the seed for everything below.

The question this ADR settles is whether anything reorders it. Stretch-backlog item 1 is "latency-based
routing — track p50 per provider over a rolling window, prefer fastest among available", and the
gateway's whole pitch is that it turns several heterogeneous free tiers into one product. A router that
knows Groq answers in 200ms and OpenRouter in 2s, and picks by declaration order anyway, is leaving the
most visible improvement in the system on the table.

## Decision

**Latency-ranked, with config order as the seed, the tiebreak, and the fallback.** An in-process EWMA
table in `app/usage/metrics.py` keyed by `(provider, model, mode)`; a snapshot of it is passed into
`selection.candidates()`, which reorders the chain where it has evidence. Behind
`Settings.ROUTING_LATENCY_RANKING`, default on.

Four rules, each of which is a bug if broken:

**1. Successful attempts only.** Only an attempt that produced usable content updates a series.

**2. Two series per `(provider, model)`.** Streaming ranks on time to first token, non-streaming on
total latency. A request ranks against the series matching its own mode.

**3. EWMA, not a true p50.** One float per candidate, α = 0.3.

**4. Rank only where there is evidence.** Below five successful samples a candidate keeps its config
position; ranked candidates trade places *with each other*, into the positions they already held.

Availability outranks latency, always: the sort never promotes a candidate past the breaker, because
the breaker is consulted per candidate as the router walks the sorted list and a skip costs no attempt.

## Why

**The successful-attempts-only rule is the whole design.** A provider that 429s in 80ms is the fastest
thing in the fleet by wall clock and the worst possible choice. Feed failures into the average and
`auto` learns to prefer whatever is most broken. The symptom is not an error — it is a gateway that
gets *worse* the longer it runs, and by the time someone notices, the evidence is an averaged number
with no history attached. `test_router.py` asserts it directly: a fast-failing candidate must never
appear in the table.

**Two series, because the two numbers are not comparable.** Total latency on a streamed response is
dominated by output length, so ranking streams by it does not prefer the fastest model, it prefers the
most terse one. TTFT is the number that characterizes a provider on the streaming path, which is also
why `requests.ttft_ms` exists as its own column rather than being folded into `latency_ms`.

**EWMA, because a percentile needs a window.** A rolling p50 requires a retained sample buffer per
candidate and a policy for aging it. An exponentially-weighted mean needs one float and answers the
same question well enough to sort three items. If the ordering ever looks unstable, this is the first
thing to revisit.

**The five-sample threshold is what makes a cold process predictable.** Without it, one measured
candidate would sort against a field of zeros and the first request after every deploy would go
somewhere arbitrary. With it, a cold process reproduces config order *exactly* and the ranking only ever
appears once it has earned an opinion. "Keeps its config position" is stated precisely on purpose: the
unranked candidates do not drift to either end, they stay at their index while the ranked ones permute
among the indices they already occupied.

**In-process, not Redis, because Contract C is frozen.** Sharing latency across instances would need a
new key format, and a frozen-contract change is worth making with sign-off rather than as a side effect
of Step 4. The cost is bounded: two workers on one instance converge independently within a few dozen
requests, and staleness is self-correcting, because the only way to update a candidate's number is to
actually use it. If Phase 6+ ever runs enough instances that the orderings visibly diverge, the fix is
that key format and this paragraph is the thing to re-read.

**Purity is preserved by passing the snapshot in.** `selection.py` documents itself as doing no I/O and
being a pure function of the registry. Reading a mutable module-level table would quietly end that and
take the table tests with it. The signature takes `latency=`, the router takes the snapshot once per
request, and a candidate list therefore cannot reorder underneath a retry loop that is midway through
walking it.

## Alternatives considered

**Static config order.** The original recommendation, and still what the system does whenever it has no
evidence. Rejected as the *only* behaviour because it makes the ordering a hand-maintained guess about
numbers the gateway is already measuring, and because the ranking degrades to exactly this when the
kill switch is off — so keeping it as the fallback costs nothing and keeping it as the ceiling costs
the feature.

**Ranking by success rate rather than latency.** Attractive, and wrong for this layer: the breaker
already encodes "has this been failing", with hysteresis and a cooldown ladder. A second, softer
reliability signal in the sort would fight it, and the two would disagree during recovery.

**Sharing the table through Redis.** See above — a frozen contract, deliberately not touched.

## Consequences

- **The standing caveat: ranking without a quota filter leans toward the provider nearest its ceiling.**
  Groq is the fastest thing in this pool *and* has the tightest TPM limit, so `auto` will lean on it
  until it 429s. Phase 2 has no quota data, so the only thing catching that is reactive failover, at
  the cost of one wasted round trip per exhaustion. Phase 3's quota filter is the real fix, and it runs
  *before* this sort — which is also why that ordering matters: removing exhausted candidates from the
  list beats having them merely lose a race.
- `ROUTING_LATENCY_RANKING` exists so the reordering can be switched off in one deploy if it misbehaves
  mid-phase. A flag is cheaper than a revert when the thing being debugged is the router itself.
- The table is per-process and unpersisted, so a restart forgets everything and the process behaves
  like a cold one until it has five samples per candidate again. Acceptable, and the alternative is a
  warm-start read that would have to come from somewhere Contract C does not currently describe.
- Phase 7 exposes the same table as a Prometheus gauge and needs no second source.
