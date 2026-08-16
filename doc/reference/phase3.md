# Phase 3 — Quota-Aware Routing

Implementation plan. Derived from `development-plan.md` §3 Phase 3, amended by `contracts-and-phase1.md`
§1 (D2, D5, D8) and §2.3 (Contract C), and written against the code Phase 2 actually shipped rather than
against the skeleton it was planned from.

Where the overview and the contracts doc disagree, the contracts doc wins — and it matters twice here.

**The development plan's Phase 3 task 6 says an explicitly-requested exhausted slot returns a structured
`slot_unavailable` error. It does not.** D2 was overridden to "silently fail over, then disclose", ADR-011
turned that into cross-slot spill, and `selection.candidates()` already implements it. Phase 3 does not
add that error, does not add a `suggestion: "auto"` field, and does not teach the endpoint a new failure
mode. What quota knowledge changes about a named slot is *when* the spill happens — before the round trip
instead of after the 429 — not whether it happens.

**And Contract C's degradation rule is the opposite of Phase 2's.** The breaker fails *open* (ADR-010);
quota fails **closed**. That asymmetry is deliberate and it is the sharpest edge in this phase, because it
also invalidates half of ADR-010's readiness reasoning — an instance that will refuse every request for
want of Redis is not, by that ADR's own general rule, ready. §3's D15 settles it.

---

## 1. Scope

**Goal:** the router stops guessing and starts knowing. A candidate that cannot be served is skipped
before the round trip rather than after the 429, `/v1/models` reports live status a client can render,
identical deterministic requests are answered from cache, and the gateway enforces its own limits on its
own users.

**In scope**

- `quota/windows.py` — per-provider reset semantics. Rolling 60s for RPM/TPM; `fixed_daily_utc` and
  `fixed_daily_pt` for the daily windows, which is the whole reason that enum exists.
- `quota/tracker.py` + `quota/scripts/{reserve,commit,release}.lua` — atomic reserve → commit/release.
- `quota/lanes.py` — D8's 50/50 Gemini split, applied to the answer lane before Phase 4 needs it.
- Router integration on **both** paths: reserve before the attempt, commit actual usage after, commit the
  *generated* tokens of a discarded streamed attempt (D1 step 4), release only what was never spent.
- `GET /v1/models` — `api/v1/models.py` + `schemas/models.py`, live status and `resets_at`, no upstream
  call.
- `cache/exact.py` — D5. Non-streaming hits served directly; streaming hits replayed as a synthetic
  stream; `X-Cache` on both.
- Our own per-user rate limiting — sliding window on `rl:{user_id}:{window_start}`, keyed on `user_id`
  and never `api_key_id` (D7).
- `QuotaHint` finally gets a reader: the ground truth every adapter has been publishing since Phase 2 and
  nothing has consumed.
- Frontend: `ModelPicker.tsx` against live status, replacing `hooks.ts`'s `DEFAULT_SLOT` constant.

**Explicitly NOT in Phase 3** — pulling any of these forward is how the phase stops being demoable:

- **No perception lane.** `quota_perception_lane()` is *written by Phase 4*, not by this phase. Phase 3
  reserves the half of Gemini's budget the lane will spend and builds the sub-counter's accounting seam;
  nothing increments it, because `POST /v1/files` does not exist. `NoAttachments` stays the resolver.
- **No idempotency (D6).** The development plan puts it in Phase 7 and the key builder already exists.
  It is a request-level replay concern, not a quota one, and bundling it here would put a second
  Redis-keyed control path into the phase that already has three.
- **No BYOK.** `scope` is threaded through every quota call as a real parameter and is
  `keys.SYSTEM_SCOPE` at every call site. Phase 6 replaces one constant with a resolver call; nothing
  else about the tracker changes, which is the whole reason `scope` is a parameter now rather than a
  Phase 6 refactor.
- **No `pricing.yaml`, no simulated cost, no `/metrics`.** Phase 7 owns the dashboard, and the numbers it
  charts are the ones this phase starts recording.
- **No semantic cache.** Exact-match only (D5, stretch backlog item 3).

**Definition of done:** hammer the `fast` slot until Groq's RPD is spent. `/v1/models` flips it to
`rate_limited` with an accurate `resets_at`; `auto` routes around it *without a wasted round trip* — the
mock transport records zero calls to the exhausted candidate; asking for `fast` explicitly still gets an
answer, from a different model, and the UI says which one. Two identical `temperature: 0` requests: the
second returns `X-Cache: HIT`, writes a `requests` row with `cache_hit=true`, and makes no provider call
at all.

**One small Alembic migration** (`0003`), and both columns are the "add it now anyway" doctrine of §4
Step 2 rather than anything this phase's logic needs:

- `requests.ttft_ms` — nullable int. Phase 2's plan called for it, `StreamCompleted.ttft_ms` and
  `StreamResult.ttft_ms` carry the number end to end, and no column ever landed to hold it. Every
  streamed turn currently measures time-to-first-token and throws it away.
- `requests.quota_scope` — text, not null, default `'system'`. What paid for this request. Constant
  today by construction; the moment Phase 6 exists, every row written before it is unambiguous rather
  than merely assumed.

---

## 2. What Phase 2 left cut, and what Phase 3 does to each seam

| Seam | Where | State today | Phase 3 |
|---|---|---|---|
| `app/quota/` | — | package does not exist | `windows.py`, `tracker.py`, `lanes.py`, `scripts/*.lua` |
| `LUA_SCRIPT_DIR` | `cache/client.py:33` | points at `app/quota/scripts`, which does not exist; `load_dir` logs `no_directory` and returns | Three scripts land in it; `LuaScriptRegistry` needs **no change** to pick them up |
| Quota key builders | `cache/keys.py:140-165` | `quota`, `quota_reservation`, `quota_perception_lane` written, called by nobody | First three callers |
| `UNTIL_PROVIDER_RESET` | `cache/keys.py:57` | `None`, with a docstring naming `quota/windows.py` as the thing that computes it | Computed per provider, per window |
| `ModelSpec.reserved_fraction` | `providers/types.py:67` | `0.0` everywhere; `providers.yaml` says "becomes 0.5 in Phase 3" | `0.5` on both Gemini candidates, read by `lanes.py` |
| `config/limits.yaml` | `config/` | fully populated, read by nobody for enforcement | The tracker's limit source; gains a `gateway` block for our own limits |
| `QuotaHint` / `rate_limit_headers` | every adapter | implemented and tested per provider, unreachable from the router — `complete()` returns a `Completion`, and the `httpx.Response` never escapes | D18's sink gives it a reader |
| `selection.candidates()` | `routing/selection.py:78` | pure, latency-ranked, no quota input | Unchanged. The quota check is the *reservation*, in the loop — see D17 |
| `router.route` / `route_stream` | `routing/router.py` | breaker → render → attempt | breaker → render → **reserve** → attempt → **commit/release** |
| `AttemptRecord.outcome` | `routing/router.py:101` | `ok \| error \| skipped_breaker` | `skipped_quota` joins them, and costs no attempt |
| `_all_skipped` | `routing/router.py:884` | always `Unavailable("every candidate is in circuit-breaker cooldown")` | Also answers for a fleet blocked on quota, as `RateLimited` with the earliest `resets_at` |
| `requests.cache_hit` | `db/models.py:304` | column exists, always `false`; `record_success` takes the parameter | First `true` |
| `GET /v1/models` | — | does not exist; `schemas/models.py` does not exist | Both, mounted in `main.py` |
| `cache/exact.py` | `app/cache/` | does not exist (`keys.exact_cache` does) | D5, both paths |
| Collector's cache seam | `streaming/collector.py:1-32` | docstring: "this is also where D5's cache write lands in Phase 3" | It lands |
| `hooks.ts::DEFAULT_SLOT` | `frontend/lib/hooks.ts:16` | constant, with "a ModelPicker replaces this once `/v1/models` exists" | Replaced |
| `/readyz` Redis clause | `main.py:171` | reports Redis, never fails on it (ADR-010) | Fails on it — see D15 |
| 429 as *our* answer | `core/errors.py` | no `AppError` subclass maps to 429; `_CODE_BY_STATUS` already reserves `rate_limited` for it | `TooManyRequests` |

The lesson carries over from Phase 2: almost nothing here is a new call site. If a step requires changing
a signature Phase 2 shipped, stop and check whether the seam was meant to absorb it. The two exceptions
are deliberate and named — the router's attempt-counter placement (Step 5) and the `AttemptRecord`
outcome literal.

---

## 3. Decisions to settle before writing code

Seven questions the frozen contracts do not answer, continuing Phase 2's numbering (D9–D14 are spent) and
`docs/decisions/`'s (ADR-016 is the last one written). In each case the reasoning, not the verdict, is the
deliverable.

**Status: all seven are decided. The text below is the design, not a proposal.**

### D15 — Quota fails closed. What does that mean, and what does it do to `/readyz`?

Contract C: "fail *closed* on quota (refuse rather than blow through a provider's limit and get the key
banned), fail *open* on caching and your own rate limiting." ADR-010 already took the fail-open half for
the breaker. This is the other half, and it is not symmetric with it.

**Decided: closed at the candidate, which becomes closed at the request.** A `reserve` that cannot reach
Redis returns `allowed=False, degraded=True`, and the router treats it exactly like an exhausted
candidate — a `skipped_quota` trail entry, next candidate. With Redis down every candidate answers the
same way, so the chain empties and the request fails with the normalized `RateLimited` that D17's
`_all_skipped` produces. One code path, no special case, and the shape of the failure tells the truth:
the gateway does not know what it has left, so it is not spending it.

The reasoning that makes this the opposite of the breaker's rule: the breaker's *absence* costs one wasted
round trip, and the normalized error hierarchy still catches what it would have predicted. Quota's absence
costs nothing less than the thing free-tier aggregation is built on — a key that gets banned for
sustained over-limit traffic is not a degraded gateway, it is a gateway with one fewer provider,
permanently, and no amount of failover recovers a revoked credential.

**Consequence, and it is the load-bearing part: `/readyz` now fails on Redis.** ADR-010 states the general
rule — "a readiness probe fails only on dependencies whose absence makes the instance unable to serve, and
fail-open dependencies are by construction not that." Quota is not a fail-open dependency, so the rule
itself flips the answer: an instance that will refuse every chat request genuinely cannot serve, and
leaving it in rotation converts a Redis outage into a fleet of instances that all answer 502 instead of
one that is taken out. ADR-017 supersedes ADR-010's readiness section and keeps its breaker section
intact — the two dependencies were never the same question, and ADR-010 answered it before quota existed
to make that visible.

**The escape hatch, because a self-inflicted total outage deserves one:** `QUOTA_ENFORCEMENT: bool = True`
in `Settings`, printed at boot beside `ROUTING_LATENCY_RANKING`. Off, the tracker is not constructed, no
reservation is made, and the gateway behaves exactly as Phase 2 did — reactive failover on a real 429.
It exists for the same reason D11's flag does: when the thing being debugged is the router, a flag is
cheaper than a revert. `/readyz` reads the same flag, so an instance with enforcement off does not fail
readiness on a dependency it is not using.

### D16 — The window model: fixed counters under a frozen key schema

`limits.yaml` calls RPM/TPM `rolling_60s`. Contract C gives them a key with no window component
(`q:{scope}:{provider}:{model}:rpm`, TTL 60s) — and gives *our own* rate limit a key that has one
(`rl:{user_id}:{window_start}`). That difference is not an accident, and it decides this.

**Decided: fixed windows for provider quota, aligned to the first increment.** `INCRBY`, and set the TTL
**only when the counter is created**. A true rolling window needs either a sorted set of timestamps per
`(provider, model)` or the two-bucket interpolation our own limiter uses — both need a key shape Contract
C does not have, and Contract C is frozen.

The honest cost, which goes in `docs/limitations.md` rather than being quietly absorbed: a fixed window
permits up to 2× the limit across a boundary — 30 requests at 00:59 and 30 more at 01:01 is 60 inside a
62-second span against a limit of 30. Three things make that acceptable here rather than merely tolerated.
The overshoot is bounded and brief; the provider answers a real 429 and the breaker opens, which is the
Phase 2 behaviour we are improving on rather than replacing; and `QUOTA_HEADROOM_FRACTION: float = 0.1`
reserves ten percent of every published limit, so the effective ceiling is under the real one by more than
the jitter costs. A gateway whose entire premise is "run on other people's free tiers" should be
under-spending them on purpose.

**Daily windows expire at the provider's reset instant, not 86,400 seconds from now.** `rpd`/`tpd` get
`EXPIRE` recomputed on *every* increment to "seconds until the next reset", which converges on the real
boundary and is idempotent. `rpm`/`tpm` must **not** do that — see trap 1; a per-minute counter whose TTL
is refreshed on every increment never expires under sustained traffic and produces a permanent false 429
that looks exactly like a provider outage.

**`fixed_daily_pt` needs a timezone database.** `ZoneInfo("America/Los_Angeles")`, never a hardcoded
`-8` offset: Google's day boundary moves with US daylight saving, and a fixed offset is wrong for roughly
eight months of the year — which shows up as an `resets_at` that is an hour off and an `rpd` counter that
resets an hour early or late. Add `tzdata` as a **runtime** dependency: Windows ships no zoneinfo database
at all, and `python:*-slim` images are not guaranteed to carry one either. A `ZoneInfoNotFoundError` at
first use is a 500 in the middle of the phase's most-demoed feature.

### D17 — Where the reservation happens, and what a rejection costs

The development plan says "filter candidates by remaining quota *before* attempting", which reads like a
filtering pass over the chain. There are two places it could live, and only one of them is correct.

**Decided: the reservation *is* the check, made inside the loop, per candidate.** A separate pre-filter
would be a read followed by a write across two round trips, which is precisely the check-then-increment
race Contract C mandates a Lua script to avoid; under any concurrency the filter would pass a candidate
the reservation then has to reject anyway. So the loop asks once, atomically, and the answer is either a
reservation or a named blocking window. `selection.candidates()` stays pure and stays untouched — which
is also what lets `/v1/models` reuse it (Step 7) without standing up a request.

**The order inside the loop is breaker → render → reserve → attempt.** Render comes before reserve
because the reservation needs a token estimate and the only trustworthy one is
`RenderReport.estimated_tokens`, which is `adapter.estimate_tokens` measured on the finished payload.
Rendering a candidate that is then skipped costs local CPU and no round trip, which is the only cost the
"a 429 you predicted is a round trip you didn't spend" claim is about.

**A quota rejection costs no attempt, exactly as a breaker skip does not (D12/ADR-015).** The trail gains
`skipped_quota`, carrying `blocked_window` and `retry_after_s`; `RouterOutcome.attempts` is untouched.
The reasoning is identical to ADR-015's: three exhausted candidates at the head of the chain would
otherwise spend the whole budget without a single request leaving the process, while a healthy provider
sat at position four.

**This moves one line, and the move is deliberate.** `attempts += 1` currently sits at the top of the
inner `while`, before `render`. It moves to after a successful reservation, immediately before
`adapter.complete` / `adapter.stream` — because ADR-015 defines an attempt as "a request that left the
process", and a candidate rejected by its own quota never does. The one behaviour this changes is a
`ContextTooLong` raised by `render` itself (the `budget <= 0` misconfiguration branch), which stops
reporting `attempts: 1` and starts reporting `attempts: 0`. That is the more honest number, it matches
what the all-skipped path already reports, and the re-fit loop stays bounded by `refit_used` rather than
by the attempt cap. Assert it rather than assuming it.

**Commit, release, and the case in between.** Three outcomes, and the middle one is the one that gets
built wrong:

- **Success** → `commit(reservation, tokens_in=actual, tokens_out=actual)`. The token counters are
  adjusted by the *difference* between reserved and actual, in either direction.
- **Failed after the request left the process** → `commit` with the tokens that were really generated —
  zero on the non-streaming path, and the D1 wasted-token estimate for a stream that died mid-sentence.
  **The request counters are not given back.** The provider counted the request; a 429 you provoked is a
  request you made. §1.1 step 4 says exactly this, and dropping it makes the tracker wrong in the one
  scenario it exists for.
- **Never left the process** — a render failure after the reservation, or a candidate abandoned before
  the call — → `release`, which subtracts everything the reservation added.

**A reservation that has expired mid-flight commits nothing and logs it.** `RESERVATION_TTL_S` is 120s
(Contract C) and a long stream can outlive it. `commit.lua` therefore no-ops when the reservation hash is
gone, leaving the estimate counted. Over-counting is the safe direction; the alternative — applying the
delta blind — double-counts a stream that a `release` already refunded.

### D18 — How `QuotaHint` reaches the tracker without touching Contract A

Every adapter implements `rate_limit_headers(response) -> QuotaHint | None`. Every one is unit-tested.
Nothing can call one, because `complete()` returns a `Completion` and the `httpx.Response` dies inside the
adapter. That is not an oversight in the adapters — it is what the frozen protocol shape implies.

**Decided: a contextvar sink in `providers/base.py`, published by `_request`, drained by the router.**
`HttpProviderAdapter._request` already holds the response; after a successful send it calls
`self.rate_limit_headers(response)` and, when the result is non-empty, sets a module-level
`ContextVar[QuotaHint | None]`. The router drains it immediately after the attempt returns and hands it to
`tracker.apply_hint(...)`, which corrects the local counters toward the provider's own number — never
upward past the limit, and never for a window the provider did not mention.

Three properties earn it. It does not touch Contract A: no signature changes, no new protocol method, and
an adapter that never publishes a hint is simply an adapter whose contextvar stays `None`. It is
per-request by construction, which instance state on a shared adapter singleton would not be — the
adapters are built once in the lifespan and serve every concurrent request, so `self._last_hint` is a data
race with a plausible-looking name. And the contextvar pattern is already load-bearing in this codebase
for `request_id`, so it is not a new mechanism to reason about.

**The alternative, and why it is not taken *here*:** widening `Completion` with a `hint: QuotaHint | None`
field is cleaner to read and is a **change to a frozen contract**, which per CLAUDE.md requires asking
first. If that sign-off is given, take it instead — it is strictly better — but it is not something to
slip into a step's diff.

**Hints correct, they never authorize.** A hint that says more budget remains than we counted moves the
counter down; a hint that says less moves it up. But a *reservation* is never granted on the strength of a
hint, because the hint is up to one request stale and Contract C's atomicity guarantee lives in the Lua
script rather than in the header.

### D19 — Cache identity, cache scope, and how a hit discloses itself

D5: cache non-streaming responses directly; for streams, assemble after `done` and replay hits as a
synthetic stream. That leaves four questions.

**What is hashed.** `sha256` over a canonical JSON serialization of: a cache-format version integer, the
**requested slot** (not the served one), the full canonical history — role, `seq` order and content blocks
verbatim — and `temperature`, `max_tokens`, `top_p`, `stop`. The requested slot is in the key because
`auto` and `general` are different questions even when they resolve identically today. The served model is
*not*, because a hit has to be servable whoever would have answered it. The version integer is what lets a
format change invalidate the whole namespace without a key migration.

**When it is skipped.** `temperature > 0` — identical inputs will not reliably reproduce a high-value
output, and a cached creative answer is worse than a fresh one. Any history carrying a `file_ref` (Phase
4's extraction confidence can change underneath a hash that does not cover it). And when the response
would carry `degraded: true`. Read and write use the same predicate, in one function, so they cannot
disagree about what is cacheable.

**Scope: global, deliberately.** Contract C's key is `cache:exact:{sha256}` with no scope segment, and
that is the right call — the *content* is the key, so two users sharing an entry sent byte-identical
inputs. The residual disclosure is "someone else asked this exact thing", which is not recoverable from a
cache hit anyway, and scoping by user would destroy the hit rate that makes the feature worth having.
Worth one sentence in `docs/limitations.md` rather than a design change.

**Disclosure: an `X-Cache` header with three values, on both paths.** `HIT`, `MISS`, and `BYPASS` for a
request that was never eligible. Three rather than two because "why did this not cache?" is the question
you will actually be debugging, and `MISS` alone cannot distinguish "first time" from "temperature 0.7".
No schema change: `ChatCompletionResponse` and the `done` event are §1.1's shape and stay exactly as they
are. The `requests` row carries `cache_hit=true` with `tokens_in`/`tokens_out` of **0** — the row answers
"what did this cost", and a hit costs nothing.

**A hit still writes a message row, and `served_by` names the model that originally produced the text.**
That is the honest answer: some model really did write those words. `meta.attempts` is `0`, meaning no
provider was attempted this turn, which is a value the frontend's `attempts > 1` marker already ignores.

### D20 — Our own rate limiting

Independent of every upstream limit, and the surface the gateway API keys were designed around.

**Decided: two-bucket sliding window, keyed on `user_id`, limits from `limits.yaml`, failing open.**

- **Sliding, not fixed** — this is the one place Contract C gives us a `window_start` segment, so the
  standard interpolation is available: `count = previous_window × (1 − elapsed_fraction) + current_window`.
  It is ~10 lines of arithmetic and it is the same technique the provider-side windows cannot use for want
  of a key shape (D16), which makes the contrast worth writing down.
- **`user_id`, never `api_key_id`** (D7, ADR-007). A user with three integrations is one user with one
  budget. `api_key_id` stays on `requests` for attribution.
- **Limits in `config/limits.yaml`**, under a new top-level `gateway:` block keyed by tier
  (`free`, `plus` — the values `users.tier`'s CHECK constraint already permits), because the risk
  register's standing rule is that every limit in this system lives in YAML and none is hardcoded.
- **Fails open** (Contract C). Redis unreachable → the request proceeds. This is our limit protecting our
  own capacity, not a credential we can get banned; refusing traffic because the counter is unavailable
  trades a real outage for a hypothetical one.
- **429 with `Retry-After`**, through a new `TooManyRequests(AppError)` — `status_code = 429`, `code =
  "rate_limited"`, which is the code `_CODE_BY_STATUS` already reserves for that status. Named
  `TooManyRequests` rather than `RateLimited` precisely because `providers.errors.RateLimited` exists and
  means something else: *their* limit, not ours. Two identically-named classes in one traceback is a
  debugging tax with no upside.
- **Applied to `POST /v1/chat/completions` only.** Reads are cheap and rate-limiting the conversation
  list makes the UI feel broken for no protective gain.

### D21 — `/v1/models` returns OpenAI's envelope, with our fields inside it

The overview §8 sketches a bare JSON array. An SDK pointed at this gateway calls `client.models.list()`
and expects `{"object": "list", "data": [...]}`.

**Decided: the envelope.** `data` entries carry OpenAI's `id` (the slot name), `object: "model"`,
`created` and `owned_by` (the provider of the primary candidate), *plus* `status`, `resets_at`,
`description` and a `candidates` array with per-candidate status, breaker state and remaining budget per
window. An SDK that ignores unknown fields sees a valid model list; our frontend reads the rest.
`schemas/chat.py` made the same trade for the same reason in Phase 1 — OpenAI's shape, plus everything the
gateway knows — and a client contract that is inconsistent about which half it honours is worse than
either choice.

`auto` is in the list, as it is in the overview's sketch, with `status` computed as the best status across
the whole fleet. It has no `owned_by`.

**Status is derived from local state only. `/v1/models` makes no upstream call, ever** — not a health
check, not a cheap `models` listing. Its answer comes from the breaker hash, the quota counters and the
registry, all of which are already in Redis or in memory. A status endpoint that calls three providers
turns a page load into three round trips against the very budgets it is reporting on.

---

## 4. Implementation steps

Ordered so each step is independently committable and three internal milestones are demoable rather than
one at the end. Every step names the files it touches and what has to be true before it is finished; a
step is done when `make lint`, `make typecheck` and `make test` are green and its own "done when" list
holds.

- **Milestone A (Steps 1–5):** the router knows. Predicted exhaustion costs zero round trips.
- **Milestone B (Steps 6–8):** the client can see. Live status, a working picker.
- **Milestone C (Steps 9–11):** caching, our own limits, and ship.

### Step 1 — Config surface, settings, migration *(half a day)*

> **In plain terms.** Paperwork before mechanism. The numbers this phase enforces are already written
> down in `limits.yaml`; what is missing is the handful of switches that decide *whether* to enforce them,
> the timezone database that makes "midnight Pacific" mean anything on a Windows laptop, and two database
> columns for numbers the code already computes and currently throws away.
>
> **After this step.** Nothing behaves differently. But the flags exist, so every later step can be
> switched off in one deploy, and Gemini is declaring the 50/50 split that Step 4 will act on.

**Files:** `pyproject.toml`, `app/config.py`, `.env.example`, `config/limits.yaml`,
`config/providers.yaml`, `alembic/versions/0003_*.py`, `app/db/models.py`, `app/db/repo/requests.py`.

- `pyproject.toml`: add `tzdata>=2025.1` to **runtime** dependencies (D16). Nothing new in dev —
  `fakeredis[lua]` is already there, and the `[lua]` extra is what makes the Lua scripts testable without
  a server.
- `app/config.py::Settings`: `QUOTA_ENFORCEMENT: bool = True`, `QUOTA_HEADROOM_FRACTION: float = 0.1`
  (`ge=0.0, lt=1.0`), `CACHE_EXACT_ENABLED: bool = True`, `RATE_LIMIT_ENABLED: bool = True`. Four
  booleans and a float, each with a docstring saying what turning it off costs — follow
  `ROUTING_LATENCY_RANKING`'s existing docstring as the model. Log all of them in `main.py`'s
  `startup.complete` line.
- `app/config.py`: a `GatewayLimits` model and `LimitsConfig.gateway: dict[str, GatewayLimits]` for D20's
  per-tier limits. `LimitsConfig` is `extra="forbid"`, so the YAML block and the model must land in the
  same commit or startup fails — which is the intended behaviour.
- `config/limits.yaml`: the new `gateway:` block. Start at `free: {rpm: 20, rpd: 500}`,
  `plus: {rpm: 60, rpd: 5000}`, with a comment stating these are *our* limits on *our* users and have
  nothing to do with the provider table above them.
- `config/providers.yaml`: `reserved_fraction: 0.5` on **both** Gemini candidates. The comment already
  there ("becomes 0.5 in Phase 3, when quota/lanes.py is the thing that reads it") becomes a statement of
  fact rather than a promise.
- Migration `0003`: `requests.ttft_ms` (`Integer`, nullable) and `requests.quota_scope` (`Text`, not null,
  `server_default='system'`). Mirror both in `db/models.py` with docstrings, and widen
  `requests_repo.create` with two keyword parameters, both defaulted, so no existing call site changes.

**Done when:** `make migrate` applies cleanly and downgrades; the app boots and the startup log prints
five new fields; `ZoneInfo("America/Los_Angeles")` resolves in a test on the dev machine.

### Step 2 — `quota/windows.py` *(1 day)*

> **In plain terms.** Providers disagree about what "per day" means. Groq's day ends at midnight UTC,
> Google's at midnight in California — which is a different instant in summer than in winter — and "per
> minute" is a sixty-second counter that has to expire on its own. This module is the only place in the
> system that knows any of that. It does no I/O and calls no clock it was not handed, so every one of its
> answers is a test you can write down.
>
> **After this step.** You can ask "when does Gemini's daily budget reset, and how many seconds is that
> from now?" and get an answer that is right in July and in January. Nothing uses it yet.

**Files:** `app/quota/__init__.py`, `app/quota/windows.py`, `tests/unit/test_quota_windows.py`.

```python
PACIFIC: Final = ZoneInfo("America/Los_Angeles")

@dataclass(frozen=True, slots=True)
class WindowSpec:
    window: keys.QuotaWindow          # rpm | rpd | tpm | tpd
    limit: int                        # already reduced by headroom and by D8's lane split
    reset: ResetKind
    cost_is_tokens: bool              # tpm/tpd reserve an estimate; rpm/rpd reserve exactly 1

@dataclass(frozen=True, slots=True)
class WindowState:
    window: keys.QuotaWindow
    limit: int
    used: int
    resets_at: datetime
    @property
    def remaining(self) -> int: ...
    @property
    def exhausted(self) -> bool: ...

def resets_at(reset: ResetKind, *, now: datetime) -> datetime: ...
def ttl_s(reset: ResetKind, *, now: datetime) -> int: ...
def declared(limits: ModelLimits) -> tuple[WindowSpec, ...]: ...
def sliding_count(previous: int, current: int, *, elapsed_fraction: float) -> float: ...
```

- `rolling_60s` → `resets_at` = now + 60s, `ttl_s` = 60. The name is `limits.yaml`'s and stays; D16
  records that the implementation is a fixed window under that name, and the ADR is where the discrepancy
  is explained rather than hidden.
- `fixed_daily_utc` → the next `00:00` UTC strictly after `now`.
- `fixed_daily_pt` → the next local midnight in `PACIFIC`, converted back to UTC. Test it on both sides of
  a DST transition; the assertion that fails on a naive implementation is that the interval between two
  consecutive resets is 23 or 25 hours in the transition weeks, never 24.
- `declared` drops every window whose limit is `None` — `limits.yaml`'s `null` means "the provider
  publishes no such window", which the tracker must **skip**, never read as unlimited and never as zero.
- `sliding_count` is D20's interpolation, here because this module owns window semantics and the rate
  limiter is a window; keeping the arithmetic pure is what makes its boundary behaviour a table test.

**Done when:** the unit suite covers all three reset kinds against a frozen clock, both DST boundaries,
and a `limits.yaml`-shaped `ModelLimits` with two `null` windows.

### Step 3 — `quota/tracker.py` and the Lua scripts *(2.5 days — the hardest step in the phase)*

> **In plain terms.** The heart of it. Before calling a provider you write down "I am about to spend one
> request and roughly 900 tokens"; afterwards you correct that to what was really spent, or hand it back
> if nothing was. The reason this is not three lines is concurrency: if two requests both *read* the
> counter, both see room, and both then *write*, you have spent twice what you checked for. So the check
> and the increment have to be one indivisible operation on the server — which is what a Lua script is,
> and the interview answer to "why not just use a pipeline?".
>
> **After this step.** A component you can ask "may I spend this?" that answers yes-with-a-receipt or
> no-with-a-reason, and that fifty simultaneous callers cannot talk into overspending. Nothing calls it.

**Files:** `app/quota/tracker.py`, `app/quota/scripts/{reserve,commit,release}.lua`,
`app/deps.py`, `tests/unit/test_quota_tracker.py`.

```python
@dataclass(frozen=True, slots=True)
class Reservation:
    scope: keys.Scope
    provider: str
    model: str
    request_id: str
    tokens: int
    windows: tuple[keys.QuotaWindow, ...]

@dataclass(frozen=True, slots=True)
class QuotaDecision:
    allowed: bool
    reservation: Reservation | None = None
    blocked_window: keys.QuotaWindow | None = None
    retry_after_s: float | None = None
    resets_at: datetime | None = None
    degraded: bool = False        # decided without Redis; allowed is False (D15)

class QuotaTracker:
    def __init__(self, redis, scripts: LuaScriptRegistry, limits: LimitsConfig, *,
                 clock: Clock = SYSTEM_CLOCK, headroom: float = 0.0) -> None: ...
    async def reserve(self, spec: ModelSpec, *, scope, estimated_tokens: int,
                      request_id: str) -> QuotaDecision: ...
    async def commit(self, reservation: Reservation, *, tokens_in: int, tokens_out: int) -> None: ...
    async def release(self, reservation: Reservation) -> None: ...
    async def remaining(self, spec: ModelSpec, *, scope) -> tuple[WindowState, ...]: ...
    async def apply_hint(self, spec: ModelSpec, *, scope, hint: QuotaHint) -> None: ...
```

**`reserve.lua`.** Variable window count, so the calling convention has to carry it:

```
KEYS[1]            reservation hash
KEYS[2..n+1]       counter keys, in order
ARGV[1]            n
ARGV[2..]          per window, four values: name, limit, cost, ttl
ARGV[last-1]       reservation TTL
ARGV[last]         request_id
-> {1}                       reserved
-> {0, window, ttl_remaining} blocked, naming the window and when it frees
```

Every window is checked before any is incremented — a script that increments as it goes and then bails on
window three leaves two counters permanently overstated. TTLs: `rpm`/`tpm` set the TTL **only when the
counter was created** (the `INCRBY` return equals the cost); `rpd`/`tpd` always `EXPIRE`, because their
TTL converges on a real instant. The reservation hash records what was added per window so `commit` and
`release` do not have to trust their caller.

**`commit.lua`** takes the actual token count, adjusts every token window by `actual − reserved` (clamped
at zero, in Lua, so a Redis restart mid-flight cannot produce a negative counter), leaves the request
windows alone, and deletes the reservation. **A missing reservation hash is a no-op plus a returned
sentinel** the tracker logs as `quota.reservation_expired` (D17).

**`release.lua`** subtracts everything the reservation recorded, request windows included, and deletes it.
It is called only when nothing left the process.

**The tracker is where fail-closed lives.** Every Redis exception inside `reserve` produces
`QuotaDecision(allowed=False, degraded=True)` and one `logger.warning("quota.fail_closed", ...)`;
`commit`/`release` swallow-and-log like the breaker's writes do, because there is nothing left to refuse
by then. `remaining` returns an empty tuple and lets `/v1/models` report `unknown`.

**Headroom and lanes are applied to the limit before the script sees it**, in `_windows_for(spec)`:
`floor(published × (1 − headroom) × lanes.answer_share(spec))`. The script does no policy; it does
arithmetic on numbers it is handed. That is what keeps it testable and what keeps D8 out of Lua.

`app/deps.py` gains `get_quota(request) -> QuotaTracker | None` — built per request from `RedisDep`,
`app.state.lua_scripts`, `get_limits_config()` and `Settings`, returning `None` when
`QUOTA_ENFORCEMENT` is off — plus `QuotaDep`. Same construction reasoning as `get_breaker`: the tracker
holds no state, the Redis keys are the state.

**Done when:** `fakeredis` tests cover a blocked window naming itself, TTL set once for `rpm` and
refreshed for `rpd`, commit adjusting in both directions, release restoring exactly, an expired
reservation committing nothing — and **50 concurrent `reserve` calls against a limit of 10 grant exactly
10**, which is the test the whole Lua design exists to pass.

### Step 4 — `quota/lanes.py` (D8) *(half a day)*

> **In plain terms.** Gemini is the only model in the fleet that can read a PDF, and it is also a
> perfectly good chat model. If plain chat is allowed to spend its whole daily budget, the one genuinely
> differentiated feature in the product stops working every afternoon. So half of Gemini's day is fenced
> off for file understanding before anything is allowed to touch it.
>
> **After this step.** Gemini's answer lane sees half the budget `limits.yaml` publishes, and the other
> half is unspendable by chat. Phase 4 arrives to a reservation that already exists rather than to an
> argument about who gets it.

**Files:** `app/quota/lanes.py`, `tests/unit/test_quota_lanes.py`.

```python
ANSWER: Final = "answer"
PERCEPTION: Final = "perception"

def answer_share(spec: ModelSpec) -> float:      # 1.0 - spec.reserved_fraction
def perception_budget(spec: ModelSpec, limits: ModelLimits) -> dict[QuotaWindow, int]: ...
async def reserve_perception(...) -> QuotaDecision:
    raise NotImplementedError("the perception lane lands in Phase 4")
```

The seam is a **typed signature raising `NotImplementedError`**, per the hard rule — never a silently
passing stub. It is what Phase 4's lane calls, and its return type is the contract.

The direction of the split is worth a test of its own, because getting it backwards is invisible: chat
must see `1 − reserved_fraction`, and a `reserved_fraction` of 0.5 must halve the *answer* budget rather
than the perception one.

### Step 5 — Wire the tracker into both router paths *(1.5 days)*

> **In plain terms.** Now the router stops guessing. Before each attempt it asks whether there is budget;
> if there is not, it moves to the next candidate without sending anything, and that skip does not count
> against the three tries a message gets. Afterwards it corrects the estimate to what was really spent —
> including the awkward case where a stream died halfway through, having generated tokens the provider
> charged for and nobody will ever read.
>
> **After this step.** **Milestone A.** Point a mock transport at an exhausted candidate and assert it was
> never called. The gateway now routes around a limit it predicted rather than one it discovered.

**Files:** `app/routing/router.py`, `app/api/v1/chat.py`, `app/streaming/orchestrator.py`,
`tests/unit/test_router.py`.

- `route` and `route_stream` gain `quota: QuotaTracker | None = None` and
  `scope: keys.Scope = keys.SYSTEM_SCOPE`. `None` disables reservation entirely — the same shape as
  `metrics=None`, and what keeps every existing unit test meaningful without rewriting it.
- The loop body becomes: breaker → `render` → `quota.reserve(...)` → **`attempts += 1`** → attempt →
  `commit`/`release`. D17 has the reasoning for each edge of that ordering.
- `AttemptOutcome` gains `skipped_quota`; `AttemptRecord` gains `blocked_window: str | None`.
  `to_json()` emits it only when set, exactly as it already does for `retry_after_s` — the trail's shape
  is read by Phase 7's dashboard, so additive-and-optional is the constraint.
- `_all_skipped` generalizes: if any skip was a quota block it returns `RateLimited` with
  `retry_after_s` = the smallest of the blocked candidates' resets, so the client's 502 carries a
  `Retry-After` that means something. If every skip was the breaker it returns `Unavailable`, unchanged.
- Streaming: the reservation is per attempt. On `AttemptAborted`, commit with
  `tokens_out=wasted_tokens_out` — those tokens were really generated (§1.1 step 4). On
  `StreamCompleted`, commit the reported usage. The `wasted_tokens_out` estimate is
  `router.DISCARDED_CHARS_PER_TOKEN` and is already computed; this step gives it its first real consumer.
- `chat.py` passes `QuotaDep` into both calls. `scope` is `keys.SYSTEM_SCOPE`, written as an explicit
  argument with a comment naming Phase 6's `resolve_provider_key` as its replacement — the same pattern
  `registry.system_key` already uses.

**Done when:** a scripted registry where candidate 1 is quota-exhausted serves from candidate 2 with
`handler.calls == 1`; the trail holds a `skipped_quota` entry; `outcome.attempts == 1`; a chain of three
exhausted candidates still reaches candidate four; and a mid-stream abort commits non-zero tokens against
the failed candidate's counter.

### Step 6 — `QuotaHint` reconciliation *(1 day)*

> **In plain terms.** Groq and OpenRouter tell you, on every response, how much of your allowance is
> left. Our own counter is an approximation built from what we think we sent; theirs is the truth. This
> step lets the truth correct the approximation, which matters because our counter drifts — a request we
> made from another environment, a token estimate that was 20% low, a Redis restart.
>
> **After this step.** The counters self-correct toward ground truth wherever a provider publishes it,
> and Gemini — which publishes nothing — carries on on our own numbers, which is exactly what
> `QuotaHint`'s "opportunistic, never required" docstring promised.

**Files:** `app/providers/base.py`, `app/routing/router.py`, `app/quota/tracker.py`,
`tests/unit/test_provider_base.py`, `tests/unit/test_quota_tracker.py`.

- `providers/base.py`: a module-level `ContextVar[QuotaHint | None]` plus `publish_hint()` / `take_hint()`
  (the second clears as it reads, so a hint cannot be attributed to the following attempt). `_request`
  publishes on every response it receives, success or error — a 429's headers are the most informative
  ones the system ever sees.
- `router.py`: after each attempt, success or failure, `hint = base.take_hint()` and, when present,
  `await quota.apply_hint(spec, scope=scope, hint=hint)`. Failing to apply a hint is never fatal.
- `tracker.apply_hint`: `used = limit − remaining`, written with `SET` **only when it increases** the
  counter, and never above the limit. A hint that would lower the counter is logged and dropped —
  the header is at best one request stale, and letting it *grant* budget reintroduces the race that
  Contract C's Lua script exists to close (D18).

**Done when:** a recorded Groq success fixture carrying `x-ratelimit-remaining-requests` moves the local
counter; a hint arriving during a concurrent reservation cannot lower it; a Gemini response publishes
nothing and the counters are untouched.

### Step 7 — `GET /v1/models` *(1 day)*

> **In plain terms.** One endpoint that answers "what can I ask for right now, and if something is
> unavailable, when does it come back?" Everything it needs is already known locally — which model is
> configured, which breaker is open, how much budget is left — so it answers without calling anybody.
>
> **After this step.** A client can grey out an exhausted slot instead of letting a user pick something
> that will visibly fail.

**Files:** `app/schemas/models.py`, `app/api/v1/models.py`, `app/main.py`,
`tests/integration/test_models_endpoint.py`.

- Status per candidate: breaker `open`/`half_open` → `unavailable` (with the breaker's `retry_after_s`);
  any exhausted window → `rate_limited` (with the earliest exhausted window's `resets_at`); Redis
  unreachable → `unknown`; otherwise `available`.
- Status per slot: the best of its candidates — a slot with one healthy candidate *is* available, since
  that is precisely what the failover chain will do.
- `auto`: the best status across the whole fleet, built from `selection.candidates(registry, "auto")` so
  the endpoint reports on exactly the list the router would walk. That reuse is what
  `selection.py`'s purity was for.
- Authenticated (`PrincipalDep`) — Phase 6 personalizes this response per user (§9.7), and an endpoint
  that starts anonymous and becomes authenticated is a breaking change on a phase boundary.
- Response per D21. `resets_at` is an ISO-8601 UTC timestamp, not a duration: a client that renders "in
  4 minutes" can compute it, and an absolute instant does not rot in a cached response.

**Done when:** an integration test with one breaker forced open and one counter forced to its limit
returns the three statuses and an accurate `resets_at`, and the mock transport records **zero** upstream
calls.

### Step 8 — Frontend: the model picker *(1.5 days)*

> **In plain terms.** The browser half of the previous step. A dropdown listing the slots, with the
> unavailable ones greyed out and labelled with when they return, and whatever the user picks rides on the
> next message. Plus the one honest touch that costs nothing: when a limit is hit, say so in the words of
> the thing that hit it.
>
> **After this step.** **Milestone B.** You can exhaust a provider in one window and watch the picker in
> another window go grey, then come back on its own.

**Files:** `frontend/lib/types.ts`, `frontend/lib/api.ts`, `frontend/lib/hooks.ts`,
`frontend/components/ModelPicker.tsx`, `frontend/components/Composer.tsx`,
`frontend/tests/ModelPicker.test.tsx`.

- `types.ts` gains `SlotStatus`, `ModelEntry`, `ModelsResponse`, mirroring `schemas/models.py`
  field-for-field — that file is the one place the frontend restates the contract and it says so.
- `api.ts` gains `fetchModels()`; `hooks.ts` gains `useModels()` — fetched on mount and **refetched after
  every completed turn**, because a status that only refreshes on page load is a status that is wrong
  exactly when it matters.
- `ModelPicker.tsx` is a `frontend/components/` file named in §3's tree. Disabled options for
  `rate_limited`/`unavailable`, with a relative "resets in ~4 min" built off `lib/format.ts`; `unknown`
  renders as selectable-but-unlabelled, since our not knowing is not a reason to stop the user.
- `hooks.ts::DEFAULT_SLOT` is replaced by the picker's value, defaulting to `auto`. Persisting it to
  `conversations.preferred_slot` is optional and small: widen the existing `PATCH /v1/conversations/{id}`
  schema and its repo function. Skip it if the step is running long — the per-request `model` field is
  what actually routes.
- `api.ts` learns the `rate_limited` error code (D20's 429) and renders it as a wait, not a failure, with
  the `Retry-After` value.

### Step 9 — Exact-match cache (D5) *(2 days)*

> **In plain terms.** If someone asks the identical question twice with the randomness turned off,
> answering it twice is a waste of a budget that is measured in hundreds of requests per day. So the
> answer is kept for an hour under a fingerprint of the question. The genuinely interesting half is
> streaming: a cached answer has to arrive as a stream of pieces too, or the client sees a completely
> different-looking response for what is, to a user, the same thing.
>
> **After this step.** Repeating a deterministic question is free and visibly instant, and `X-Cache`
> tells you why.

**Files:** `app/cache/exact.py`, `app/api/v1/chat.py`, `app/streaming/collector.py`,
`app/streaming/orchestrator.py`, `tests/unit/test_exact_cache.py`,
`tests/integration/test_chat_cache.py`.

- `exact.py`: `request_hash(...)`, `is_cacheable(...)`, and an `ExactCache` over Redis with `get`/`put`,
  both failing open (a Redis error is a `MISS` on read and a shrug on write) per Contract C. The cached
  value is a small JSON object — text, provider, model, slot, finish reason, the original usage,
  `created_at` — not a serialized `ChatCompletionResponse`, which carries a `request_id` and a
  `message_id` that are wrong for the replay.
- Non-streaming: the lookup sits in `chat.py` **after** the user's message is committed and **before**
  the router call, so a hit skips routing, the breaker and quota entirely. A hit still writes the
  assistant message and a `requests` row (`cache_hit=true`, tokens 0, `status='ok'`).
- Streaming: a hit is replayed by a small synthetic generator emitting `meta` → `delta`* → `done` through
  the same `streaming/sse.py` framing. **No artificial delay** — a fake typing effect is a lie about
  where the answer came from, and `X-Cache: HIT` is the honest signal. Chunk on whitespace boundaries at
  roughly 24 characters so the deltas look like deltas.
- The write for a streamed answer lands in `collector.py`, after `done`, exactly where its docstring said
  it would ("this is also where D5's cache write lands in Phase 3"). Never write a `failed` stream's
  partial.
- `X-Cache: HIT|MISS|BYPASS` on every response from the endpoint, both paths.

**Done when:** two identical `temperature: 0` requests produce one upstream call and two message rows; a
`temperature: 0.7` request bypasses in both directions; the synthetic stream's event sequence is
byte-comparable in shape to a real one (same events, same field names); a Redis outage degrades to
`MISS` without an error.

### Step 10 — Our own rate limiting *(1 day)*

> **In plain terms.** Everything so far protects the providers from us. This protects us from our own
> users: a per-user cap on how fast requests can be sent, enforced by our own counter, so one enthusiastic
> script cannot drain a shared free tier that everybody is using.
>
> **After this step.** The gateway has a limit of its own, expressed in the same envelope as every other
> failure, with a `Retry-After` a client can obey.

**Files:** `app/core/errors.py`, `app/deps.py`, `app/api/v1/chat.py`,
`tests/integration/test_rate_limit.py`.

- `core/errors.py`: `TooManyRequests(AppError)` — `status_code = 429`, `code = "rate_limited"`, a message
  written for a human, `Retry-After` passed through `headers`.
- `app/deps.py`: `RateLimitDep`, a dependency that reads the principal's `tier`, looks the limits up in
  `LimitsConfig.gateway`, counts with `windows.sliding_count` over `keys.rate_limit(user_id, window_start)`
  and `keys.rate_limit(user_id, previous_window_start)`, and raises `TooManyRequests` when over. TTL is
  `window × RATE_LIMIT_TTL_MULTIPLIER`, which is the constant `keys.py` already declares and explains.
- Fails open on any Redis error, and logs it at warning (D20). Applied to the chat endpoint only.

**Done when:** the tier limit is enforced, the window slides rather than resetting in a cliff, two API
keys belonging to one user share one budget (ADR-007's rule, and the test that proves it), and a Redis
outage lets traffic through.

### Step 11 — Tests, ADRs, docs, deploy *(2 days)*

> **In plain terms.** Prove it, write down why it was built this way, and ship it. The ADRs are the
> portfolio artifact: this phase contains the single best interview answer in the project — why the
> reservation is a Lua script and not a pipeline — and it is worth writing down properly while the
> reasoning is fresh.
>
> **After this step.** **Milestone C.** Green CI with zero live provider calls, seven decision records,
> and quota enforcement running against the real Upstash instance on the deployed URL.

Covered by §6 and §7 below. Deployment specifics: the Upstash free tier has a command-per-day ceiling, and
this phase multiplies the commands per request by roughly four — check the quota dashboard after a day of
real use and note the headroom in `docs/limitations.md`. Also confirm on the deployed instance that
`/readyz` now goes red when Redis is unreachable (D15) and that Fly's health check does what you expect
with that, because discovering it during a demo is how a working phase looks broken.

---

## 5. Traps

Collected from the contracts, the code's own warnings, and the shape of the work:

1. **`EXPIRE` on every increment of a per-minute counter.** The counter never expires under sustained
   traffic, so `rpm` climbs forever and the gateway serves permanent false 429s that look exactly like a
   provider outage. Set `rpm`/`tpm` TTLs **only on creation**; `rpd`/`tpd` refresh every time because
   theirs converges on a real instant.
2. **Check-then-increment.** Two round trips overshoot under concurrency, and the overshoot is invisible
   until a key rate-limits earlier than predicted. This is the entire reason Contract C mandates a single
   Lua script, and the reason the 50-concurrent-reserves test exists.
3. **Incrementing as you check.** A script that increments window 1, checks window 2 and bails leaves
   window 1 permanently overstated. Check every window, then increment every window.
4. **A hardcoded `-8` for Pacific.** Wrong for eight months a year. `ZoneInfo`, and `tzdata` as a runtime
   dependency so it resolves on Windows and in a slim container.
5. **Reserving after the call.** Post-hoc counting undercounts under exactly the concurrency it needs to
   handle. Say this out loud in the README — the development plan asks for it, and it is a better answer
   than it looks.
6. **Refunding a request that really happened.** A 429 you provoked is a request the provider counted.
   Release is for attempts that never left the process; everything else commits.
7. **Losing a discarded stream's tokens.** Same trap Phase 2 flagged, now with teeth: those tokens were
   generated and charged, and Phase 3 is the phase where dropping them makes a live counter wrong.
8. **A quota rejection consuming an attempt.** Three exhausted candidates would spend the whole budget of
   three with zero requests made. Same rule as a breaker skip (ADR-015), and the same test.
9. **Failing open on quota because it feels safer.** It is not. Contract C's asymmetry exists because a
   banned key does not come back, and the readiness probe has to move with the decision (D15) or the
   fleet quietly serves 502s while reporting itself healthy.
10. **Ranking before filtering.** D11's latency sort must run on the list the quota filter has already
    thinned, not before it — that ordering is the fix for ADR-014's standing caveat that ranking by speed
    leans toward whichever provider is nearest its ceiling. Here it falls out for free, because the
    reservation happens inside the loop and the sort happens when the chain is built, but a future
    pre-filter must be inserted on the correct side of it.
11. **`/v1/models` calling a provider.** Turns a page load into three round trips against the very
    budgets it reports on. Every number it returns is already local.
12. **Caching a non-deterministic or degraded response**, or caching the *failed* half of a stream. Read
    and write must share one `is_cacheable` predicate, or they will disagree and the disagreement will be
    a cache that never hits.
13. **A cache hit that skips persistence.** The user sees an answer, refreshes, and the turn is gone.
14. **A reservation expiring under a long stream.** `RESERVATION_TTL_S` is 120s and a generation can
    outlive it; `commit` must no-op on a missing reservation rather than applying its delta blind.
15. **Splitting D8 the wrong way.** `reserved_fraction: 0.5` must halve the *answer* lane. Backwards, it
    reserves half the budget for chat and starves the feature the reservation exists to protect — and
    nothing fails, it just quietly stops working in Phase 4.
16. **Forgetting that Gemini's quota is per Google Cloud project.** Two environments sharing a project
    share the real budget while keeping separate Redis counters, so both believe they have half of what
    they actually have. `limits.yaml` already says this; the tracker cannot know it.

---

## 6. Test matrix

| Layer | Approach |
|---|---|
| `quota/windows.py` | Pure table tests on a frozen clock. Three reset kinds; both DST boundaries (consecutive `fixed_daily_pt` resets are 23h and 25h apart in transition weeks); `null` windows dropped, never read as unlimited; `sliding_count` at 0%, 50% and 100% elapsed. |
| `quota/tracker.py` | `fakeredis[lua]`. Blocked window names itself with a usable `retry_after_s`; `rpm` TTL set once and not refreshed; `rpd` TTL refreshed every increment; commit adjusts up and down; release restores exactly; expired reservation commits nothing; **50 concurrent reserves against a limit of 10 grant exactly 10**; every Redis exception yields `allowed=False, degraded=True` (D15). |
| `quota/lanes.py` | `reserved_fraction=0.5` halves the *answer* budget; `0.0` leaves it whole; the perception seam raises `NotImplementedError` rather than returning something plausible. |
| Router | Exhausted candidate 1 → served by candidate 2 with **zero** upstream calls to candidate 1; `skipped_quota` in the trail with `blocked_window`; skips do not consume attempts (three exhausted candidates still reach candidate four); commit on success carries actual usage; commit on failure keeps the request counters; release only on a pre-call abandonment; a render-raised `ContextTooLong` now reports `attempts: 0` (D17). |
| Streaming | A mid-stream abort commits `wasted_tokens_out` against the failed candidate; the restarted attempt makes its own reservation; a stream outliving `RESERVATION_TTL_S` logs `quota.reservation_expired` and does not double-count. |
| `QuotaHint` | A recorded Groq 200 with rate-limit headers moves the counter toward ground truth; a 429's headers do too; a hint that would *lower* the counter is dropped; Gemini publishes none and nothing moves; the contextvar is cleared by `take_hint` so a hint cannot leak into the next attempt. |
| `/v1/models` | Status matrix as an integration test — breaker open, window exhausted, Redis down, healthy — each producing its own status and `resets_at`; `auto` reflects the best of the fleet; zero upstream calls; unauthenticated → 401 in the standard envelope. |
| Exact cache | Hash stability as a golden test (a committed hash for a fixed history — any change to the hashed inputs must be deliberate); `temperature > 0` bypasses read *and* write; a hit makes no provider call and still writes both rows; the synthetic stream emits the same event sequence as a real one; Redis down degrades to `MISS`. |
| Our rate limit | Enforced per tier; the window slides rather than cliff-resetting; two API keys of one user share one budget; Redis down fails open; the 429 carries `Retry-After` and the standard envelope. |
| Frontend | `ModelPicker` disables `rate_limited` entries and renders `resets_at` relatively; `useModels` refetches after a turn; a 429 renders as a wait rather than an error. |

Coverage stays concentrated in `quota/`, `routing/` and `cache/exact.py` — where a bug is both likely and
invisible until a counter has been wrong for a day.

---

## 7. Documentation

- **ADR-017** Quota fails closed (D15): the asymmetry with ADR-010's breaker rule, why a banned key is
  unrecoverable in a way a wasted round trip is not, and the readiness consequence that supersedes
  ADR-009/ADR-010's readiness section.
- **ADR-018** The window model (D16): fixed windows under a frozen key schema, the 2× boundary overshoot
  and the headroom fraction that absorbs it, why our own limiter gets a sliding window and the provider
  counters do not, and the DST reasoning behind `fixed_daily_pt`.
- **ADR-019** Reservation placement (D17): why the reservation is the filter, why a quota skip is not an
  attempt, the commit/release/commit-anyway trichotomy, and the attempt-counter move.
- **ADR-020** `QuotaHint` transport (D18): the contextvar sink, why instance state on a shared adapter is
  a race, why hints correct but never authorize, and the `Completion`-widening alternative that was not
  taken because Contract A is frozen.
- **ADR-021** Exact-cache identity and scope (D19): what is hashed and what deliberately is not, why the
  cache is global rather than per-user, `X-Cache`'s three values, and why a hit's `served_by` names the
  original model.
- **ADR-022** Our own rate limiting (D20): sliding vs. fixed, `user_id` over `api_key_id`, fail-open, and
  why the limits live in YAML.
- **ADR-023** `/v1/models` shape (D21): OpenAI's envelope plus our fields, and the rule that the endpoint
  makes no upstream call.
- `docs/architecture.md`: the reserve → commit/release lifecycle as a diagram, and the request flow with
  the quota check in place.
- `docs/limitations.md`: the fixed-window boundary overshoot; the cache's global scope; Gemini's
  per-project budget shared across environments; Upstash's command ceiling; and the fact that a Redis
  outage now takes an instance out of rotation by design.
- `README.md`: the "why Lua and not a pipeline" paragraph. It is the single best interview answer this
  project produces and it belongs where someone will read it.

---

## 8. Exit checklist

- [ ] Hammer one slot until exhausted → `/v1/models` flips it to `rate_limited` with an accurate
      `resets_at`, and `auto` routes around it
- [ ] The exhausted candidate receives **zero** requests while it is exhausted — assert it on the mock
      transport, don't infer it from the logs
- [ ] Requesting the exhausted slot explicitly is still answered, by a different model, and the UI
      discloses the substitution (D2 — *not* the development plan's `slot_unavailable` error)
- [ ] 50 concurrent requests against a limit of 10 produce exactly 10 upstream calls
- [ ] Two identical `temperature: 0` requests: the second is `X-Cache: HIT`, makes no provider call, and
      writes a `requests` row with `cache_hit=true`
- [ ] A cached streamed answer replays as a real-looking stream, and the turn survives a refresh
- [ ] A mid-stream restart commits the discarded attempt's tokens — the counter moves, and
      `wasted_tokens_out` in the `requests` row agrees with it
- [ ] Gemini's answer lane sees half its published daily budget; the other half is unspendable by chat
- [ ] Exceeding your own tier limit returns 429 with `Retry-After` in the standard envelope; two API keys
      of one user share one budget
- [ ] Kill Redis: every chat request fails closed with a `rate_limited` code, `/readyz` goes red, and the
      log says why once per attempt rather than silently
- [ ] Set `QUOTA_ENFORCEMENT=false` with Redis still dead: the gateway serves normally, Phase 2-style
- [ ] `requests.ttft_ms` is populated on every streamed turn
- [ ] `make test` green, zero live API calls; `make lint` and `make typecheck` clean
- [ ] ADR-017…023 written

**Realistic duration:** 13–16 working days, or ~3 weeks part-time. The development plan's estimate was
~1.5 weeks; the Lua scripts and their concurrency tests, the reset-semantics work that `fixed_daily_pt`
forces, the streaming half of both the reservation lifecycle and the cache, and the `QuotaHint` seam that
Phase 2 left unreachable are where the difference goes. Steps 3 and 9 are the two that will overrun.

---

## 9. What Phase 3 hands to Phase 4

Left deliberately unbuilt, with the seam visible:

- `quota/lanes.py::reserve_perception` is a typed signature raising `NotImplementedError`, and the
  budget it will spend is already fenced off — Phase 4's lane inherits a reservation rather than an
  argument.
- `keys.quota_perception_lane`, `keys.extraction` and `keys.extraction_lock` are written and still have no
  writer.
- `render()`'s step 1 still returns `NoAttachments`, and `ResolvedAttachment` still has no token cost —
  `memory/render.py:170` names that gap, and it is what makes native multimodal input unmeasurable
  against quota today. Phase 4 has to give an attachment a token cost *and* teach `is_cacheable` about it
  (D19 skips any history with a `file_ref` precisely so this stays a Phase 4 decision).
- `MessageMeta.extraction_tier` and `RenderReport.degraded` are plumbed end to end, through the SSE
  `done` event and into the frontend indicator, and nothing sets either to anything but the default.
- Idempotency (D6) is still unbuilt; `keys.idempotency` exists and Phase 7 owns it.
- `config/pricing.yaml` still does not exist. Phase 7's simulated cost reads the `requests` rows this
  phase started populating correctly — `tokens_in`/`tokens_out` reconciled against real usage,
  `wasted_tokens_out` committed rather than estimated-and-dropped, and `ttft_ms` finally stored.
- `scope` is threaded through every quota call and is `keys.SYSTEM_SCOPE` at every call site. Phase 6
  replaces one constant with `resolve_provider_key(user_id, provider)` and the tracker does not change —
  which is the payoff for making it a parameter one phase early rather than a refactor one phase late.
