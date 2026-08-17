# Limitations

The honest-edges document. Not a bug list — a record of what was deliberately scoped out, what a free
tier costs you no matter how carefully the gateway is built, and what "works" means here versus what it
would mean in a paid, production system. Opened in Phase 2, because streaming and mid-stream failover
are where the free-tier trade-offs first become visible to a user rather than just to a log line.

---

## Streaming and failover

**A restart discards tokens the free tier already charged.** When D1's restart fires, the failed
candidate generated real output — tokens a provider's own metering counted against RPD/TPM — and the
gateway throws the text away because it produced no usable answer. `wasted_tokens_out` records this
honestly rather than hiding it (`ADR-012`), but recording a cost is not the same as avoiding it: a
message that triggers two restarts before succeeding has spent roughly three times the quota of one
that did not, on a pool that was already the scarce resource this whole project exists to manage.

**Two attempts on very different free models can produce visibly different answers.** `served_by` and
`substituted` disclose *which* model answered, and a `restart` event discloses that a swap happened
mid-generation — but disclosure is not consistency. A response that started in one model's voice and
finished in another's, after a restart, reads as coherent to a human ear at the sentence level and
inconsistent at the paragraph level in a way this project does not attempt to smooth over. The dialogue
this project is built to demonstrate is "what happens when a provider is slow but not fully down," not
"how do you make three different free models sound like one."

**A sleeping free-tier Render instance can drop a stream mid-flight.** Free web services spin down
after 15 minutes without inbound traffic and take about a minute to come back, which is accepted
deliberately (`development-plan.md` §5's risk register). The failure mode specific to streaming: an
instance reclaimed while a long-running SSE response is still open can end that response before `done`
is ever sent, and the client sees a connection drop rather than an in-band failure — the one shape of
failure `route_stream`'s error hierarchy cannot classify, because nothing about it comes from a
provider. Unlike Fly's `min_machines_running`, there is no setting that removes the window: on Render
the only fix is a paid instance type that does not spin down.

**A cold start now includes a database migration, so an unreachable Supabase means the gateway does
not come back at all.** Render's free plan has no pre-deploy command, so `alembic upgrade head` runs
in the container's start command (`render.yaml`, [ADR-017](decisions/ADR-017-render-as-deploy-target.md)).
On a deploy that is the right trade — a broken migration cancels the deploy instead of reaching
traffic. On a *wake-up* it means a dependency failure that would previously have produced a running
instance failing `/readyz` now produces no running instance, and the difference is visible to a user
as a timeout rather than a 503. The escape hatch is editing `dockerCommand` in the dashboard.

**`/readyz` failures restart the instance rather than just draining it.** Render pauses traffic to an
instance after 15 seconds of consecutive health-check failures and restarts it after 60. Because the
probe reaches Postgres by design (ADR-009), a Supabase outage cycles the service instead of leaving it
up and honestly unready. This is the platform behaviour ADR-010's argument depends on not being
extended to Redis: a fail-open dependency allowed to fail this probe would manufacture a restart loop
out of an Upstash blip.

**Every request pays a cross-region round trip to Postgres.** `fly.toml` co-located the app with its
Supabase project on the argument that an app a continent from its database is slow in a way no tuning
fixes. Render has no Tokyo region, so that co-location is gone: Singapore is the closest available and
each of the several Postgres round trips per request costs roughly 60–90ms instead of single digits.
Nothing about the gateway's logic changed; its floor latency did.

**`auto`'s latency ranking leaned toward the provider nearest its own limit, before Phase 3 — now
fixed, not merely mitigated.** `ADR-014` named this as the standing caveat it inherited from D11:
ranking by measured speed with no quota awareness preferentially selects whichever provider is
fastest *because* it has the least contention, which on a free tier is often the one closest to a
429. Phase 3's reservation happens *inside* the failover loop, before D11's latency sort ever runs
([ADR-020](decisions/ADR-020-quota-reservation-placement.md), trap 10) — the sort now always runs on
a list quota has already thinned, so a candidate near its ceiling is removed from contention rather
than merely deprioritized. `ROUTING_LATENCY_RANKING` still exists as the kill switch for the ranking
itself, independent of this fix.

**Restarting a stream is not free even when it never fires.** The first-token budget (`D13`,
`DEFAULT_FIRST_TOKEN_TIMEOUT_S = 10.0`) means a client's very first byte of *any* streamed response can
legitimately take up to 10 seconds if the first candidate is slow to start, before the gateway has even
decided whether a restart will be necessary. That is a deliberate trade against a worse alternative
(silence with no way to distinguish a slow provider from a dead gateway), not a cost that disappears
once a fast provider is picked.

---

## Quota, caching, and rate limiting

**A fixed window permits up to 2× its limit across a boundary it straddles.** Contract C's quota
key (`q:{scope}:{provider}:{model}:{window}`) has no `window_start` segment to interpolate a true
rolling window from, so RPM/TPM are fixed counters aligned to their first increment
([ADR-019](decisions/ADR-019-quota-window-model.md), D16). Thirty requests at `:59` and thirty more
at `:01` is sixty inside a sixty-two-second span against a limit of thirty. `QUOTA_HEADROOM_FRACTION
= 0.1` holds the effective ceiling under the real one by more than that jitter costs, and the
provider's own 429 plus the breaker is the same backstop Phase 2 already had — this is a bounded,
brief overshoot rather than a way to blow through a key's real limit.

**The exact-match cache is global, deliberately, and the residual disclosure is a real one.**
`cache:exact:{sha256}` carries no per-user segment ([ADR-023](decisions/ADR-023-exact-cache-identity-and-scope.md),
D19) — the content of a byte-identical request *is* its identity, and scoping by user would destroy
the hit rate that makes exact-match caching worth having on a free-tier budget. The trade: a cache
hit tells you someone else asked this exact question recently, at `temperature: 0`, with no way to
know who. Not recoverable from a hit in any more specific way, and not a new class of leak beyond
what a shared, unscoped cache always implies.

**Gemini's quota is metered per Google Cloud project, not per Redis deployment.** Two environments
(a local dev box and the deployed instance, say) pointed at the same Gemini API key share one real
budget at Google, while each keeps its own independent Redis counters — both believe they have the
full published limit, or D8's half of it, when together they are drawing down one pool. `limits.yaml`
already documents this; nothing in `quota/tracker.py` can detect or correct for it, because the
tracker has no way to observe another process's Redis. Worth checking deliberately before running a
local dev session and the deployed instance against the same Gemini key at the same time.

**Upstash's free tier has a command-per-day ceiling, and Phase 3 roughly quadruples the Redis
commands per request.** Breaker checks, quota reserve/commit/release, the exact-cache lookup, and
our own rate limiter's two-bucket read all land on the same Redis instance
([ADR-018](decisions/ADR-018-quota-fails-closed.md), [ADR-022](decisions/ADR-022-our-own-rate-limiting.md)).
Check the Upstash dashboard after a day of real traffic and note the actual headroom rather than
assuming the free tier absorbs it — this is a real ceiling a demo can hit that has nothing to do
with any LLM provider's own limits.

**A Redis outage now takes an instance out of rotation by design, not by accident.** With
`QUOTA_ENFORCEMENT=True` (the default), `/readyz` fails closed the moment Redis is unreachable
([ADR-018](decisions/ADR-018-quota-fails-closed.md), D15) — the opposite of the breaker's fail-open
rule (ADR-010) for the same dependency, because quota's absence risks a banned provider key rather
than one wasted round trip. On Render (ADR-017) that means a sustained Upstash blip cycles the
instance through repeated restarts rather than leaving it up and quietly refusing every chat
request. The escape hatch is `QUOTA_ENFORCEMENT=false`, which reproduces Phase 2's reactive-failover
behavior exactly and lets `/readyz` stay green through a Redis outage — at the cost of the fail-closed
guarantee this whole section is about.

---

## Provider-pool honesty

**Answer quality varies by which model actually served a given response.** Free-tier models differ
significantly in capability, and the gateway's whole design accepts routing a request to whichever one
is available rather than guaranteeing a specific one answers. `provider_used`/`model_used` are logged
per message specifically so this stays visible and debuggable rather than a silent, unexplained quality
swing.

**Rate limits are organization-level for Groq and project-level for Gemini, not per-key.** A second key
on either provider adds nothing — `keys_resolution` (Phase 6) has to treat a user's private key on these
providers as a billing change, not a capacity one, and the gateway does not attempt to work around this
by acquiring additional keys on the same account, which several providers' terms explicitly prohibit
anyway (see below).

**Multi-key farming on a single provider is out of scope, on purpose.** The value this project
demonstrates is combining *independent* providers' free offerings, not generating extra keys or
projects on one provider to inflate a single quota — the latter is against most providers' terms and
answers a different, less interesting engineering question.

**Free-tier data-privacy terms differ by provider, and some may use submitted prompts for model
training.** This matters most for the perception lane (Phase 4), which routes uploaded file content to
a third-party provider for extraction — a real privacy trade-off, not a hypothetical one, and worth a
visible disclosure in the UI once that lane exists rather than a line buried here.

---

## Explicitly out of scope for v1

**Tool-call history across providers.** Incompatible schemas between providers make lossless
translation of tool/function-call history a genuinely unsolved problem in production gateways, not just
this one. D3's answer — the first tool call pins `conversations.pinned_model`, and every later turn in
that conversation ignores slot selection — is a documented limitation rather than a broken translation
attempt. Being able to say *why* this was scoped out, not just that it was, is the stronger position.

**Context-window mismatches are truncated, not summarized.** D4 drops the oldest non-system messages and
inserts a visible omission marker. Summarizing older turns into a compact system message instead is
designed as a seam (`app/memory/summarize.py`) but deliberately not built — it costs quota on every
switch to a smaller-context model and adds a failure mode (a bad summary) that truncation does not have.

**No perception lane yet.** Phase 3 reserves the half of Gemini's daily budget the perception lane
will spend (`reserved_fraction: 0.5`, D8) and builds the accounting seam
(`quota/lanes.py::reserve_perception`, a typed signature raising `NotImplementedError` rather than a
silently-passing stub) — but nothing increments it, because `POST /v1/files` does not exist until
Phase 4. `render()`'s attachment-resolution step still returns `NoAttachments` unconditionally.

**Not a production system.** Built entirely on free tiers for a portfolio/learning purpose, which means
lower throughput, higher latency variance, and lower consistency than a paid setup would have — stated
plainly here rather than oversold anywhere else in the docs.
