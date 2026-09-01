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

## The perception lane

**The first turn about any document pays for its own extraction, in front of the answer.** D22
(ADR-025) puts extraction inside the request that needs it rather than at upload time, on purpose —
tier 1 cannot exist at upload, and invariant 6's retroactive-improvement guarantee only works if
nothing about a reading is frozen in before a model is chosen. The honest cost of that decision: a
large PDF's first question pays a real, multi-second Gemini call in front of the answering call. The
`cache` tier (tier 0) turns this into a once-per-document cost instead of a per-turn one, and the
streaming path emits its `meta` frame before the lane runs specifically so a client waiting on a slow
extraction is shown *why*, rather than staring at silence that looks identical to a hung connection.

**A cached `llm` reading can outlive the moment a `native` passthrough became available, for up to
`EXACT_CACHE_TTL_S`.** D29 folds a `file_ref`'s hash into the exact-cache's identity rather than
excluding every attachment from caching outright — two identical questions over identical bytes are
the same question, and the cache hit rate this buys is worth having. The residual: for up to one
hour, an answer built from a tier-2 extraction is served from cache even after the fleet's
availability shifts such that a fresh request would have resolved `native` instead. The answer itself
was never degraded — `degraded=True` is the one condition the write side already refuses to cache
(trap 14) — so this is a staleness window on *which tier answered*, not on correctness.

**Tier 0 beating tier 1 is the wrong call exactly once: a question about layout, not content.** D25
puts the cache ahead of native passthrough because the cached text came from the same model that
would otherwise read the bytes directly, and the free option should win when both are available. That
reasoning holds for "what does this document say" and breaks for "what color is the header on page
3" — a question the tier-2 extraction prompt was never asked to answer and the summary it wrote
necessarily discarded. There is no flag that routes around this; a user who genuinely needs a visual
read of a document neither the cache nor a fast passthrough exists for has to ask again after the
cache entry expires, or wait for a native-capable candidate to be tried.

**OCR is capped at `PERCEPTION_OCR_MAX_PAGES` (default 10), and pages past the cap are simply not
read.** An unbounded OCR pass over a 400-page scan is minutes of CPU on a request that still has to
return inside the client's timeout (trap 15). `perception/local.py` says which pages were skipped
directly in the extracted text rather than silently truncating, but the tier-3 reading of a long scan
is genuinely partial — a fact worth surfacing to a user asking about page 200 of a 300-page document
that only the first ten pages of were ever OCR'd.

**PyMuPDF is AGPL-3.0.** This project is not distributed as a closed-source product, so the licence
costs nothing here — but it is a real constraint on anyone forking this codebase into something that
is. `pypdfium2` (BSD) is the documented swap (ADR-030, D30) should that ever matter; the local tier
is one module, and the swap is one function inside it.

**An uploaded document is sent to a third-party provider for extraction, and free-tier terms vary on
what that provider may do with it.** This is the concrete case `development-plan.md`'s risk register
and `project-overview.md` §10 both flagged before the perception lane existed: tier 2 routes a
document's bytes to Gemini, whose free tier's training-data terms differ from a paid one's.
`PERCEPTION_LOCAL_ONLY=true` forces every extraction to stay on-box, at the cost of tier 2's quality —
and the frontend composer discloses this trade-off before the send that would trigger it, not after,
per the overview's own standard for what "visible" disclosure means.

---

## Provider-pool honesty

**Answer quality varies by which model actually served a given response.** Free-tier models differ
significantly in capability, and the gateway's whole design accepts routing a request to whichever one
is available rather than guaranteeing a specific one answers. `provider_used`/`model_used` are logged
per message specifically so this stays visible and debuggable rather than a silent, unexplained quality
swing.

**A retired model ID takes the whole gateway down, not just its own candidate.** Free-tier catalogues
rotate, and a model that disappears answers with HTTP 404 `model_not_found`. Every adapter's
`parse_error` maps a 404 to `BadRequest` — failover-ineligible by the Contract A table — on the
reasoning that our config is wrong and the next provider would only be asked the same wrong question.
That reasoning does not survive contact with the failover chain: the next candidate is a *different*
provider and a *different* wire name, and it would have answered fine. In August 2026 Groq retired
`llama-3.3-70b-versatile` and `llama-3.1-8b-instant`, which were candidate 0 of `general` and `fast`
respectively; because a `BadRequest` stops the loop, every request the deployed gateway served failed
with `bad_request` while Gemini and OpenRouter sat healthy behind them. The fix was a two-line config
edit (`config/providers.yaml`), but the blast radius is the point: **`GET /v1/models` cannot warn about
this**, because it reports breaker and quota state and makes no upstream call, so a wire name that no
longer exists looks `available` right up until it is asked. Re-check the checked-in model IDs against
each provider's live catalogue whenever a request starts failing with `bad_request` and nothing in the
gateway changed.

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
a third-party provider for extraction — a real privacy trade-off, not a hypothetical one, which is why
the composer discloses it at the point of attachment rather than leaving it as a line buried here.

---

## Tool calls across providers

**Not a formality — a genuinely unsolved problem this project scopes around rather than pretends to
solve.** OpenAI, Gemini, and Groq/OpenRouter's OpenAI-compatible surface each represent a tool call, a
tool result, and — the specific case with no lossless answer — *parallel* tool calls in one turn with
incompatible shapes: different fields for call IDs, different rules for how a result correlates back to
its call, different (or absent) support for more than one call per turn at all. Translating losslessly
between them is not a gap in this gateway's effort; production multi-provider gateways solve the same
problem the same way this one does, by picking one provider per conversation once tool use starts rather
than by inventing a canonical tool-call schema every provider's wire format can round-trip through.

**What the gateway does instead: pin, disclose, never translate.** D3 (`contracts-and-phase1.md` §1) is
locked: the first message in a conversation's history carrying tool-call content pins
`conversations.pinned_model` to whichever model produced it, and every later turn in that conversation
ignores slot selection — `auto` included — until the conversation ends. `routing/selection.py` has
honoured a pin since Phase 2; Phase 5 (D32, [ADR-032](decisions/ADR-032-pinning-without-tool-calls.md))
built the write side — `canonical.pin_target`, `conversations_repo.set_pinned`, and the `warning` field
disclosing *why* a request for a different slot was silently overridden. No later turn is ever asked to
carry tool-call history across a provider boundary, because no later turn is ever routed to a different
provider in the first place.

**The trigger has never fired outside a unit test, and that is by design, not an oversight.** `parse_block`
rejects `tool_call` and `tool_result` at the database boundary — `RESERVED_BLOCK_TYPES` — and
`ChatCompletionRequest` accepts no `tools` field, so no history v1 can store and no request v1 can accept
will ever carry the content `pin_target` looks for. The pin's write path is complete and reachable code
today; only the condition that would trigger it is unbuilt, because D3 and this phase's own scope both say
tool calls are not v1's problem to solve. ADR-032 has the full reasoning for why a complete mechanism with
one deferred trigger was chosen over either leaving the whole feature as a seam or unfreezing the reserved
block types to build tool calls as a side effect of finishing this one.

## Truncation is disclosed, and a truncated answer is never cached

**D4 chose truncation over summarization, and the honest cost of that choice used to be invisible.**
Dropping the oldest non-system messages and inserting a visible omission marker is testable in a way
summarizing them never would be — but before Phase 5 (D34,
[ADR-033](decisions/ADR-033-truncation-disclosed-and-uncached.md)), the only place that cost was recorded
was a log line. A user reading a coherent-sounding answer had no way to know it was built on two thirds of
what they actually said. `messages_dropped` now takes the same three-hop path `extraction_tier` already
took in Phase 4 — the stored `meta`, both response shapes, the `done` event, and the frontend indicator —
so "148 earlier messages omitted" is something the client can read, not just something the gateway once
computed and forgot. `app/memory/summarize.py` stays the unbuilt §2.2.7 seam this disclosure exists in
place of, not instead of solving.

**A truncated answer can no longer poison the cache for a differently-truncated request that looks
identical.** Two byte-identical `temperature: 0` requests under `auto` can resolve to two different
servers with two different context windows — Gemini's 1M tokens versus Groq's 128k — and one can be built
on a whole thread while the other is built on its last twenty turns. `cache/exact.py`'s `request_hash`
folds in the requested slot and the full history, deliberately (ADR-023), and cannot tell those two
requests apart on that basis alone. D35 closes the gap on the write side: `is_cacheable` now refuses to
cache a turn whose `RenderReport.truncated` was true, the same shape `degraded` already had — so a partial
answer cannot be replayed for up to `EXACT_CACHE_TTL_S` to a request that might have gotten the whole
history. The read side gets no equivalent gate — a stored entry is by construction a whole-history answer,
since the write side already refused anything else — and ADR-033 explains why that asymmetry is correct
rather than an inconsistency.

## Bring your own key

**The exact cache is keyed on the request, not on the user — so a private key's answer can be served
to someone else.** `request_hash` folds in the requested slot, the full canonical history and the
generation knobs, and deliberately not `user_id` (ADR-023: a cache scoped per user is a cache that
almost never hits). Under `auto`, two accounts asking the same question with `temperature: 0` produce
the same hash, so an answer a private Gemini key paid for can be replayed to a shared-pool user for
up to `EXACT_CACHE_TTL_S`. The reverse also holds and is the more common case. Nothing leaks — the
credential never enters the cache entry, and `key_pool` is `null` on a hit because no key was spent —
but the *work* is shared, and a user who added their own key partly to keep their questions off a
shared path should know the answer text can outlive their request. ADR-023 weighed this class of
trade already; this is a new instance of it, documented rather than discovered. Turning it off is
`CACHE_EXACT_ENABLED=false`.

**One active key per provider per user, and no rotation.** A partial unique index on
`(owner_id, provider) WHERE owner_type='user' AND is_active` enforces it; replacing a key is
remove-then-add, which the API does in one `POST` (a deactivate-then-insert upsert) but which is
still, semantically, a replacement rather than a second credential. There is no key-rotation UI and
no multi-key fallback: if your key is spent, the chain fails over to a *different provider* on the
shared pool (ADR-037), never to a second key of yours on the same one.

**The shared pool's own credentials are not in the database.** `GROQ_API_KEY`, `GEMINI_API_KEY` and
`OPENROUTER_API_KEY` stay in the environment (ADR-035), so `provider_keys` holds only
`owner_type='user'` rows in v1 even though the column admits `'system'`. A database dump therefore
contains every *user's* key as ciphertext and none of ours in any form.

**`ENCRYPTION_KEY` is a single Fernet key with no rotation path built.** Lose it or rotate it and
every stored user key becomes unreadable: the resolver logs one error per row (`key_id`, never
ciphertext) and falls back to the shared pool, so the gateway keeps answering while quietly ignoring
credentials it can no longer read. Users are not told; their settings page still reads *Using your
key* until something asks the provider. `MultiFernet` is the named seam for fixing this and is not
built. `docs/deploy.md` says the same thing where an operator will actually meet it.

**A key's `validation_status` is a snapshot, not a live fact.** `valid` means the provider accepted
it the last time anyone asked — at add time, or at the user's own re-check. A key revoked upstream
five minutes ago still reads `valid` until a real request fails with it, at which point D40's write
flips it to `invalid` (ADR-037) and the settings row says so. There is no background revalidation:
polling three providers per user per hour to keep a status field warm is not something a free tier
has room for.

**The personal shared-pool cap is per (provider, model), and it is not the gateway rate limit.**
`q:{user_id}:{provider}:{model}:alloc:rpd` fences one user's slice of a shared free tier and fails
*closed* with the rest of quota; `rl:{user_id}:rpd` limits requests to the gateway across all
providers and fails *open* (ADR-036 vs ADR-022). Hitting the cap skips that candidate the way any
exhausted candidate is skipped — the request fails over rather than erroring — so a capped user on a
one-candidate slot sees the same "everything is at its limit" answer an exhausted pool produces, with
no message distinguishing "your cap" from "the pool's". **Phase 7's dashboard did not close this**,
and that is worth saying rather than leaving a forward reference that reads as a promise:
`/v1/admin/quota` delegates to `/v1/models`, whose per-candidate status comes from
`QuotaTracker.remaining`, which reads the provider's own declared windows and knows nothing about the
`alloc:rpd` grant the router appends at reservation time. Surfacing a personal cap would mean teaching
`remaining` about a window that only exists on the write path — a real change to the tracker, not a
dashboard panel.

**What the leak test covers, and what it does not.** `tests/integration/test_credential_leakage.py`
drives a sentinel key through the real app — add, list, a non-streaming turn, a streaming turn, a
live `AuthFailed` failing over to another provider, remove — and asserts the plaintext and its Fernet
ciphertext appear in no JSON log record, no response body, and no stored `requests.attempts` row; a
second test forces an unhandled exception while the key is a live local in the router's frame and
checks the traceback carries neither. That is coverage of *our* log stream and *our* responses. It
says nothing about what a provider logs on their side, about a heap dump, or about a future log call
written after the test was, which is why the sentinel is asserted against captured records rather
than against a list of known-risky call sites.

## Reading the system back (Phase 7)

**The `/metrics` counters are process-local, and they are not a total.** `gateway_requests_total`,
`gateway_request_duration_ms` and `gateway_breaker_fail_open_total` live in one process's memory
([ADR-044](decisions/ADR-044-hand-rolled-metrics-endpoint.md)) and reset on every deploy and every
cold start — which on a free instance that spins down after 15 idle minutes is often. The deployed
service pins `WEB_CONCURRENCY=1` (`render.yaml`, a 0.1-CPU instance), so today a scrape sees the whole
of one process rather than a fraction; raise that number and each scrape becomes a **sample** of
whichever worker answered it. The two gauge families in the same response (`gateway_breaker_state`, `gateway_quota_remaining`)
are read live from Redis and are correct on any worker, so one `/metrics` body legitimately mixes the
two kinds of number. The standard production answer is a shared counter store or a push gateway;
neither is built, because sharing them means new Contract C keys and that is a change made with
sign-off rather than as a side effect of a polish phase — the same trade ADR-014 already made for the
latency table, and the same one decision would unblock both.

**Simulated cost is a fiction, computed now, at list prices.** Nothing on this project is billed. The
dashboard's total is `config/pricing.yaml` applied to token counts at read time
([ADR-041](decisions/ADR-041-simulated-cost-at-read-time.md)), which means editing that file changes
every historical number on the page — deliberately, since the only claim the feature can honestly make
is "what this traffic would cost at *today's* published rates". It is not an invoice and there is no
stored ledger to reconcile against. A model with no entry contributes `None` and is counted separately
as `unpriced_requests`, never folded in as `$0`. The shared-vs-private cost split is one *blended*
rate — this window's total divided by the priced tokens behind it — which is exact only if every
priced request in the window shares one per-token price; input and output are priced differently, so
in general it is an approximation, and the page says so.

**Request volume and provider distribution disagree, on purpose.** Volume counts every `requests` row.
Provider distribution excludes `cache_hit = true` rows (the row names the candidate that *originally*
answered, so counting it reports a call that never went out) and `provider IS NULL` rows (which mean
"never got that far", not a provider called *unknown*) — and a `status='replayed'` idempotent replay is
both. So "1,203 requests" over "980 provider calls" is not a bug in one of the two numbers; it is the
cache and idempotency working, and the difference is the closest thing this dashboard has to a
savings figure. `total = ok + errors + replays` partitions exactly, so a successful retry never
inflates the error rate.

**The dashboard is your own view, not an ops console.** Every route under `/v1/admin/` is scoped to the
calling principal's `user_id` in the SQL itself
([ADR-040](decisions/ADR-040-self-scoped-usage-dashboard.md)). There is no admin identity in this
system — `Principal` is frozen at four fields and `users` has no role column — so there is no
all-users view, no provider-health control, and no editor for `user_quota_allocations` (whose rows are
still written by hand). The quota panel reports the pool *the caller resolves to*, which for a
private-key holder is their own counters and for everyone else is the shared pool's. The system-wide
view is `/metrics`, which is scraped rather than browsed.

**Message pagination applies to the read, not to the render pipeline.** `GET /v1/conversations/{id}`
returns the newest page and a cursor walks backwards from it
([ADR-043](decisions/ADR-043-keyset-pagination-beside-the-full-read.md)), which fixes the payload the
browser downloads. It does not shrink what a *turn* costs: `list_for_conversation` stays unpaginated
and D4's fitting step still reads the complete history on every request, because choosing what to drop
is a decision a page of history cannot make well. A very long thread therefore still costs a full
history read per turn, and the fix for that is summarization (`memory/summarize.py`, still the unbuilt
seam) rather than a paged render.

**Idempotency is best-effort, and deliberately so.** A Redis outage disables it silently and the
request is served as it would have been before the feature existed
([ADR-042](decisions/ADR-042-idempotency-claim-before-routing.md)) — which means a retry during that
outage really can produce a second completion. That is the correct trade (nothing is being *spent* by
proceeding, and refusing to answer because a cache is down is the worse failure), but it is a trade,
not a guarantee. The window is also finite: envelopes expire after 24 hours, so a retry a day later is
a new request. And a stored envelope that a newer build cannot revalidate is served fresh rather than
replayed, because the shape of a response is allowed to change between deploys.

**The chaos demo is an in-process mock, not a network.** `scripts/chaos_demo.py` drives the real ASGI
app over `httpx.ASGITransport` with a scripted `MockTransport` upstream
([ADR-045](decisions/ADR-045-chaos-demo-drives-the-real-app.md)), so there is no TLS, no DNS, no
connection-pool exhaustion and no real timeout — the latencies in the transcript are not latencies.
One worker proves nothing about two, and "zero client-visible failures" is a claim about *that*
schedule rather than a proof that no schedule produces one. `docs/chaos-demo.md` carries the full
list of what the run does and does not demonstrate.

---

## Explicitly out of scope for v1

**No file management UI, no summarization, no audio/video/office formats, no async
extraction.** The perception lane (Phase 4) deliberately stops short of a file browser or a delete
endpoint — a file is referenced by the turn that uploaded it, and `GET /v1/files/{hash}` returns
metadata only. A truncated document keeps its summary through D28's prompt ordering rather than
through a new summarization strategy (`memory/summarize.py` is still the unbuilt seam D4 always
described). Both lanes resolve their credential per candidate since Phase 6, so a
perception-lane reservation runs under whichever scope that candidate resolved to — see the BYOK
section above. The upload allowlist is PDF,
PNG, JPEG and WebP — a format with no tier-3 fallback is a format that fails at 3am rather than
degrading, so nothing is accepted without one. And extraction runs synchronously inside the request
that needs it (D22) rather than on a queue, because a background worker is a second runtime a free
tier does not have room for.

**Still deliberately unbuilt at v1, with the seam visible in each case.** Summarization
(`memory/summarize.py`; the `summary` block type stays reserved and rejected at the JSONB boundary,
and `fitting.FitStrategy` still has one member); semantic caching, which would sit behind
`cache/exact.py::is_cacheable` and would have to respect the same two gates (`degraded`, `truncated`);
p50-over-a-window latency routing, which needs the same cross-instance counter store the `/metrics`
counters do — one decision unlocks both; an operator identity and `allocations_repo.set_cap`
([ADR-040](decisions/ADR-040-self-scoped-usage-dashboard.md)); `MultiFernet` key rotation
([ADR-035](decisions/ADR-035-shared-pool-stays-in-the-environment.md)); `owner_type='system'` rows in
`provider_keys`; `pin_target`'s tool-call branch ([ADR-032](decisions/ADR-032-pinning-without-tool-calls.md));
rollup tables for the dashboard; and `scripts/seed_dev.py`. Each is a named slot rather than a
rewrite, which is the whole point of listing them.

**Not a production system.** Built entirely on free tiers for a portfolio/learning purpose, which means
lower throughput, higher latency variance, and lower consistency than a paid setup would have — stated
plainly here rather than oversold anywhere else in the docs.
