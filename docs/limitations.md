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
a third-party provider for extraction — a real privacy trade-off, not a hypothetical one, and worth a
visible disclosure in the UI once that lane exists rather than a line buried here.

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
no message distinguishing "your cap" from "the pool's". Making that distinction visible is a Phase 7
usage-dashboard question.

**What the leak test covers, and what it does not.** `tests/integration/test_credential_leakage.py`
drives a sentinel key through the real app — add, list, a non-streaming turn, a streaming turn, a
live `AuthFailed` failing over to another provider, remove — and asserts the plaintext and its Fernet
ciphertext appear in no JSON log record, no response body, and no stored `requests.attempts` row; a
second test forces an unhandled exception while the key is a live local in the router's frame and
checks the traceback carries neither. That is coverage of *our* log stream and *our* responses. It
says nothing about what a provider logs on their side, about a heap dump, or about a future log call
written after the test was, which is why the sentinel is asserted against captured records rather
than against a list of known-risky call sites.

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

**Not a production system.** Built entirely on free tiers for a portfolio/learning purpose, which means
lower throughput, higher latency variance, and lower consistency than a paid setup would have — stated
plainly here rather than oversold anywhere else in the docs.
