# Phase 7 — Polish & Portfolio

Implementation plan. Derived from `development-plan.md` §3 Phase 7, read against the code Phases 1–6
actually shipped rather than against the skeleton it was planned from, and against `project-overview.md`
§4.8 (usage/simulated cost), §11 (`/metrics` as a stretch), §13 (the interview answers this phase exists
to make demonstrable) and §6's `user_quota_allocations`.

**Read this first, because it changes where the work is.** Phase 7 reads like a documentation sprint with
a dashboard bolted on. It is not. Three of its seven tasks are genuine engineering with sharp edges, and
one of them — idempotency — is the only remaining locked decision (**D6**) that has never had a line of
behaviour written for it:

- **Idempotency (task 5) is unbuilt but not unplanned.** `cache/keys.py::idempotency` and
  `IDEMPOTENCY_TTL_S` have existed since Phase 3 (`app/cache/keys.py:110`, `:250`) and are the only key
  builders in Contract C that nothing calls. CLAUDE.md's Phase 7 blurb says "`keys.idempotency` still
  unwritten"; that is now inaccurate — **the key builder exists, the behaviour does not**. What is missing
  is the store, the claim-before-routing discipline, and the interaction with the two things Phase 3 and
  Phase 5 put in its way: the exact cache and the streaming path.
- **The dashboard (task 1) has no admin role to hang off.** `Principal` is frozen at four fields
  (`user_id`, `auth_method`, `api_key_id`, `tier`) and `users` has no `is_admin`. `api/admin.py` is a
  designated but empty slot. This is a decision, not an oversight to fix by widening a frozen contract —
  see **D44**.
- **`/metrics` (task 3) has a reader waiting for it.** `usage/metrics.py`'s module docstring already says
  the breaker's fail-open counter is deliberately absent "until Phase 7's `/metrics` endpoint gives it a
  reader". That module is the designated slot and the precedent for how process-local numbers are
  justified here (ADR-014).
- **Pagination (task 6) is a cursor for free.** `messages(conversation_id, seq)` is indexed and `seq` is
  gap-free and monotonic by Contract B invariant 2. The work is not the query; it is the frontend state
  and the four ways it can collide with an optimistic turn.
- **Phase 6 handed this phase its most interesting column.** `requests.quota_scope` is no longer a
  constant, and every `attempts` entry carries `key_pool`. "How much of the shared free tier is one user
  consuming" and "did this turn get billed to the user's own provider account" are now queries.

The two tasks that really are writing — the README build-out (task 2) and `docs/limitations.md`
finalized (task 7) — are the phase's *deliverable*, not its filler. `development-plan.md` says it
outright: "This phase is where the interview value gets realized. Don't skip it for more features."

---

## 1. Scope

**Goal:** someone who has never seen this repo can open the README, understand the architecture in three
minutes, click through to the reasoning behind every major decision, watch a recording of the gateway
degrading gracefully while providers are killed underneath it, and — if they have a login — open a page
that shows where their requests went, what they would have cost, and how much of each free tier is left.
And a client that retries a request it already made gets the answer it already got, once.

**In scope**

- **`config/pricing.yaml` + `app/usage/pricing.py`** — §4.8's simulated cost, computed at read time from
  a checked-in price table. The fourth config file and the fourth `lru_cache`d loader in `config.py`.
- **Aggregation reads on `requests`** — new functions in `db/repo/requests.py` beside `create` and
  `list_for_user`, doing their work in SQL: volume over time, provider distribution, error rate, cache
  hit rate, and the shared-vs-private split Phase 6's `quota_scope` made possible.
- **`app/api/admin.py`** — the designated slot, filled with self-scoped usage endpoints plus a live
  quota-utilization read that reuses Phase 6's `CredentialsDep`. Mounted in `main.py`.
- **`GET /metrics`** — Prometheus text exposition, hand-rolled, with process-local counters in
  `usage/metrics.py` beside `LatencyTable`, and live gauges read from Redis at scrape time.
- **Idempotency (D6)** — `Idempotency-Key` on `POST /v1/chat/completions`, claim-before-routing in Redis
  under the key builder that has been waiting since Phase 3, replay on both the streaming and
  non-streaming paths, and a new `requests.status` value for a replay.
- **Keyset message pagination** — a **second** repo function beside an untouched
  `list_for_conversation`, a `GET /v1/conversations/{id}/messages` page route, and `useConversation`
  turned into paginated state with a scroll-up loader.
- **`scripts/chaos_demo.py`** — concurrent load against the real app with providers dying underneath it,
  in-process and deterministic, changing nothing in `app/`.
- **The dashboard page** in the existing Next.js app, with hand-rolled inline-SVG charts.
- **README build-out** — architecture diagram, request-flow walkthrough, the failure-mode table, and a
  "Design Decisions" index linking all 45 ADRs.
- **ADR-040 … ADR-045**, `docs/limitations.md` finalized, `docs/architecture.md`'s Phase 7 section,
  `docs/deploy.md`'s new variables, `.env.example`, `CLAUDE.md`.

**Explicitly NOT in Phase 7**

- **No admin role, no `users.is_admin`, no `Principal` change.** Contract §1.2 stays frozen. The
  dashboard is self-scoped — see D44.
- **No write surface for `user_quota_allocations`.** Phase 6's handoff note names Phase 7's admin surface
  as where `set_cap` would land; D44 declines it. A user raising their own cap is not administration, it
  is a bug, and an operator-facing surface needs an operator identity this project does not have. The
  seam stays: `get_cap`/`list_for_user` exist and `set_cap` would mirror them.
- **No new runtime dependency.** No `prometheus_client` (the exposition format is twenty lines and the
  hand-rolled-breaker precedent applies), no chart library in the frontend (D51).
- **No rollup tables, no materialized views, no background aggregation job.** The dashboard queries
  `requests` live. At portfolio scale the existing `ix_requests_user_id_created_at` is the whole
  optimization, and a rollup job is a second runtime a free tier does not have room for — the same
  reasoning D22 used to refuse a background extraction worker.
- **No semantic cache, no summarization, no tool calls, no latency-based routing beyond D11's existing
  EWMA.** Stretch backlog, `development-plan.md` §7.
- **No change to any frozen contract signature.** Contract A untouched. Contract B untouched — pagination
  reads `seq`, it does not add a field. **Contract C gains no new key builder**: `idem:{user_id}:{key}`
  was frozen in §2.3 and built in Phase 3; what changes is the *value* stored at it, which the key schema
  never specified. Say so in the ADR rather than leaving a reader to wonder.
- **`list_for_conversation` is not touched.** Stated three times in `development-plan.md` task 6 and once
  more here, because it is the single most likely mistake in this phase.

**Definition of done — one session, demoed live:**

1. `GET /metrics` on the running service returns Prometheus text: request counters by provider and
   status, a latency histogram, a breaker-state gauge, and a quota-remaining gauge — scraped once,
   readable by eye.
2. Send the same `POST /v1/chat/completions` twice with the same `Idempotency-Key`. The second returns
   the identical body, `X-Idempotent-Replay: true`, makes no provider call, and moves no quota counter.
   Send a third with the same key but a different body: 409, not a wrong answer.
3. Do it again with `stream: true`. The replay arrives as a synthetic SSE stream framed identically to
   the live one — the same machinery D5 built for cache hits.
4. Open a conversation with 300 messages. The page loads the newest 50, scrolls up smoothly, and fetches
   older pages without ever re-fetching the whole thread. Send a message: it appends correctly, and the
   older pages already loaded are still there.
5. Open the usage page. Request volume over the last 24 hours with no missing buckets, provider
   distribution that does not count cache hits as provider calls, error rate, cache hit rate, simulated
   cost split into "shared pool" and "your own key", and per-provider quota utilization that agrees with
   `/v1/models`.
6. Run `python -m scripts.chaos_demo`. Watch the table: providers get killed, the breaker opens, latency
   moves, failure counts stay at zero on the client side, and the degradation is visible per-provider.
7. Open the README cold. Architecture diagram, request-flow walkthrough, failure-mode table, and 45 ADRs
   linked by the decision they record.

---

## 2. What Phases 1–6 left, and what Phase 7 does to each seam

| Seam | Where | State today | Phase 7 |
|---|---|---|---|
| `keys.idempotency`, `IDEMPOTENCY_TTL_S` | `cache/keys.py:110`, `:250` | Built, called by nothing | The store, the claim, the replay (Steps 5–6) |
| `api/admin.py` | designated in §3, absent on disk | Does not exist | Created, self-scoped (Step 3) |
| `requests` aggregate reads | `db/repo/requests.py` | `create` + `list_for_user`; the docstring says outright "Phase 7's usage dashboard aggregates rather than lists" | Four aggregate functions (Step 2) |
| `requests.quota_scope` | `db/models.py:312` | Real values since Phase 6 Step 7 | The shared-vs-private axis of the dashboard (Steps 2–3) |
| `requests.attempts[].key_pool` | Phase 6 Step 5 | Written per attempt | Read by the failover panel; must tolerate pre-Phase-6 rows (Step 2) |
| Breaker fail-open counter | `usage/metrics.py` docstring | Deliberately absent, naming this phase | A counter with a reader (Step 4) |
| `CircuitBreaker.peek` | `routing/circuit_breaker.py:284` | Read-only, built for `/v1/models` | Second reader: the breaker-state gauge (Step 4) |
| `QuotaTracker.remaining` | `quota/tracker.py:443` | Read-only, built for `/v1/models` | Second reader: the quota gauge and the dashboard (Steps 3–4) |
| `config/pricing.yaml` | named in §3's tree | Absent | Created, loaded, validated, priced (Step 1) |
| `messages_repo.list_for_conversation` | `db/repo/messages.py:158` | Unpaginated, docstring says why | **Untouched.** A second function beside it (Step 7) |
| `allocations_repo.get_cap`/`list_for_user` | `db/repo/allocations.py` | Read-only | Still read-only. `set_cap` stays a named seam (D44) |
| `scripts/` | `record_fixtures.py` only | `chaos_demo.py`, `seed_dev.py` named in §3, absent | `chaos_demo.py` (Step 10). `seed_dev.py` stays absent |
| `README.md` | 161 lines, four "why" essays | No diagram, no ADR index, no failure table | Built out (Step 11) |
| `docs/limitations.md` | 346 lines, 9 sections | Accurate through Phase 6 | Finalized (Step 12) |

---

## 3. Decisions to settle before writing code

Seven decisions, D44–D50, plus D51 for the frontend. Each becomes an ADR in Step 12 unless noted.

### D44 — The dashboard is self-scoped, and there is no admin identity

**The problem.** `development-plan.md` calls it a "usage dashboard" against `/admin/*`, and
`project-overview.md` §14 calls it "a minimal admin dashboard". But `Principal` is frozen at four fields
and `users` has no role column. Building an admin surface means either widening a frozen contract or
inventing a second authorization axis (an env allowlist of emails, a separate admin token) — one week
before the project ships.

**The decision.** **Every route in `api/admin.py` is scoped to the calling principal's own `user_id`,
inside the SQL, exactly like `conversations` and `files`.** No role, no allowlist, no `is_admin`. The
file keeps its designated name because §3's tree named it and renaming a slot is churn; its docstring
says in its first paragraph that "admin" here means "this account's own operational view", not
"everyone's".

Two panels are not per-user data and are handled explicitly rather than fudged:

- **Quota utilization** is a property of a *pool*, and which pool the caller draws from is exactly what
  Phase 6's resolver answers. The endpoint reuses `CredentialsDep`, resolves each candidate the way
  `/v1/models` does, and reports `remaining()` under the caller's own resolved scope — a shared-pool user
  sees the shared pool's remainder (which `/v1/models` already shows them), a private-key user sees their
  own. No new disclosure.
- **Breaker state** is global and already visible through `/v1/models`' per-candidate status. It is not
  duplicated into the dashboard; the dashboard links to what `/v1/models` reports.

**Consequences.** The demo is "here is *your* usage", which is a truthful product feature rather than a
fake ops console. `user_quota_allocations` gets no write surface this phase (there is no operator to use
it). If this project ever grows a real operator identity, the aggregate functions in Step 2 take a
`user_id` argument that would become `user_id | None`, and that is the whole change — say so in the ADR.

### D45 — Aggregation happens in SQL, and the buckets are generated, not discovered

**The decision.** `db/repo/requests.py` grows aggregate functions that `GROUP BY` in Postgres and return
frozen dataclasses. Nothing loads rows into Python to count them.

Three windows — `1h`, `24h`, `7d` — with bucket widths of 1 minute, 1 hour and 6 hours respectively, so
every window renders as roughly 30–60 points. Buckets are produced by `generate_series` **left-joined**
against the aggregate, not by `date_trunc` alone: an hour in which nothing happened must render as a zero
bar, and a `date_trunc` `GROUP BY` simply omits it, which draws a chart where a quiet night looks like a
busy one. Everything is UTC in the API; the client renders local time.

**Semantics that are easy to get wrong and are therefore specified here:**

| Metric | Rule |
|---|---|
| Request volume | every row, `status` included — a failure is a request |
| Provider distribution | `WHERE provider IS NOT NULL AND cache_hit = false`. A `NULL` provider means "never got that far" (the repo docstring says so); a cache hit names the provider that *originally* answered and calling it a provider call double-counts |
| Error rate | `count(*) FILTER (WHERE status <> 'ok') / count(*)`, over every row |
| Cache hit rate | `count(*) FILTER (WHERE cache_hit) / count(*)` |
| Failover rate | `count(*) FILTER (WHERE substituted)` and `jsonb_array_length(attempts) > 1` — two different questions, both interesting |
| Simulated cost | D46 |
| Pool split | `quota_scope = 'system'` vs everything else. Never compare against the caller's own id in SQL — a row written before Phase 6 Step 7 is `'system'` and that is correct |

### D46 — Cost is a fiction computed at read time, and an unpriced model is not free

**The decision.** `config/pricing.yaml` (version, then `pricing: {provider: {model: {input_per_mtok,
output_per_mtok, currency}}}`) is loaded by a fourth `lru_cache`d `get_pricing_config()` in `config.py`,
validated by `validate_startup_config()` like the other three. `app/usage/pricing.py` holds one pure
function, `simulated_cost(provider, model, tokens_in, tokens_out) -> Decimal | None`.

**No cost column is added to `requests`.** Prices change; a stored number freezes a fiction at the moment
it was written and then quietly disagrees with the table it came from. Computing at read time means the
number is always "what this traffic would cost at today's published rates", which is the only claim the
feature can honestly make. Say this in the README — it is a better answer than the number itself.

**A model with no price entry contributes `None`, not `0`.** The aggregate returns
`(total_cost, unpriced_requests)`, and the dashboard renders "≈ $0.42 across 1,203 requests (18 unpriced)"
rather than silently understating. A missing entry is a **warning at boot**, not a `ConfigError`: a
fictional price table is not a correctness dependency of serving traffic, and killing the process because
someone added a model to `providers.yaml` before adding it to `pricing.yaml` would be disproportionate.
Every other config failure in this project is fatal; this one exception is worth one sentence of
justification in the loader's docstring.

### D47 — Idempotency: claim before routing, envelope in Redis, fail open

**The decision.** Optional `Idempotency-Key` header on `POST /v1/chat/completions` (nowhere else — a
`GET` is already idempotent and `DELETE /v1/conversations/{id}` is idempotent by construction).

The value at `idem:{user_id}:{idem_key}` is a small JSON envelope, not a bare `request_id`:

```
{"state": "in_flight" | "done",
 "fingerprint": "<sha256 of the canonical request>",
 "request_id": "<the gateway request id that claimed it>",
 "response": {...} | null,          # the ChatCompletionResponse body, on "done"
 "stream": true | false}
```

`request_id` alone — which is literally what §2.3 and D6 wrote down — cannot reconstruct a body, and
"replays return the stored response" is the behaviour D6 asks for. **This is not a Contract C amendment**:
§2.3 froze the key *format*, and the format is unchanged. The ADR says so explicitly, because the
last two phases both amended Contract C with sign-off and a reader should be able to tell the difference
between "amended again" and "did not need to be".

**The order is the whole design.** The claim is a `SET key <in_flight envelope> NX EX 86400` issued
**before** the cache read, before quota, before routing — otherwise two concurrent identical retries both
reach a provider and D6 has bought nothing.

| Claim outcome | Behaviour |
|---|---|
| `NX` succeeded | This request owns the key. Proceed. On success, overwrite with the `done` envelope. On failure, **delete the key** — a failed request must be retryable, and a 24h lock on a key whose request 502'd is the worst possible outcome |
| Existing, `done`, fingerprint matches | Replay: return the stored body, `X-Idempotent-Replay: true`, no provider call, no quota, no new message rows |
| Existing, `in_flight`, fingerprint matches | `409 idempotency_in_flight` with `Retry-After: 1`. `Conflict` already exists in `core/errors.py` |
| Existing, fingerprint differs | `409 idempotency_key_reuse` — the Stripe semantics. Silently answering a different question under a reused key is the failure mode this check exists to prevent |

**Redis down → fail open**: proceed without idempotency, log once, serve the request. Same rule as
caching and D20's rate limiting, opposite of D15's quota rule, and for the same reason — nothing is being
*spent* by proceeding, and refusing to answer because a cache is down is a worse failure than answering
twice.

**Streaming.** The claim happens on the shared path before the `body.stream` branch. The `done` envelope
is written by the collector after the stream completes (it is already the component that assembles the
full text for the exact cache — D5), and a replay is served through the existing
`stream_cached_completion` machinery. A stream that fails deletes the claim, same as above.

**A replay writes a `requests` row with `status = "replayed"`, `provider`/`model` NULL, `cache_hit`
false.** The `status` column is deliberately unconstrained precisely so phases can add values (its
docstring says so). NULL provider is correct and already means "never got that far", which keeps the
replay out of provider distribution and out of cost while keeping request volume honest — the client
really did make two requests.

### D48 — Pagination is a second route, not a second mode of the detail route

**The decision.** `GET /v1/conversations/{id}` keeps its URL and its shape, but returns the **newest**
page of messages (default 50, oldest-first within the page) plus two new fields, `has_more: bool` and
`next_before_seq: int | None`. Older pages come from a **new route**,
`GET /v1/conversations/{id}/messages?before_seq=&limit=`.

Two routes rather than query parameters on one, because they are two resources with two cache lifetimes:
the detail route is "this thread as it stands now" and is the thing an optimistic turn mutates and SWR
revalidates; a page of old messages is immutable history and must never be written into the same SWR key
(D51's trap). One URL with a `before_seq` parameter makes the head page and page 4 the same cache entry
under different arguments, which is exactly how an optimistic append ends up prepended to page 4.

The repo function is new and named `list_page_for_conversation`, keyset on `seq`
(`WHERE seq < :before_seq ORDER BY seq DESC LIMIT :n+1`), ownership-scoped by the same join
`list_for_conversation` uses, returning `(messages_oldest_first, has_more, next_before_seq)`. It fetches
`limit + 1` rows to answer `has_more` without a second `COUNT`. **`list_for_conversation` is not
modified, not deprecated, and not called by the paginated route** — D4's fitting step needs the complete
history and that need is independent of the UI.

### D49 — `/metrics` is hand-rolled, process-local where it must be, and honest about it

**The decision.** `GET /metrics` returns `text/plain; version=0.0.4` built by hand in
`app/usage/metrics.py`. No `prometheus_client`: four metric families and an exposition format that is
twenty lines of string building do not justify a runtime dependency, and the hand-rolled circuit breaker
already set this precedent for the same reason (`project-overview.md` §11 calls it "a better interview
story than importing one").

Four families, and where each number comes from:

| Metric | Type | Source | Labels |
|---|---|---|---|
| `gateway_requests_total` | counter | process-local, incremented in `usage/logger.py`'s facade functions | `provider`, `model`, `status`, `key_pool` |
| `gateway_request_duration_ms` | histogram | process-local, fixed buckets | `provider`, `mode` (`complete`/`stream`) |
| `gateway_breaker_state` | gauge | **live**, `CircuitBreaker.peek` per candidate at scrape time | `provider`, `model` |
| `gateway_quota_remaining` | gauge | **live**, `QuotaTracker.remaining` under `SYSTEM_SCOPE` at scrape time | `provider`, `model`, `window` |

Counters are **process-local**, like `LatencyTable` and for the same reason (ADR-014): sharing them would
need new Contract C keys, and that is a change with sign-off, not a side effect of a polish phase.
Render runs two workers, so a scrape hits one of them and the counters are a *sample*, not a total. That
is a real limitation, it goes in `docs/limitations.md` with the standard production answer (a shared
store or a push gateway), and it is *not* papered over by pretending the numbers are complete.
*(Corrected in Step 12: the deployed `render.yaml` pins `WEB_CONCURRENCY=1`, so a scrape reads one whole
process today rather than a fraction. The caveat survives — the counters still reset on every deploy and
cold start, and a second worker is one value away — but the docs say what is true rather than what this
plan assumed. See ADR-044.)* The gauges
are live from Redis and therefore correct on any worker.

**Labels never carry a `user_id`, an email, a conversation id, or free text.** Unbounded label
cardinality is how a metrics endpoint takes down the thing scraping it, and the two identifiers a
gateway is tempted to label with are also the two that make the endpoint a privacy surface.

**Access.** New `Settings.METRICS_ENABLED: bool = True` and `METRICS_TOKEN: SecretStr | None = None`.
With a token set, the endpoint requires `Authorization: Bearer <token>`; with none set it is open, which
is fine locally and is called out in `docs/deploy.md` as a thing to set on Render, where there is no
private network. Disabled returns 404, not 403 — an endpoint that is off should not advertise itself.
Redis unreachable → the gauges are simply omitted from the output and the counters still render; a
metrics endpoint that 500s during an incident is a metrics endpoint that is useless exactly when needed.

### D50 — The chaos demo drives the real app in-process and changes nothing in `app/`

**The problem.** "Randomly kill providers" against a deployed service means either real credentials being
revoked mid-run, or a chaos toggle endpoint in production code. The first is not reproducible and the
second is a permanent hole punched in the app for the sake of one recording.

**The decision.** `scripts/chaos_demo.py` builds the **real** application via `create_app()` and drives
it over `httpx.ASGITransport`, with the upstream side served by a scripted `httpx.MockTransport` whose
per-provider behaviour the script flips over time (healthy → 429 → 503 → recovered), reusing the recorded
fixtures the test suite already commits. Redis is whatever `REDIS_URL` points at (fakeredis is acceptable
and documented); Postgres is the compose instance.

**Nothing in `app/` changes for this step.** That constraint is the point: if the demo needs a hook, the
demo is wrong. Output is a live-updating terminal table — per-provider state, in-flight requests, served
counts, breaker transitions, p50 — plus a summary asserting the headline claim: *N concurrent requests,
M providers killed, zero client-visible failures, K disclosed substitutions*. A `--json` flag writes the
run to a file so `docs/chaos-demo.md` can quote real numbers rather than invented ones.

### D51 — The dashboard is a page in the existing app, with hand-rolled charts

**The decision.** A `/usage` route in the existing Next.js app, behind the same Supabase session as
everything else, using the existing design system (`components/ui/`), SWR, and `lib/api.ts`. Not
Streamlit: a second app means a second deploy, a second auth story and a second visual language, for a
page that is fundamentally four charts over one JSON document.

**No chart library.** Four visualizations — a time-series area/bar, a horizontal distribution bar, a pair
of rate meters, and a per-provider utilization bar — are inline SVG built from an array of numbers. A
charting dependency costs hundreds of kilobytes, renders through a canvas or a `ResizeObserver` that
jsdom does not implement (so the tests would assert on mocks rather than on output), and buys nothing
this page needs. `components/charts/{Sparkline,BarRow,Meter}.tsx`, each a pure function of props, each
testable by asserting on the SVG it produces.

---

## 4. Implementation steps

Twelve steps, three milestones. Each is a commit, each leaves the whole suite green, and each names the
files it is allowed to touch. Every step also names the **model** to run it under.

**How to read the model column.** Sonnet where the step is mechanical, single-shaped, and has a strong
in-repo precedent to copy line for line — the failure mode is a typo, and the tests catch it. Opus where
the step has to hold several files in mind at once, where the plausible-but-wrong answer is the likely
one (SQL aggregate semantics, async state races, the streaming/idempotency interaction), or where the
prose *is* the deliverable. When in doubt on a step marked Sonnet, the escalation trigger is: if the
first attempt needs a second design pass, restart it under Opus rather than patching.

**Milestone A — the numbers exist** (Steps 1–4). Cost, aggregation, the admin endpoints, `/metrics`.
Nothing in the request path changes; every one of these is a read.

**Milestone B — the honest edges** (Steps 5–8). Idempotency and pagination. These are the only steps that
touch `api/v1/chat.py` and the only ones that can break a working gateway.

**Milestone C — the artifact** (Steps 9–12). The dashboard page, the chaos demo, the README, the docs.

Before starting: `make test`, `make lint`, `make typecheck`, `make frontend-test`, `make frontend-lint`
all green on `main`. If any is red, that is the first commit and it is not part of this phase.

---

### Step 1 — `config/pricing.yaml` and simulated cost *(0.5 day)* — **Sonnet**

Touches `config/pricing.yaml` (new), `app/config.py`, `app/usage/pricing.py` (new), and tests.

1. **`config/pricing.yaml`** — `version: 1`, then `pricing: {provider: {model: {input_per_mtok,
   output_per_mtok, currency}}}` for every candidate `config/providers.yaml` declares, including `pro`'s
   `gemini-3.6-pro` and the `perception` slot's models. Prices are dollars per million tokens as
   `Decimal`-safe strings, with a header comment stating loudly that **these are published list prices
   for models this project consumes for free**, and that the resulting number is a simulation whose only
   purpose is to answer "what would this traffic cost if it were paid".
2. **`app/config.py`** — `PricingEntry`/`PricingConfig` pydantic models in the same style as
   `ModelLimits`/`LimitsConfig` (`extra="forbid"`, `frozen=True`), a `for_model(provider, model)` lookup
   mirroring `LimitsConfig.for_model`, an `@lru_cache(maxsize=1) get_pricing_config()`, and a call from
   `validate_startup_config()`. Add `_warn_unpriced_models()`: cross-reference every enabled
   `providers.yaml` candidate against the table and log one `config.unpriced_models` warning naming them.
   **A missing entry warns; malformed YAML still fails.** The docstring must say why this one config is
   not fatal (D46) — it is the only exception in the file and an unexplained exception invites someone to
   "fix" it into a `ConfigError`.
3. **`app/usage/pricing.py`** — one pure function, `simulated_cost(provider, model, *, tokens_in,
   tokens_out) -> Decimal | None`. `Decimal`, not `float`: this number gets summed thousands of times and
   rendered as currency. `None` for an unpriced model, never `Decimal(0)`.

**Tests** (`tests/unit/test_pricing.py`, new; `tests/unit/test_config.py`): a known pair of token counts
prices correctly; an unknown model returns `None`; zero tokens is `Decimal("0")` and not `None` (a priced
model that used nothing cost nothing — a different fact from an unpriced one); the committed YAML loads,
covers every enabled candidate, and the unpriced warning fires for a synthetic gap.

**Done when:** `get_pricing_config()` loads at boot, every committed candidate is priced, and nothing
outside `usage/pricing.py` and `config.py` imports the table.

---

### Step 2 — Aggregate reads on `requests` *(1.5 days)* — **Opus**

Touches `app/db/repo/requests.py` and `tests/integration/test_repo_requests.py`. **No new tables, no
migration, no index** — `ix_requests_user_id_created_at` already matches every predicate here.

Add, beside `create` and `list_for_user`, with frozen dataclasses for the return types (defined in this
module, since they are its vocabulary):

1. `volume_series(session, *, user_id, window: Literal["1h","24h","7d"], now)` → tuple of
   `VolumePoint(bucket_start, total, errors, cache_hits)`. **`generate_series` left-joined against the
   aggregate** so empty buckets are present with zeros (D45). Bucket widths: 1 minute / 1 hour / 6 hours.
2. `provider_distribution(session, *, user_id, since)` → tuple of `ProviderSlice(provider, model,
   requests, tokens_in, tokens_out)`, `WHERE provider IS NOT NULL AND cache_hit = false`.
3. `outcome_summary(session, *, user_id, since)` → `OutcomeSummary(total, ok, errors, cache_hits,
   replays, substituted, multi_attempt, tokens_in, tokens_out, wasted_tokens_out)`. One query with
   `FILTER` clauses, not six round trips.
4. `pool_split(session, *, user_id, since)` → `PoolSplit(shared_requests, shared_tokens_in,
   shared_tokens_out, private_requests, private_tokens_in, private_tokens_out)`, keyed on
   `quota_scope = 'system'` versus everything else (D45's last row — never compare to the caller's own
   id).

Every function is ownership-scoped in the SQL (`WHERE user_id = :uid`), takes `now`/`since` from the
caller rather than calling the clock (the codebase's clock discipline; it is also what makes these
testable), and returns UTC.

Costing is **not** done here. `provider_distribution` returns token counts; `usage/pricing.py` turns them
into money in Step 3. A repo module that reads a YAML price table is a repo module with a config
dependency, and the next person to need cost in a different unit has to change the SQL.

**Tests** (`tests/integration/test_repo_requests.py`): seed rows at known timestamps through
`requests_repo.create` and assert — an empty bucket in the middle of the series is present and zero; a
`provider IS NULL` failure row counts in volume and errors but is absent from the distribution; a
`cache_hit` row counts in volume and cache hits but is absent from the distribution; a `status='replayed'`
row counts in volume and in `replays` and nowhere else; a second user's rows never appear; the pool split
puts a pre-Phase-6 `'system'` row on the shared side; `multi_attempt` reads `jsonb_array_length(attempts)`
and tolerates a row whose attempt objects have no `key_pool` key at all (pre-Phase-6 shape).

**Done when:** four functions, four dataclasses, every one of them ownership-scoped, and no SQL has
appeared anywhere in `app/api/`.

---

### Step 3 — `app/api/admin.py` and its schemas *(1 day)* — **Sonnet**

Touches `app/api/admin.py` (new), `app/schemas/admin.py` (new), `app/main.py`, and
`tests/integration/test_admin_endpoints.py` (new).

Router prefix `/v1/admin`, tags `["admin"]`, `AUTHENTICATED_ERROR_RESPONSES`, and a module docstring whose
**first paragraph** states D44: "admin" means this account's own operational view; every read is scoped
to the calling principal and there is no operator role in this system.

Three routes:

- `GET /v1/admin/usage?window=24h` → `UsageOverview`: the `OutcomeSummary`, the `VolumePoint` series, the
  `ProviderSlice` list each annotated with `simulated_cost` from Step 1, the `PoolSplit` with a cost per
  side, plus `total_cost`, `currency` and `unpriced_requests`. One handler, four repo calls, no logic
  beyond the costing map.
- `GET /v1/admin/quota` → per-candidate `remaining()` under the caller's own resolved scope via
  `CredentialsDep` (imported from `api/v1/chat.py`, the way `api/v1/models.py` already imports it — no
  cycle, `chat.py` imports neither module). Shape mirrors `/v1/models`' status block deliberately, so the
  two pages cannot disagree.
- `GET /v1/admin/requests?limit=` → the existing `list_for_user`, mapped to a wire model. The "show me my
  last few calls" table under the charts; the repo docstring already says this function exists for
  exactly this and is not the dashboard's aggregate query.

Mount in `main.py` beside the others. `Depends` shapes copy `api/v1/models.py`.

**Tests:** each route 401s unauthenticated; each returns only the caller's rows with two users seeded;
`window` validates; an unpriced model surfaces in `unpriced_requests` and not in `total_cost`; the quota
route reports the private scope for a key holder and the shared scope for everyone else (one assertion
that reuses `test_models_endpoint.py`'s two-account fixture shape).

**Done when:** three routes, all self-scoped, `make test` green, and no route in the module reads a row
it did not scope by `principal.user_id`.

---

### Step 4 — `GET /metrics` *(1.5 days)* — **Opus**

Touches `app/usage/metrics.py`, `app/usage/logger.py`, `app/config.py`, `app/main.py`,
`tests/unit/test_metrics.py`, `tests/integration/test_metrics_endpoint.py` (new), `.env.example`.

1. **`usage/metrics.py`** gains a `MetricsRegistry` beside `LatencyTable` — process-local counters and a
   fixed-bucket histogram, no locks (same reasoning the `LatencyTable` docstring already gives: an
   increment is cheaper than the lock protecting it, and a lost increment delays a number by one
   request). One instance on `app.state`, created in the lifespan beside `LatencyTable`. Also the
   breaker's **fail-open counter** that this module's docstring has been promising since Phase 2.
2. **`usage/logger.py`** — each facade function increments the matching counter. They are already the one
   place every terminal outcome passes through, which is why the counter goes here and not in the router;
   a counter incremented at three call sites is a counter that will be wrong within two phases. The
   registry arrives as an optional argument defaulting to `None` (no-op) so every existing test call site
   keeps working unchanged — the same `None`-keeps-callers-honest shape D36 established.
3. **`config.py`** — `METRICS_ENABLED: bool = True`, `METRICS_TOKEN: SecretStr | None = None`.
4. **`main.py`** — `@app.get("/metrics")` beside the health probes, returning
   `PlainTextResponse(media_type="text/plain; version=0.0.4")`. Disabled → 404. Token set and absent or
   wrong → 401. Gauges built from `CircuitBreaker.peek` and `QuotaTracker.remaining` over the registry's
   candidates under `SYSTEM_SCOPE`; **any Redis failure omits the gauges and still returns 200 with the
   counters** (D49).

Exposition format, exactly: `# HELP`, `# TYPE`, then samples; labels escaped; histogram emitted as
`_bucket{le="..."}` plus `_sum` and `_count`, cumulative and ending in `le="+Inf"`. A hand-rolled
exporter that emits a non-cumulative histogram parses fine and charts wrong.

**Tests:** the four families appear with `# TYPE` lines; a counter increments once per recorded outcome
and carries the right `status`/`key_pool` labels; the histogram is cumulative and its last bucket equals
`_count`; label values with quotes or backslashes are escaped; disabled → 404; wrong token → 401; no
label anywhere contains a UUID (assert it — this is the privacy rule, and an assertion is the only thing
that keeps a future label from quietly becoming `user_id`); Redis down still returns 200 with counters
and no gauges.

**Done when:** `curl localhost:8000/metrics` is readable by eye and the output would pass `promtool check
metrics` (no need to run it in CI; format it as if it will).

---

### Step 5 — The idempotency store *(1 day)* — **Opus**

Touches `app/cache/idempotency.py` (new), `app/deps.py`, `tests/unit/test_idempotency.py` (new).

A new module in an existing designated directory, beside `exact.py`, and for the same reason `exact.py`
is not in `cache/client.py`: it is a policy over Redis, not a Redis client. Nothing here knows about
FastAPI or about chat.

- `IdempotencyEnvelope` — frozen dataclass with `to_json`/`from_json` mirroring how `AttemptRecord` and
  `MessageMeta` already do this, tolerating an unknown key rather than raising.
- `fingerprint(body, *, user_id) -> str` — SHA-256 over the canonicalized request, reusing
  `cache/exact.py::request_hash`'s serialization discipline (sorted keys, explicit float formatting).
  It must include **everything the answer depends on**: the slot, the messages, every generation
  parameter, the `file_refs`, `conversation_id`, and `stream`. A fingerprint that ignores `stream` lets a
  streaming retry replay a non-streaming body.
- `IdempotencyStore.claim(key, *, fingerprint, request_id, stream) -> ClaimResult` — one `SET NX EX`, and
  on collision one `GET`, returning `Claimed | Replay(envelope) | InFlight | FingerprintMismatch`.
- `IdempotencyStore.complete(key, *, response, ...)` and `.release(key)` — the success and failure ends.
- Every method **fails open**: any Redis exception logs once and returns `Claimed` (or does nothing, for
  `complete`/`release`), so a Redis outage degrades to today's behaviour rather than to a 500.

**Tests** (fakeredis): a fresh key claims; a second claim with the same fingerprint while in flight is
`InFlight`; after `complete`, it is `Replay` and the envelope round-trips; a different fingerprint is
`FingerprintMismatch` in both the in-flight and done states; `release` makes the key claimable again;
the TTL is `IDEMPOTENCY_TTL_S`; every method fails open under a raising client; two concurrent `claim`
calls produce exactly one `Claimed` (the `asyncio.gather` test that proves `NX` is doing the work).

**Done when:** the module is complete and unit-tested and nothing in `app/api/` imports it yet.

---

### Step 6 — Idempotency wired into both chat paths *(1.5 days)* — **Opus**

Touches `app/api/v1/chat.py`, `app/streaming/collector.py`, `app/db/repo/requests.py` (one constant),
`app/usage/logger.py` (one facade function), `app/schemas/errors.py`,
`tests/integration/test_idempotency.py` (new).

1. `STATUS_REPLAYED = "replayed"` beside `STATUS_OK`/`STATUS_ERROR`, with the comment the column's own
   docstring invites.
2. `usage/logger.py::record_replay(...)` — the fifth facade function, for the same reason
   `record_cache_hit` was the fourth: a replay has no `ModelSpec`, no attempt trail, and no tokens.
   `provider`/`model` stay NULL.
3. `api/v1/chat.py` — read the header (`Header(default=None, alias="Idempotency-Key")`, length-capped,
   rejected as a 400 if longer than 255 characters or non-printable, since it becomes a Redis key segment
   and `keys._segment` will refuse it anyway — better a clear 400 than a `ValueError`).
   Claim **immediately after `_validate_slot` and before `_resolve_conversation`**, so a replay writes no
   message rows and does not touch `preferred_slot`. Handle the four claim outcomes per D47. On the
   non-streaming success path, `complete()` with the assembled `ChatCompletionResponse`; on every failure
   path — including the `except` that records a failure row — `release()`.
4. `streaming/collector.py` — the `done` path writes the envelope (it already assembles the full text for
   the exact cache; this is the same hook), and the failure path releases. A replay of a streaming
   request is served through the existing `stream_cached_completion`, which already frames a stored
   answer as `meta` → `delta`* → `done`.
5. Response headers: `X-Idempotent-Replay: true` on a replay, absent otherwise. `X-Cache` keeps its own
   meaning — a replay of a request that was originally a cache hit carries both, and that is correct.

**Tests:** same key twice, identical body → identical response, one provider call, no second message row,
no quota movement, `X-Idempotent-Replay: true`, and a `status='replayed'` row with NULL provider; same
key, different body → 409 `idempotency_key_reuse`; a concurrent duplicate → 409 `idempotency_in_flight`
with `Retry-After`; a failed first attempt → key released, retry with the same key really re-runs; the
streaming twin of the first case, asserting the replay's SSE frames match the live stream's shape; Redis
down → both requests served normally, no 500; no header at all → today's behaviour, byte for byte
(assert against an existing test's expected body, so a regression here fails loudly).

**Done when:** D6 is demonstrable end to end, and the no-header path is provably unchanged.

---

### Step 7 — Keyset message pagination, server side *(0.5 day)* — **Sonnet**

Touches `app/db/repo/messages.py`, `app/schemas/conversations.py`, `app/api/v1/conversations.py`,
`tests/integration/test_repo_messages.py`, `tests/integration/test_conversations_endpoints.py`.

1. `list_page_for_conversation(session, *, conversation_id, user_id, before_seq: int | None = None,
   limit: int = 50)` → `MessagePage(messages, has_more, next_before_seq)`. Same ownership join as its
   sibling; `ORDER BY seq DESC LIMIT :limit + 1`, then reverse to oldest-first and drop the sentinel.
   Its docstring must point at `list_for_conversation` and say which one the render pipeline uses and
   why, so the two never get "unified".
2. `ConversationDetail` gains `has_more: bool` and `next_before_seq: int | None`; the detail route calls
   the new function with `before_seq=None`. A client that ignores both fields sees today's behaviour for
   any thread under 50 messages.
3. `GET /v1/conversations/{id}/messages` → `MessagePageOut`, same `_to_message_out` mapping, ownership
   resolved by `get_owned` first so "not yours" is a 404 rather than an empty page.

**Tests:** a 120-message thread pages exactly three times with no duplicates and no gaps, and the
concatenation equals `list_for_conversation`'s output; `has_more` is false on the last page;
`before_seq` beyond the end returns empty with `has_more=false`; a non-owner gets 404 on the page route
and cannot page another user's thread by guessing the id; `list_for_conversation` is asserted to still
return everything (a regression guard, since Step 8 is where someone will be tempted to "optimize" it).

**Done when:** `git diff` shows no change inside `list_for_conversation`'s body.

---

### Step 8 — Pagination in the frontend *(1.5 days)* — **Opus**

Touches `frontend/lib/{types,api,hooks}.ts`, `frontend/components/{ConversationView,MessageList}.tsx`,
`frontend/tests/`.

- `types.ts`: `has_more`/`next_before_seq` on `ConversationDetail` (**optional**, like every other field
  added to a wire type since Phase 5 — a client build can be newer than the server it talks to), plus
  `MessagePage`.
- `api.ts`: `conversationMessagesKey(id, beforeSeq)` and `fetchMessagePage`.
- `hooks.ts`: `useConversation` keeps its SWR head fetch and gains older pages held in component state,
  `loadOlder()`, `isLoadingOlder`, and `hasMore`. Older pages are **prepended to a local array and never
  written into the head SWR key** (D48's whole reason for two routes). The merged list is `[...older,
  ...head.messages]`, de-duplicated by `id` — the optimistic turn appends to the head and a revalidation
  replaces it, so only the head moves.
- `ConversationView`/`MessageList`: a scroll-up trigger that calls `loadOlder` once per page (guarded —
  an unguarded `onScroll` fires dozens of times per gesture and will request page 4 before page 2
  arrives), and **scroll-anchoring**: capture `scrollHeight` before the prepend and restore
  `scrollTop += (newHeight - oldHeight)` after, or the viewport jumps to the top every time a page lands.
  That is the single most visible bug this step can ship.

**Tests:** `useConversation` merges head + older with no duplicates; `loadOlder` is not re-entrant;
`hasMore` false hides the trigger; an optimistic turn during an in-flight older-page fetch leaves both
intact (the race D48 exists to prevent — assert it directly); a thread under a page renders identically
to today.

**Done when:** a 300-message thread scrolls up smoothly and sending a message still appends correctly.

---

### Step 9 — The usage page *(2 days)* — **Opus**

Touches `frontend/app/usage/page.tsx` (new), `frontend/components/UsageDashboard.tsx` (new),
`frontend/components/charts/{Sparkline,BarRow,Meter}.tsx` (new), `frontend/lib/{types,api,hooks}.ts`,
`frontend/components/AccountDialog.tsx` (one link), `frontend/tests/`.

Panels, in this order (it is the order a reader asks the questions in): volume over time with a
1h/24h/7d switch; outcome rates (error, cache hit, failover) as three meters; provider distribution as
labelled bars with token counts; simulated cost with the shared/private split and the unpriced count
rendered rather than hidden; per-provider quota utilization from `/v1/admin/quota`; and the recent
requests table underneath.

Charts are pure functions of a number array producing inline SVG (D51). Every panel needs an explicit
**empty state** — a brand-new account has no requests, and a dashboard that renders `NaN%` on day one is
the first thing a reviewer sees. Rate denominators are guarded at zero. Cost renders with the currency
from the config and a one-line note that it is simulated at list prices — the disclosure register this
project has used for provenance since Phase 2 applies to a fabricated number too.

**Tests:** each chart component renders expected SVG for a known array and for an empty array; the
dashboard renders every panel from a mocked hook; zero-request state shows empty states and no `NaN`;
the window switch refetches under a new key.

**Done when:** `make frontend-test`, `make frontend-lint` and `next build` are green and the page renders
correctly for an account with zero requests.

---

### Step 10 — `scripts/chaos_demo.py` *(1 day)* — **Opus**

Touches `scripts/chaos_demo.py` (new), `docs/chaos-demo.md` (new), `Makefile`, `README.md` (one link).
**`app/` is not touched — verify with `git diff --stat` before committing.**

Per D50: `create_app()` over `httpx.ASGITransport`, a scripted upstream `MockTransport` reusing
`tests/fixtures/provider_responses/`, N concurrent clients over a fixed duration, a phase schedule that
kills and revives providers, and a live terminal table. Flags: `--duration`, `--concurrency`, `--seed`
(reproducible chaos is the only kind worth recording), `--json out.json`.

The summary is the artifact: total requests, client-visible failures (the number that should be zero),
substitutions disclosed, breaker transitions observed, per-provider served counts, and p50/p95. Then
`docs/chaos-demo.md` — what the script does, how to run it, a real captured transcript, and a paragraph
on what the numbers demonstrate and what they do not (an in-process mock is not a network, and the
latencies are not real latencies).

**Done when:** `python -m scripts.chaos_demo --seed 1` runs twice and produces the same summary.

---

### Step 11 — The README *(1 day)* — **Opus**

Touches `README.md` and `docs/architecture.md` (one new diagram if the README links to it rather than
inlining).

The existing README is four good "why" essays with no map. Add, above them:

1. **What this is**, in four sentences, with the three-provider/two-lane framing and an honest "portfolio
   project, runs entirely on free tiers" line.
2. **The architecture diagram** — the §5.1 request flow and the §5.2 two lanes, as one figure consistent
   with `docs/architecture.md`'s existing style.
3. **A request-flow walkthrough** — one real request traced end to end: auth → rate limit → idempotency
   claim → cache → slot validation → candidate chain → credential resolution → quota reservation →
   attempt → failover → commit → persistence → disclosure. Name the file and function at each hop so it
   doubles as a code tour.
4. **The failure-mode table** — `development-plan.md` asks for it by name: what happens when Postgres,
   Redis, one provider, every provider, the object store, or the extraction lane dies. Every row's answer
   already exists in the code and in the ADRs; this table is where they stop being scattered.
5. **Design Decisions** — all 45 ADRs, grouped by area, each with the one-line question it answers.
   Grouped rather than numbered-in-order: nobody reads an ADR index by number.
6. Keep the four existing essays as "In depth", and add a fifth for idempotency (D6 is one of §13's named
   interview questions and would otherwise be the only one without an essay).

**Done when:** someone who has not seen the repo can answer "what happens when Groq goes down mid-stream"
from the README alone.

---

### Step 12 — ADRs, `docs/limitations.md` finalized, and the rest of the docs *(1 day)* — **Opus**

Touches `docs/decisions/ADR-040…045`, `docs/limitations.md`, `docs/architecture.md`, `docs/deploy.md`,
`.env.example`, `CLAUDE.md`. **No application code.**

Six ADRs: **ADR-040** self-scoped dashboard, no admin identity (D44, and the note that D45's aggregate
signatures are the seam an operator role would widen); **ADR-041** simulated cost computed at read time,
unpriced ≠ free (D46); **ADR-042** idempotency — claim before routing, envelope not bare id, fail open,
and **why this is not a Contract C amendment** (D47); **ADR-043** keyset pagination as a second function
and a second route, with `list_for_conversation` untouched and why (D48); **ADR-044** `/metrics`
hand-rolled, process-local counters and live gauges, label-cardinality and privacy rules (D49);
**ADR-045** the chaos demo drives the real app and changes nothing in it (D50). D51 gets no ADR — it is a
frontend implementation choice with no live alternative once D44 fixed the auth story, and ADR-040's
consequences section says so, following the precedent ADR-032 set for D33.

`docs/limitations.md` — the honest-edges document, **finalized**, meaning: a new section for Phase 7 (the
per-worker `/metrics` counters; simulated cost being a fiction at list prices; a replayed request
counting in volume but not in provider distribution, so the two numbers legitimately disagree; the
dashboard being self-scoped and therefore not an ops console; pagination not applying to the render
pipeline, so a very long thread still costs a full history read on every turn), **and** a pass over
every existing section for anything Phase 6 or 7 made stale — the out-of-scope list in particular, which
must now stop naming idempotency and message pagination.

`docs/architecture.md` — "Phase 7: reading the system back" — one diagram of where each number on the
dashboard comes from (Postgres aggregate, Redis live, process-local counter), because "which of these
three is authoritative" is the question the page raises and the code answers in three different places.

`docs/deploy.md` — `METRICS_ENABLED`/`METRICS_TOKEN` rows, the note that Render has no private network so
the token is not optional there, and a line on scraping a two-worker service. `.env.example` matching.

`CLAUDE.md` — via the `update-claude-md` skill, marking Phase 7 complete and the project at v1.

**Done when:** `development-plan.md` §8's Definition of Done checklist can be ticked line by line against
the repo.

---

### Step model summary

| Step | Work | Model | Why |
|---|---|---|---|
| 1 | pricing config + cost function | **Sonnet** | Mirrors `LimitsConfig` exactly; one pure function |
| 2 | `requests` aggregates | **Opus** | SQL semantics with six ways to be plausibly wrong (D45) |
| 3 | `api/admin.py` | **Sonnet** | Three thin handlers over Step 2, `api/v1/models.py` as the template |
| 4 | `/metrics` | **Opus** | Format correctness, counter placement, fail-open, cardinality rules |
| 5 | idempotency store | **Opus** | Concurrency semantics; the claim protocol is the design |
| 6 | idempotency wiring | **Opus** | Touches the one endpoint that must not regress, on both paths |
| 7 | pagination repo + route | **Sonnet** | Textbook keyset query with a precise spec |
| 8 | pagination frontend | **Opus** | Async state races, scroll anchoring, optimistic-turn interaction |
| 9 | usage page + charts | **Opus** | Composition, empty states, hand-rolled SVG |
| 10 | chaos demo | **Opus** | Standalone harness driving a real ASGI app deterministically |
| 11 | README | **Opus** | The prose is the deliverable |
| 12 | ADRs + docs | **Opus** | Same |

---

## 5. Traps

1. **`list_for_conversation` stays unpaginated.** Said in `development-plan.md` twice, in the repo
   docstring, in D48, and here. D4's fitting step needs the whole history to choose what to drop; a page
   of it moves the truncation decision somewhere that cannot make it well. Step 7 adds a function; it
   does not edit one.
2. **The idempotency claim goes before the conversation is touched.** After `_validate_slot`, before
   `_resolve_conversation`. Claim late and a replay appends a duplicate user message and moves
   `preferred_slot` before discovering it had nothing to do.
3. **A failed request must release its claim.** Every `except` path, the streaming failure path included.
   A 502 that leaves an `in_flight` envelope locks that key for 24 hours and the client's retry — the
   exact thing D6 exists to serve — gets a 409.
4. **Redis down means idempotency fails open, not closed.** Caching's rule, not quota's (D15). Getting
   this backwards turns a Redis blip into a total outage.
5. **A cache hit writes `provider`/`model` on its `requests` row.** `record_cache_hit` names the candidate
   that *originally* answered, with zero tokens. Provider distribution must exclude `cache_hit = true`
   rows or every dashboard over-reports provider calls.
6. **`provider IS NULL` is not a provider called "unknown".** The repo docstring is explicit: NULL means
   "never got that far". Bucketing it as a provider poisons the exact query the table exists to serve.
7. **An unpriced model costs `None`, not `$0`.** A silent zero makes the total a lie in the flattering
   direction, which is the worst kind.
8. **Empty time buckets must be generated, not discovered.** `GROUP BY date_trunc` omits quiet periods
   and the chart then shows a smooth line through an outage.
9. **`/metrics` counters are per-worker.** Document it, do not pretend otherwise, and do
   not "fix" it by inventing Redis keys — that is a Contract C amendment and needs sign-off. (The
   deployed service pins one worker; see the correction under D49.)
10. **No `user_id` in a metric label.** Ever. Cardinality and privacy, and the assertion in the test suite
    is what keeps it true.
11. **`X-Cache` and `X-Idempotent-Replay` are different facts.** A replay of a cache hit sets both. Do not
    collapse them.
12. **The optimistic turn writes to the head SWR key only.** Older pages live in component state. A
    `globalMutate` that rewrites the merged list will drop the pages already loaded, and the symptom —
    the thread getting shorter after you send a message — looks like data loss to a user.
13. **Prepending a page without anchoring the scroll jumps the viewport.** Capture `scrollHeight` before,
    restore after. This is not polish; it makes the feature unusable.
14. **The chaos script may not touch `app/`.** If it needs a hook, redesign the script.
15. **No new runtime dependencies.** Not `prometheus_client`, not a chart library, not `pandas` for the
    aggregates. Three separate temptations in one phase.
16. **`attempts` entries predate `key_pool`.** Any JSONB read must tolerate its absence, the same way
    `MessageMeta.from_jsonb` tolerates a missing key rather than backfilling one (phase5 trap 7).
17. **The dashboard is not an ops console.** Resist adding "all users" toggles, provider health controls,
    or an allocation editor. D44 is a decision; re-opening it in Step 9 is how this phase stops shipping.
18. **`status='replayed'` is a new value in an unconstrained column.** Check every existing reader of
    `requests.status` — the aggregates written in Step 2 among them — before Step 6 introduces it.

---

## 6. Test matrix

| Layer | What | Where |
|---|---|---|
| Unit | `simulated_cost`: priced, unpriced (`None`), zero tokens, `Decimal` precision | `tests/unit/test_pricing.py` (new) |
| Unit | pricing YAML loads; every enabled candidate priced; unpriced warning fires | `tests/unit/test_config.py` |
| Unit | Metrics exposition: `# TYPE` lines, cumulative histogram, label escaping, no UUID in any label | `tests/unit/test_metrics.py` |
| Unit | Counter increments once per facade call with correct labels | `tests/unit/test_metrics.py` |
| Unit | Idempotency envelope round trip, fingerprint sensitivity (incl. `stream`), all four claim outcomes, fail-open, concurrent single-winner | `tests/unit/test_idempotency.py` (new) |
| Integration | Aggregates: empty buckets present, NULL-provider excluded, cache hits excluded, replays counted once, cross-user isolation, pool split, pre-Phase-6 `attempts` shape | `tests/integration/test_repo_requests.py` |
| Integration | Keyset pagination: exact page boundaries, no gaps/dupes, concatenation equals `list_for_conversation`, 404 for a non-owner | `tests/integration/test_repo_messages.py` |
| Integration | `/v1/admin/*`: 401, self-scoping with two users, window validation, unpriced surfacing, quota under the caller's own scope | `tests/integration/test_admin_endpoints.py` (new) |
| Integration | `/metrics`: disabled 404, bad token 401, Redis down still 200 with counters | `tests/integration/test_metrics_endpoint.py` (new) |
| Integration | Idempotency: replay identical / one provider call / no message row / no quota move / `status='replayed'`; reuse 409; in-flight 409; released after failure; streaming twin; Redis down; **no-header path byte-identical to today** | `tests/integration/test_idempotency.py` (new) |
| Integration | Detail route returns newest page + `has_more`; page route walks a 120-message thread | `tests/integration/test_conversations_endpoints.py` |
| Frontend | `Sparkline`/`BarRow`/`Meter` render for known and empty arrays | `frontend/tests/charts.test.tsx` (new) |
| Frontend | Dashboard panels, zero-request empty states, no `NaN`, window switch | `frontend/tests/UsageDashboard.test.tsx` (new) |
| Frontend | `useConversation` merge, `loadOlder` non-re-entrancy, optimistic turn during an older-page fetch | `frontend/tests/useConversationPaging.test.tsx` (new) |
| Script | `chaos_demo --seed 1` is reproducible | manual, documented in `docs/chaos-demo.md` |

Coverage concentration for this phase: `db/repo/requests.py`'s aggregates, `cache/idempotency.py`, and
`usage/metrics.py`'s exposition.

---

## 7. Documentation

| Document | Change |
|---|---|
| `docs/decisions/ADR-040-self-scoped-usage-dashboard.md` | new (D44) |
| `docs/decisions/ADR-041-simulated-cost-at-read-time.md` | new (D46) |
| `docs/decisions/ADR-042-idempotency-claim-before-routing.md` | new (D47) |
| `docs/decisions/ADR-043-keyset-pagination-beside-the-full-read.md` | new (D48) |
| `docs/decisions/ADR-044-hand-rolled-metrics-endpoint.md` | new (D49) |
| `docs/decisions/ADR-045-chaos-demo-drives-the-real-app.md` | new (D50) |
| `docs/limitations.md` | Phase 7 section **and** a staleness pass over every existing section |
| `docs/architecture.md` | "Phase 7: reading the system back" — where each dashboard number comes from |
| `docs/chaos-demo.md` | new — what the script does, how to run it, a captured transcript, what it does and does not demonstrate |
| `docs/deploy.md` | `METRICS_ENABLED`/`METRICS_TOKEN`; scraping a two-worker service |
| `README.md` | the build-out (Step 11) |
| `.env.example` | the two new variables |
| `CLAUDE.md` | Phase 7 complete, project at v1 — via the `update-claude-md` skill |
| `doc/reference/phase7.md` | this file — kept accurate as steps land, the way phases 3–6 were |

---

## 8. Exit checklist

- [ ] `config/pricing.yaml` covers every enabled candidate; a gap warns at boot and surfaces as
      `unpriced_requests`, never as `$0`.
- [ ] Aggregates run in SQL, are ownership-scoped in the query, and generate empty buckets.
- [ ] Provider distribution excludes cache hits and NULL-provider rows; volume includes both.
- [ ] `/v1/admin/*` is self-scoped on every route; two accounts see two different dashboards.
- [ ] No `is_admin`, no `Principal` change, no write surface for `user_quota_allocations`.
- [ ] `/metrics` exposes four families, escapes labels, emits a cumulative histogram, 404s when disabled,
      401s on a bad token, and returns counters with no gauges when Redis is down.
- [ ] No metric label anywhere contains a user id, email, or conversation id — asserted in a test.
- [ ] Same `Idempotency-Key`, same body → one provider call, identical response,
      `X-Idempotent-Replay: true`, no quota movement, no duplicate message row.
- [ ] Same key, different body → 409. Concurrent duplicate → 409 with `Retry-After`.
- [ ] A failed request releases its key and the retry really re-runs.
- [ ] Streaming replay is framed identically to a live stream.
- [ ] Redis down → requests are served, idempotency silently disabled.
- [ ] A request with no `Idempotency-Key` behaves exactly as before this phase.
- [ ] `git diff` shows `list_for_conversation` unchanged.
- [ ] A 300-message thread pages up with no duplicates, no viewport jump, and correct appends.
- [ ] `python -m scripts.chaos_demo --seed 1` is reproducible and `git diff --stat` shows no `app/` change
      in that commit.
- [ ] README carries the architecture diagram, the request-flow walkthrough, the failure-mode table, and
      all 45 ADRs indexed by question.
- [ ] `docs/limitations.md` no longer lists idempotency or pagination as out of scope, and carries the
      per-worker metrics caveat and the simulated-cost caveat.
- [ ] `development-plan.md` §8's eleven-line Definition of Done ticks line by line.
- [ ] `make test`, `make lint`, `make typecheck`, `make frontend-test`, `make frontend-lint` green.

---

## 9. What Phase 7 hands to v1 and the stretch backlog

**The gateway is feature-complete against §8's Definition of Done.** Everything after this is
`development-plan.md` §7's ordered backlog, and Phase 7 leaves each of its first three items a visible
seam rather than a rewrite:

- **Latency-based routing** already exists in its EWMA form (D11); §7's item 1 is the
  p50-over-a-rolling-window version, and the thing it needs — shared cross-instance latency — is the same
  Contract C amendment `/metrics`' process-local counters need. One decision unlocks both.
- **Summarization (§7 item 2)** replaces D4's truncation. `memory/summarize.py` is still the seam, the
  `summary` block type is still reserved and still rejected at the JSONB boundary, and
  `fitting.FitStrategy` still has one member. Phase 7 adds the number that makes the case for building
  it: `messages_dropped` is now aggregatable, so "how often does truncation actually fire" is a query.
- **Semantic caching (§7 item 3)** sits behind `cache/exact.py`'s `is_cacheable`, whose two gates
  (`degraded`, `truncated`) are the ones a similarity cache would also have to respect.

**Left deliberately unbuilt, seams visible:** `allocations_repo.set_cap` and any operator identity
(ADR-040); rollup tables for the dashboard; `scripts/seed_dev.py`; `MultiFernet` key rotation (ADR-035);
`owner_type='system'` rows in `provider_keys` (D37); `pin_target`'s tool branch; a shared, cross-instance
counter store for `/metrics`.
