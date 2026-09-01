# ADR-044 — `/metrics` is hand-rolled, process-local where it must be, and honest about it

**Status:** accepted · Phase 7, Step 4 · 2026-09-01
**Implements:** `phase7.md` §3 D49 (`project-overview.md` §11, where `/metrics` is a stretch goal)
**Relates to:** [ADR-014](ADR-014-latency-ranked-auto.md) (the precedent: process-local numbers under a
frozen Contract C), [ADR-013](ADR-013-hand-rolled-breaker-and-retries.md) (the same "why is there no
library here" argument), [ADR-018](ADR-018-quota-fails-closed.md) (why a Redis failure here is
different from one in the request path)

## Context

`project-overview.md` §11 lists a Prometheus endpoint as a stretch goal and says a hand-rolled circuit
breaker "is a better interview story than importing one". `app/usage/metrics.py`'s module docstring has
said since Phase 2 that the breaker's fail-open counter is deliberately absent "until Phase 7's
`/metrics` endpoint gives it a reader".

Two real questions, and one temptation. Where do the numbers come from — and specifically, are the
counters shared across workers? And does a metrics endpoint justify `prometheus_client` as a runtime
dependency?

## Decision

**`GET /metrics` returns `text/plain; version=0.0.4` built by hand in `app/usage/metrics.py`, beside
`LatencyTable`.** No `prometheus_client`. Five families, not D49's four — the breaker's fail-open
counter is the fifth, and it is the reason the module docstring's five-phase promise finally resolves:

| Metric | Type | Source | Labels |
|---|---|---|---|
| `gateway_requests_total` | counter | process-local, `usage/logger.py`'s facades | `provider`, `model`, `status`, `key_pool` |
| `gateway_request_duration_ms` | histogram | process-local, fixed buckets | `provider`, `mode` (`complete`/`stream`) |
| `gateway_breaker_fail_open_total` | counter | process-local, `routing/circuit_breaker.py` | `provider`, `model` |
| `gateway_breaker_state` | gauge | **live**, `CircuitBreaker.peek` per candidate at scrape time | `provider`, `model` |
| `gateway_quota_remaining` | gauge | **live**, `QuotaTracker.remaining` under `SYSTEM_SCOPE` at scrape time | `provider`, `model`, `window` |

**Counters are incremented in `usage/logger.py`'s facade functions and nowhere else** — plus the
breaker's own, which has to be counted where the fail-open happens and funnels through one private
`_count_fail_open` so its two call sites cannot drift.

**Counters are process-local.** They live in one process's memory and reset on every deploy and cold
start; under more than one worker a scrape hits one of them and reads that worker's share — a *sample*,
not a total. This deployment happens to pin `WEB_CONCURRENCY=1` in `render.yaml` (a 0.1-CPU free
instance), so today the sample is the whole of it, but that is a capacity decision and not a property
of the metric. The limitation goes in `docs/limitations.md` with the standard production answer rather
than being papered over. The gauges are read live from Redis and are correct on any worker.

**Labels never carry a `user_id`, an email, a conversation id, or free text.**

**Access:** `METRICS_ENABLED` (default true) and `METRICS_TOKEN` (default unset). Disabled returns
**404**, not 403. A set token is required as `Authorization: Bearer <token>` and compared with
`secrets.compare_digest`. Any Redis failure drops both gauge families — HELP and TYPE included — and
still returns 200 with the counters.

## Why

**No `prometheus_client`** (trap 15). Five families and an exposition format that is twenty lines of
string building do not justify a runtime dependency, and the dependency is not free: it brings its own
multiprocess mode, its own registry globals, and its own opinions about what a label is — all to
produce text this module produces in a function. The hand-rolled breaker set this precedent for the
same reason, and the exposition is the part with a *specification* to test against, which makes it the
easiest kind of thing to write by hand correctly.

**The counters live in one funnel.** `usage/logger.py`'s facades are already the single place every
terminal outcome passes through. A counter incremented at three call sites is a counter that will be
wrong within two phases, and the wrongness is invisible — a metric that under-counts looks exactly like
traffic that did not happen. `metrics` arrives there optional and defaulting to `None`, the same shape
D36 established, so no pre-existing call site needed editing.

**That funnel is also what supplies the labels honestly.** `key_pool` is threaded from the same
`outcome.key_pool`/`failure.key_pool`/`result.key_pool` the wire already discloses, so the metric and
the response cannot disagree about who paid. `quota_scope` deliberately is **not** a label: it carries a
real `user_id` on a private-key turn, which is exactly what trap 10 forbids.

**Unbounded label cardinality is how a metrics endpoint takes down the thing scraping it** — and the
two identifiers a gateway is most tempted to label with (`user_id`, `conversation_id`) are also the two
that turn the endpoint into a privacy surface. The rule is asserted rather than written down: a unit
test renders the exposition and fails if any label value anywhere matches a UUID.

**The histogram stores its buckets non-cumulatively and accumulates only at render time.** A
non-monotonic bucket series is the one hand-rolled-exporter bug that parses fine and charts wrong;
storing raw counts per bucket and summing on the way out makes it unrepresentable rather than merely
untested. `+Inf` equals `_count` by construction, and a test asserts it.

**A cache hit counts but is not timed.** A replay's latency is a property of Redis, and folding it into
a provider's histogram drags that distribution toward zero in proportion to how well the cache is
working — a metric that gets more flattering the less work you do. A `status='replayed'` idempotent
replay gets the same treatment for the same reason.

**Per-worker counters are a real limitation, and "fixing" it would be a Contract C amendment**
(trap 9). Sharing them needs new Redis keys, and that is a change made with sign-off, not as a side
effect of a polish phase — the identical argument ADR-014 makes about the latency table. The two
problems have one solution, which is noted in `phase7.md` §9 as the thing that would unlock
cross-instance latency ranking at the same time. Note that `phase7.md`'s own D49 wrote "Render runs
two workers"; the deployed `render.yaml` pins one. The caveat survives the correction — a counter that
resets on deploy is still not a total, and the second worker is one dashboard slider away — but the
number was worth checking rather than repeating.

**404 when disabled, not 403.** An endpoint that is switched off should not advertise its own
existence. 401 on a bad token, because there the endpoint does exist and the credential is wrong.

**Gauges are dropped rather than faked when Redis is down.** A metrics endpoint that 500s during an
incident is useless exactly when it is needed; one that reports a stale or zero quota gauge is worse
than useless, because it reports a number that is wrong rather than absent. A `degraded` breaker
decision renders no sample at all for the same reason — rather than a flattering `closed`.

## Consequences

- Gauges are enumerated per candidate over `registry.describe()` at scrape time, including the
  internal `perception` slot: it spends a real budget, so omitting it would make the gauge disagree
  with the counters.
- `gateway_quota_remaining` reports the **shared pool's** remainder and nothing else. A per-user
  series would need a `user_id` label, which the rule above forbids outright — a BYOK holder's own
  counters are visible to them through `/v1/models` and `/v1/admin/quota` (ADR-040), which is a
  session-scoped surface rather than a scrape target.
- On Render there is no private network, so `METRICS_TOKEN` is not optional there. `docs/deploy.md`
  says so in the variable table rather than leaving it to judgment.
- The endpoint's output was read by eye against the format `promtool check metrics` accepts; the
  suite's assertions are on `# TYPE` lines, cumulative bucket monotonicity, quote/backslash escaping,
  and byte-stable sorted output. Sorted output is what makes a diff of two scrapes readable.
- Adding a sixth family is a new constant, a new `describe`-style render function, and a test. There
  is no registry to register with and no collector interface to implement, which is the whole benefit
  being claimed here.
