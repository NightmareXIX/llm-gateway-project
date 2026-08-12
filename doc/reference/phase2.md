# Phase 2 — Multi-Provider Core, Failover & Streaming

Implementation plan. Derived from `development-plan.md` §3 Phase 2, amended by `contracts-and-phase1.md`
§1 (D1/D2) and §1.1, and written against the code Phase 1 actually shipped rather than against the
skeleton it was planned from.

Where the overview and the contracts doc disagree, the contracts doc wins — which matters here more than
anywhere else in the project, because the original development plan chose "never fail over mid-stream"
and D1 overrode it with "restart the stream and disclose". **Phase 2 builds D1's restart, not the
development plan's simpler boundary.** Everything downstream of that choice — the attempt trail, the
`wasted_tokens_out` column, `stream:{message_id}:attempts`, the `restart` SSE event, the frontend's
"clear the bubble" contract — already exists as a seam and is waiting for it.

---

## 1. Scope

**Goal:** three providers behind one interface, automatic failover with an observable circuit breaker,
and SSE streaming that survives a provider dying mid-generation.

**In scope**

- `providers/gemini.py` and `providers/openrouter.py`, both passing the existing conformance suite.
- `stream()` on all three adapters — real async generators, no buffering, idle-timeout enforced.
- Redis arrives for real: `cache/client.py`, `cache/keys.py`, and the breaker's keys.
- `routing/circuit_breaker.py` — hand-rolled, three-state, Redis-backed, shared across instances.
- `routing/router.py` + `selection.candidates()` — the ordered candidate list and the failover loop,
  serving **both** the streaming and non-streaming paths.
- `streaming/{sse,orchestrator,collector}.py` — the §1.1 wire protocol and the D1 restart state machine.
- `POST /v1/chat/completions` with `stream: true` accepted instead of refused.
- The attempt trail in `requests.attempts`, `substituted`/`attempts` populated for real on both the
  response body and `messages.meta`.
- Frontend: `openCompletionStream` implemented, `ModelIndicator` rendering substitution and restarts.

**Explicitly NOT in Phase 2** — these are Phase 3's, and pulling them forward is how this phase stops
being demoable:

- No quota tracking or enforcement. The router fails over *reactively*, on a 429 it did not predict.
  `quota/` stays empty. This is the single biggest scoping line in the phase: Phase 3's whole thesis is
  "the router stops guessing and starts knowing", and it needs a working reactive path to improve on.
- No `GET /v1/models` with live status (it has nothing live to report yet).
- No exact-match cache, no idempotency, no per-user rate limiting.
- No perception lane. `NoAttachments` stays the resolver; Gemini's `inline_data` branch stays unbuilt.
- No BYOK. `registry.system_key()` remains the only credential path.

**Definition of done:** with the Groq key revoked mid-session, a user's messages keep being answered —
by Gemini, then OpenRouter — the UI says so under every message, the breaker's state transitions are in
the logs, and `stream: true` delivers tokens incrementally through a provider failure without the client
ever seeing a broken bubble.

**One small Alembic migration.** Phase 1 already added `requests.attempts`, `requests.substituted`,
`requests.wasted_tokens_out` and every `MessageMeta` field this phase populates — so the failover and
streaming work needs nothing. What D11 adds is a single nullable `requests.ttft_ms` column: with
streaming, time-to-first-token becomes the number that actually characterizes a provider, and
`latency_ms` keeps meaning total wall time. The routing table itself is in-process and needs no
persistence, but the column is what lets Phase 7's dashboard chart TTFT and what a future warm-start
would read. One nullable column now, following §4 Step 2's own "add them now anyway" reasoning, beats a
second migration in Phase 3.

---

## 2. What Phase 1 left cut, and what Phase 2 does to each seam

| Seam | Where | State today | Phase 2 |
|---|---|---|---|
| `ProviderAdapter.stream` | `providers/base.py:271` | plain `def` raising `NotImplementedError` (deliberately not an async generator, so it fails at the call, not at the first `__anext__`) | Real async generators in all three adapters; base gains shared SSE-frame plumbing |
| `selection.candidates()` | `routing/selection.py:62` | typed signature, `NotImplementedError` | Implemented: `auto` expansion, named-slot chain, D3 pin |
| `routing/router.py` | — | does not exist | The failover loop |
| `routing/circuit_breaker.py` | — | does not exist | Three-state, Redis-backed |
| `streaming/*` | `app/streaming/` | empty package | `sse.py`, `orchestrator.py`, `collector.py` |
| `cache/*` | `app/cache/` | empty package | `keys.py`, `client.py` (`exact.py` waits for Phase 3) |
| Redis pool | `main.py:80` | `# Phase 3 — redis.asyncio pool` comment | Arrives one phase early; the breaker needs it |
| `/readyz` | `main.py:137` | Postgres only, per ADR-009 | Redis becomes load-bearing → revisit the ADR (§4.6 below) |
| `_PROVIDER_TRAITS` | `providers/registry.py:76` | groq only, with the two others named in a comment | Gemini `supports_system_field=True`, OpenRouter `False` |
| `_conformance_check` | `providers/registry.py:273` | one line, asserts Groq conforms under mypy | One line per new adapter |
| `ADAPTERS` list | `tests/contract/test_adapter_conformance.py:59` | one `AdapterCase`, with "Phase 2 appends gemini and openrouter here" | Two more cases; suite cases 3 & 4 self-activate |
| `stream: true` | `api/v1/chat.py:84` | 400 `streaming_not_supported` | Routed to the orchestrator |
| `_is_substitution` | `api/v1/chat.py:258` | computed, always `False` | Becomes true for the first time |
| `frontend/lib/sse.ts` | — | full event typings, `openCompletionStream` throws | Implemented against those exact typings |
| `providers.yaml` | `config/` | gemini/openrouter declared, `enabled: false`, no candidates | Enabled, with real failover chains |
| `limits.yaml` | `config/` | `gemini: {}`, `openrouter: {}` | Populated (declared now, *enforced* in Phase 3) |

The lesson to carry: almost nothing in this phase is a new call site. If a step here requires editing a
signature Phase 1 shipped, stop and check whether the seam was meant to absorb it.

---

## 3. Decisions to settle before writing code

Six questions the frozen contracts do not answer. Each needs an ADR (`docs/decisions/` currently holds
only ADR-007 and ADR-009, so the numbering is open). In each case the reasoning, not the verdict, is the
deliverable — an ADR that records what was chosen without recording what it was chosen over is a note,
not a decision.

**Status: all six are decided. The text below is the design, not a proposal.** D11 was decided *against*
the original recommendation: latency-based ranking was pulled forward from the stretch backlog in place
of static config order. Every other decision took the recommendation as written.

### D9 — Redis unreachable: does the breaker fail open or closed?

Contract C specifies the asymmetry for quota (**closed**) and for caching plus our own rate limiting
(**open**), and says nothing about the breaker because the breaker was scheduled for the same phase as
quota. It arrives alone.

**Decided: fail open** — an unreachable Redis means every candidate is treated as `closed`/allowed.
The breaker is an *optimization* that skips attempts we predict will fail; the normalized error
hierarchy still protects the request if the prediction was needed. Failing closed turns one Redis blip
into a total outage of a gateway whose providers were all healthy. This is the opposite of the quota
rule, and the difference is the point: quota-closed protects a *provider's* key from getting banned,
breaker-open protects *our* availability, and nothing about a missing breaker state can get a key
banned.

Log every fail-open at warning with a counter; a permanently-down Redis should be visible, not silent.

### D10 — Does a named slot's failover chain spill into other slots?

D2 says a specific exhausted slot fails over silently and then discloses. `selection.candidates()`'s
docstring says a named slot expands to "that slot's own candidates". Those two are not the same rule
when a slot's every candidate is exhausted.

**Decided: spill.** Try the named slot's candidates in priority order; if all of them fail with
`failover_eligible` errors and the attempt budget is not spent, continue into the `auto` chain minus
what has already been tried. The decisive argument is that `substituted` is otherwise a dead field:
`_is_substitution` compares `requested_slot` to `served_slot`, and without cross-slot spill those can
never differ, which would make the response's provenance block — the load-bearing honesty mechanism —
structurally incapable of ever reporting a substitution.

### D11 — `auto` expansion: ordering and de-duplication

`auto` is a sentinel, not a slot. It must become a concrete ordered list.

**Decided: latency-ranked, with config order as the seed, the tiebreak, and the fallback.** This pulls
stretch-backlog item 1 (`development-plan.md` §7) forward into Phase 2. The mechanics matter more than
the headline, because the naive implementation of this ranks providers backwards.

**The list is built first, then sorted.** Slots in `providers.yaml` declaration order, each slot's
candidates in priority order, flattened, de-duplicated on `(provider, model)` keeping the first
occurrence. De-duplication is on the *pair*, not the provider: two Groq models are two genuinely
different candidates, since free-tier limits are per-model, and collapsing on provider would erase a
whole failover chain. That flattened list is the input to the ranking and the answer whenever the
ranking has nothing to say.

**The metric — three rules, each of which is a bug if broken:**

1. **Successful attempts only.** A provider that 429s in 80ms is the fastest thing in the fleet by wall
   clock and the worst possible choice. Feeding failures into the average makes `auto` actively seek
   out broken providers. Only attempts that produced usable content update the series.
2. **Two series per `(provider, model)`, not one.** Streaming ranks on **time to first token**; the
   non-streaming path ranks on total latency. They are not comparable numbers — total streaming latency
   is dominated by output length, so ranking streams by it just prefers whichever model is most terse.
   A request ranks against the series matching its own mode.
3. **EWMA, not a true p50.** A rolling percentile needs a retained sample window per candidate; an
   exponentially-weighted mean needs one float and answers the same question well enough to sort three
   items. Revisit if the ordering ever looks unstable.

**Storage: in-process, in `app/usage/metrics.py`** — a small EWMA table the router updates on every
successful attempt. Deliberately *not* Redis. Cross-instance latency sharing would require a new key
format, and Contract C is frozen; that is a change worth making later with sign-off, not a side effect
of this step. Two uvicorn workers on one Fly instance converge on their own within a few dozen requests,
and staleness is self-correcting because the only way to update a candidate's number is to actually use
it.

**Guardrail: rank only where there is evidence.** A candidate with fewer than N successful samples
(start at 5) in the window keeps its config position. A cold process therefore behaves exactly like the
config-order design until it has learned something, which is also what makes the first request after a
deploy predictable rather than arbitrary.

**Availability outranks latency, always.** The sort applies *within* the set of candidates that already
passed the breaker check — it never promotes a candidate the breaker would skip, and in Phase 3 it will
run after the quota filter for the same reason.

**Purity is preserved by passing the snapshot in.** `selection.py` documents itself as doing no I/O and
being a pure function of the registry, and reading a mutable module-level table would quietly end that.
The signature becomes:

```python
def candidates(registry, requested, *, pinned=None, latency=None) -> tuple[ModelSpec, ...]
```

Still pure — a function of (registry, request, latency snapshot) — so its tests stay a table and Phase
3's `/v1/models` can still call it without standing up a request.

**The honest caveat, to write into the ADR:** latency ranking without quota data preferentially selects
the provider you are closest to rate-limiting. Groq is the fastest thing in your pool *and* has the
tightest TPM ceiling, so `auto` will lean on it until it 429s. Phase 2 has no quota filter, so the only
thing catching that is reactive failover, at the cost of one wasted round trip per exhaustion. Phase 3's
quota filter is the real fix. Until then, put the reordering behind a settings flag
(`ROUTING_LATENCY_RANKING`, default on) so it can be switched off in one deploy if it misbehaves during
the phase — a flag is cheaper than a revert when the thing you are debugging is the router itself.

### D12 — Attempt cap, and where it is counted

D1 caps at 3 attempts per message and Contract C reserves `stream:{message_id}:attempts` (TTL 300s).

**Decided:** the in-process loop counter is authoritative — one request is served by one process, and
a distributed counter cannot make a decision a local variable cannot make faster and more correctly. The
Redis key is written for *observability* (and as the seam a future multi-instance retry-resume would
need), not read for control flow. Say this in the ADR; "we wrote a Redis key we deliberately do not
read" is a claim that needs its reasoning attached. Apply the same cap of 3 to the non-streaming path,
so the two behave identically.

**An attempt is a request that left the process.** A candidate skipped because its breaker is open cost
no round trip and does not consume one of the three. This is why the candidate list is *not* truncated
to the cap: three open breakers at the head of the list would otherwise exhaust the chain without a
single request being made, while healthy providers sat at position four. The list runs to its full
length; the cap binds on attempts that actually happened. Skips are still recorded in the trail as
`skipped_breaker`, so the count in `requests.attempts` can legitimately exceed 3 while `meta.attempts`
does not.

### D13 — When does a streaming request commit to HTTP 200?

Once an SSE body has started, the error envelope is unavailable — the status line is already sent, and
every failure after that is in-band (`done` with `status: "failed"`). That is unavoidable mid-stream and
avoidable before it.

**Decided: do not yield the first byte until an upstream attempt has produced its first chunk.** The
router's pre-first-delta failures then surface as ordinary JSON error envelopes with a `request_id` —
the same shape Phase 1 clients already handle — and only genuine mid-stream faults go in-band. This
quietly preserves the *original* D1 boundary as an implementation detail underneath D1's restart
behavior, and it means a fully-down provider pool produces a debuggable 502 rather than a 200 that says
"failed" inside itself. Mechanically it is free: Starlette sends headers when the body generator yields
its first value, so the router loop simply runs before the first `yield`.

Consequence: `meta` is emitted immediately *before* the first delta rather than at request start. It
costs nothing real — the client has nothing to render until there is a token anyway.

**A separate, tighter first-token budget.** The risk this decision introduces is silence: with nothing
sent, a client waiting on a candidate that stalls has no headers, no status, and no way to tell the
gateway apart from a dead socket — and some clients and proxies impose their own header-response
timeouts. So the pre-first-byte phase gets its own limit, `DEFAULT_FIRST_TOKEN_TIMEOUT_S = 10.0`,
distinct from `DEFAULT_IDLE_TIMEOUT_S = 30.0`:

- **First token: 10s.** A provider that has produced nothing at all is not warming up, it is not
  answering, and two others are standing by. Exceeding it is an `Unavailable` like any other stall —
  failover-eligible, breaker-eligible, and cheap because nothing was streamed.
- **Between tokens, once flowing: 30s.** A model mid-generation has demonstrated it works; the bar for
  abandoning it is rightly higher.

This also bounds D13's worst case honestly: three candidates that each stall costs ~30s before the
client sees anything, not ~90s. Both constants live in `providers/base.py` beside the existing timeout
defaults, and `stream` takes the first-token budget as a parameter rather than reading a global.

### D14 — Where the streaming path gets its database session

`deps.get_session` yields a request-scoped `AsyncSession`. FastAPI tears down `yield` dependencies
around the response lifecycle, and a `StreamingResponse` body generator is not a safe place to be
holding one — you get a closed session, or a session pinned open for the entire generation, which on a
free-tier pool is worse.

**Decided:** the orchestrator/collector opens its own short-lived session from
`app.state.db_session_factory` at the moment it needs to persist, after the stream is done. This also
preserves the rule `api/v1/chat.py`'s docstring already states — never hold a transaction across a
provider call that can legitimately take sixty seconds.

The lifetime across a streamed turn is therefore: request-scoped session persists and commits the user's
message *before* streaming begins; **no session is held for the duration of the generation**; the
collector opens a fresh one after `done`, writes the assistant message and the `requests` row, commits,
and closes. The middle phase is the point — a free-tier pool cannot afford a connection pinned for
thirty seconds per concurrent chat, and that failure appears only under load.

**A persistence failure after `done` must not raise.** The user already has their answer; there is no
response left to turn into a 500, and throwing produces a traceback attached to a request that visibly
succeeded. Log it at error level and swallow it. The honest cost is that the message on screen will not
survive a refresh — worse than persisting, better than a spurious crash, and visible in the logs rather
than silent. Taking a session *factory* rather than a session also makes the collector testable without
standing up a request.

---

## 4. Implementation steps

Ordered so that two internal milestones are demoable rather than one at the end:

- **Milestone A (Steps 1–6):** three providers, reactive failover, breaker — non-streaming.
- **Milestone B (Steps 7–11):** streaming, then D1 restart on top of a failover loop already proven.

### Step 1 — Dependencies, settings, config surface *(half a day)*

> **In plain terms.** Before the gateway can talk to Gemini and OpenRouter, it has to know they exist:
> where they live, which environment variable holds their key, and what their models are called. This
> step is paperwork — no logic, no network calls, no cleverness. You are filling in forms.
>
> **After this step.** The app boots with three providers configured instead of one, and a typo in a key
> name kills the process at startup with a message naming the variable, rather than surfacing as a
> confusing 502 halfway through a demo. Nothing new *works* yet — but nothing is missing either.

- `pyproject.toml`: add `fakeredis>=2.26` to the dev extra. `redis>=5.2` is already a runtime dep and
  goes unused today. Do **not** add `tenacity` — see Step 4.
- `app/config.py::Settings`: add `GEMINI_API_KEY: SecretStr`, `OPENROUTER_API_KEY: SecretStr`, and
  `ROUTING_LATENCY_RANKING: bool = True` (D11's kill switch).
  `registry._resolve_system_key` already resolves `api_key_env` against a `Settings` *field* and already
  raises a `ConfigError` naming the variable when it is missing or empty, so this is two lines plus
  `.env.example` entries.
- `config/providers.yaml`: `enabled: true` on both, and per-provider `options` (see below).
- `config/limits.yaml`: fill in the `gemini` and `openrouter` blocks with current published numbers and
  the right `reset` kinds — Gemini's daily window is `fixed_daily_pt`, which is the reason that enum
  exists. Nothing reads them for enforcement until Phase 3; declaring them now means Phase 3 is a
  tracker, not a research project.

**OpenRouter's attribution headers** (`HTTP-Referer`, `X-Title`) need somewhere to live. They are not
secrets and vary per deployment.

*Recommend:* add `options: dict[str, str] = {}` to `ProviderEntry` in `app/config.py`, carry it in
`providers.yaml`, and widen `AdapterFactory.__call__` to `(*, client, base_url, options)`. Every adapter
accepts it; ones with nothing to configure ignore it. This keeps the registry free of per-provider
construction branches. **It does not touch Contract A** — `AdapterFactory` is registry-internal and
`__init__` was never part of the frozen `ProviderAdapter` protocol. Worth stating explicitly in the
commit message, because it looks like a contract change at a glance.

### Step 2 — Redis arrives *(1 day)*

> **In plain terms.** Redis is a very fast scratchpad shared by every copy of your app. Postgres
> remembers things forever; Redis remembers things for the next sixty seconds and then forgets. It has
> been sitting in Docker doing nothing since Phase 1. This step plugs it in: open the connection at
> startup, close it at shutdown, and write the one module that is allowed to build key names (so that
> key strings never get scattered across the codebase where they can drift apart).
>
> **After this step.** The app can read and write Redis. Nothing uses it yet — but the next step needs
> somewhere to leave notes that every copy of the app can see, and this is that somewhere.

`app/cache/keys.py` — **every key builder in Contract C**, written now even though Phase 2 only calls
two of them. The hard rule ("never write an f-string Redis key outside `app/cache/keys.py`") is only
enforceable if the builder exists when someone needs the key; a half-populated module is how the first
f-string gets written. Builders take typed arguments and the module owns the TTL constants alongside
them.

Phase 2 calls: `cb(provider, model)` and `stream_attempts(message_id)`. Phase 3 calls the rest.

`app/cache/client.py` — a `redis.asyncio` pool created in the lifespan, plus script loading (`SCRIPT
LOAD` / `EVALSHA` with a `NOSCRIPT` fallback) which Phase 3's `reserve.lua` needs and which is much
easier to get right now, with one trivial script, than later with three real ones.

Wire into `main.py`'s lifespan next to `http_client`, close it in the `finally`. Add a `RedisDep` to
`deps.py` alongside `RegistryDep`.

`/readyz`: Redis is now load-bearing, which contradicts ADR-009's stated reasoning ("a readiness probe
that fails on an unused dependency takes the app out of rotation for no reason"). But D9 says the
breaker fails *open*, so an instance with a dead Redis can still serve every request correctly, just
less efficiently — which is the definition of ready. **Recommend:** `/readyz` reports Redis status in
its body but does not fail on it; supersede ADR-009 with an ADR that states the general rule (a
readiness probe fails only on dependencies whose absence makes the instance unable to serve, and
fail-open dependencies are by construction not that).

### Step 3 — Circuit breaker *(1.5 days)*

> **In plain terms.** It is named after the electrical thing, and it works the same way. When a provider
> keeps failing, you flip a switch that says "stop trying this one for a while" — because sending
> requests to something you already know is broken just wastes time and makes the user wait. After a
> cooldown the switch goes to a middle position where exactly *one* request is allowed through to test
> the water. If it succeeds, everything goes back to normal; if it fails, the cooldown gets longer.
>
> **After this step.** The gateway has a memory of failure. You can ask it "is Gemini worth trying right
> now?" and get a real answer, shared across every running copy of the app. Nothing calls it yet.

`app/routing/circuit_breaker.py`. Per `(provider, model)`, state in the `cb:{provider}:{model}` hash
(`state`, `failures`, `opened_at`, `cooldown_s`, `probe_holder`), TTL 1h.

```
closed     → normal. `failures` increments on breaker-eligible errors only.
             5 consecutive failures, or any single RateLimited, → open.
open       → candidate is skipped without an attempt, for `cooldown_s`.
             cooldown = retry_after_s when the provider told us, else exponential 30s → 300s.
half_open  → the cooldown elapsed. Exactly one request may probe.
             Probe succeeds → closed, failures reset, cooldown reset.
             Probe fails    → open, with the next cooldown up the ladder.
```

Two details that are easy to get wrong:

- **Only `breaker_eligible` errors count.** The flags are class attributes on the normalized hierarchy
  precisely so this is a one-line check and cannot be overridden per-occurrence. `EmptyResponse` is
  *not* breaker-eligible — a free tier returning 200-with-nothing is annoying, not broken, and opening
  on it takes a working provider out of rotation for the whole cooldown.
- **The half-open probe must be exclusive across instances.** Use `HSETNX cb:{p}:{m} probe_holder
  <request_id>` — atomic, single round trip, and it uses a field the frozen key schema already
  declares, so no new key is invented. Clear the field on both outcomes.

Log every transition at info with `provider`, `model`, `from`, `to`, `failures`, `cooldown_s`. The
Phase 7 chaos demo's most persuasive frame is a log tail showing breakers opening and closing; it is
free if the transitions are logged from the start and expensive to retrofit.

An injected clock (`app/core/clock.py` exists) rather than `time.time()`, so the state-machine tests are
deterministic instead of `sleep`-based.

### Step 4 — `selection.candidates()` and `routing/router.py` *(2 days)*

> **In plain terms.** This is the brain of the phase, and it is two jobs. First: given "the user asked
> for `general`", produce an ordered shortlist of models worth trying. Second: walk that shortlist —
> check the breaker, send the request, and when it fails, decide from the *kind* of failure whether to
> retry the same model, move to the next one, or stop entirely. That last distinction is the whole
> game: a rate limit means "try someone else", but a malformed request means "stop, this will fail
> everywhere, and trying it three more times just wastes three more seconds".
>
> **After this step.** A function you can hand a request to that will try up to three models and come
> back with either an answer or a failure that explains itself, plus a record of everything it tried.
> Still not connected to the web endpoint — that is the next step, and it is a small one.

`selection.candidates(registry, requested, *, pinned=None, latency=None)` implements D10/D11: pin wins
outright (single-candidate list), else named-slot chain then spill, else `auto` expansion, with the
latency snapshot reordering the result where it has enough samples. Pure, no I/O — which is what lets
Phase 3's `/v1/models` reuse it, and what lets its tests be a table.

`usage/metrics.py` gains the EWMA table behind a tiny interface — `record(provider, model, mode,
ms)` and `snapshot()`. The router records only on success; the snapshot is taken once per request and
passed down, so a candidate list cannot reorder underneath a retry loop that is midway through walking
it. Phase 7 exposes the same table as a Prometheus gauge and needs no second source.

`routing/router.py` holds the loop. One entry point serving both paths, because the alternative is two
copies of the attempt bookkeeping that drift:

```python
async def route(...) -> RouterOutcome           # non-streaming
def route_stream(...) -> AsyncIterator[...]     # Step 9's orchestrator drives this
```

The loop, per candidate:

1. Ask the breaker. `open` → skip without attempting, record a skipped attempt in the trail.
2. Render. `render()` is the only source of payloads; never `build_payload` directly.
3. Attempt. On success, record the breaker success and return.
4. On `ProviderError`, branch on the flags — **never on `isinstance`, and never on `adapter.name`**:
   - `retryable_same_provider` → bounded retry against the same candidate (below).
   - `failover_eligible` → record the breaker failure, next candidate.
   - neither → abort the whole request immediately. Walking a `BadRequest` down three providers turns
     one fast failure into three slow ones and burns quota doing it.
5. Candidates exhausted, or the cap of 3 attempts hit → raise the last error; the endpoint's existing
   `to_app_error` translation handles the rest, unchanged.

**Same-provider retries, hand-rolled, not `tenacity`.** Two jittered retries, and only for
`Unavailable`; `EmptyResponse` gets exactly one. Never for `RateLimited` — that is the router's job, not
the retry's, and hammering a 429 is how a free-tier key gets banned. Hand-rolled because it is ~20 lines,
because a decorator fights the async-generator streaming path in Step 9, and because the overview calls
out the hand-rolled version as the better story.

**`ContextTooLong` is the one special case.** Not failover-eligible (a bigger history does not fit better
elsewhere, and the next candidate's window is usually smaller), but it has a fix: re-fit and retry once
against the same provider. Do it with `dataclasses.replace(spec, context_window=exc.limit_tokens)` and a
second `render()` call — no new parameter on `render()`, and the substitution is honest: the model's real
window is smaller than the config claimed. If the retry raises `ContextTooLong` again, give up.

**The attempt trail.** Define an `AttemptRecord` in `router.py` (it is what produces it) and serialize it
into `requests.attempts`:

```json
{"n": 1, "slot": "general", "provider": "groq", "model": "llama-3.3-70b-versatile",
 "outcome": "error", "error_code": "rate_limited", "latency_ms": 412,
 "wasted_tokens_out": 0, "breaker": "closed"}
```

`outcome` ∈ `ok | error | skipped_breaker`. This array is the answer to "why did this answer look
weird?", and it is the only place the answer will ever exist — one `messages` row per logical message
means the discarded attempts leave no other trace.

### Step 5 — Wire the router into the non-streaming endpoint *(half a day)*

> **In plain terms.** The chat endpoint currently says "call Groq". Change it to say "ask the router".
> That is genuinely most of the work — a small edit with a large consequence, which is what all those
> Phase 1 seams were for.
>
> **After this step.** The first thing you can actually show someone. Point two candidates at the
> `general` slot, break the first one, send a message — you still get an answer, and the response says
> which model produced it. Failover is real, end to end, through the API and into the UI.

`api/v1/chat.py` replaces `_resolve_spec` + `adapter.complete` with one router call. The order of
operations, the early commit of the user's message, the ownership scoping and the `to_app_error`
translation all stay exactly as they are. What changes:

- `meta` on the assistant message gets real `attempts` and `substituted`, and `provider_used`/
  `model_used`/`slot_used` come from the *serving* candidate, not the requested one.
- `usage/logger.record_success` and `record_failure` gain an `attempts` parameter for the trail, plus
  `substituted`. Its module docstring already predicted three call sites; this is the first of them.
- `ServedBy` in the response now sometimes differs from `requested_slot`. No schema change —
  `schemas/chat.py` shipped every one of these fields in Phase 1 for exactly this moment.

**Failover is demoable here**, before a single line of streaming exists — though only *across Groq's own
models* for the moment, since the other two adapters land in Step 6. Give the `general` slot a second
candidate (`llama-3.1-8b-instant`), revoke nothing, and force a failure by pointing the first candidate
at a model name that does not exist: the second one answers, and `served_by` says so. Milestone A is
complete one step later, when that same mechanism is spanning three genuinely different providers.

### Step 6 — Gemini and OpenRouter adapters *(3 days — Gemini is 2 of them)*

> **In plain terms.** Teaching the gateway two new languages. Every provider wants the same conversation
> in a different shape: Gemini calls the assistant `"model"` instead of `"assistant"`, puts the system
> prompt in a completely different place, and puts the model name in the URL rather than the body. An
> adapter is a translator with two directions — canonical conversation in, that provider's exact format
> out; their strange error responses in, your seven standard error types out. Every quirk gets trapped
> here, so nothing above this layer ever learns that Gemini is different.
>
> **After this step.** Three real providers, all passing the same test suite that Groq passes. Open the
> three golden payload files side by side: the Gemini one looks nothing like the other two, and that
> difference is the evidence that the abstraction is real rather than just OpenAI's shape wearing a hat.
> **Milestone A is complete** — genuine cross-provider failover, no streaming yet.

Write them against the conformance suite; it is already parameterized and the two new `AdapterCase`
entries are the whole registration.

**Gemini** — the shape-divergent one, and the reason the abstraction was built before Groq was:

| Concern | Shape |
|---|---|
| Endpoint | `POST /models/{model}:generateContent` — the model is in the *path*, so `_url()` is built per call |
| Auth | `x-goog-api-key` header, not `Authorization: Bearer` |
| System prompt | top-level `system_instruction` — `supports_system_field=True`, the first adapter to take that branch |
| Messages | `contents: [{role, parts: [{text}]}]`, assistant role is **`"model"`** |
| Params | `generationConfig: {temperature, maxOutputTokens, topP, stopSequences}` — none of them top-level |
| Usage | `usageMetadata.{promptTokenCount, candidatesTokenCount}` |
| Errors | `{"error": {"code", "message", "status"}}` — map on `status`: `RESOURCE_EXHAUSTED`→`RateLimited`, `INVALID_ARGUMENT`→`BadRequest`, `UNAUTHENTICATED`/`PERMISSION_DENIED`→`AuthFailed`, `UNAVAILABLE`/`INTERNAL`→`Unavailable`. `retryDelay` lives in `error.details[].RetryInfo`, not a header |
| Safety | `promptFeedback.blockReason`, or a candidate with `finishReason: "SAFETY"`/`"RECITATION"` → `ContentFiltered` |
| Empty | `candidates: []` with no block reason → `EmptyResponse` |
| Streaming | `:streamGenerateContent?alt=sse` — without `alt=sse` you get a JSON array, not an event stream |
| Quota | per Google Cloud *project*, not per key |

**Leave Gemini's native `inline_data` branch unbuilt** — raise `NotImplementedError` the way
`groq.py::_render_attachment` does. No `file_ref` can exist before Phase 4 (`POST /v1/files` does not
exist), so building it now means an untested branch pretending to be a capability, which is precisely
what `build_payload`'s docstring says the Groq adapter refused to do for the system-field branch. The
injected-text path is identical to Groq's and goes through the shared `document_envelope`.

**OpenRouter** — OpenAI-compatible, with four teeth:

- The `:free` suffix is part of the model name. Dropping it silently routes to a paid variant; a config
  test should assert every OpenRouter model in `providers.yaml` ends in `:free`.
- `HTTP-Referer` and `X-Title` attribution headers, from Step 1's `options`.
- **402 is `RateLimited`, not `AuthFailed`.** Credit exhaustion is a quota condition; classifying it as
  auth would fire the `alert` flag and page someone about a free tier working as designed.
- Upstream availability changes without notice, so `EmptyResponse` and `Unavailable` are ordinary here
  rather than exceptional. Its SSE stream also carries `: OPENROUTER PROCESSING` comment keepalives —
  see the idle-timeout trap in Step 7.

**Fixtures.** Extend `scripts/record_fixtures.py` to both providers and run it once with live keys, per
the hard rule. Each provider needs the set the suite's hygiene test enforces — `success`,
`success_no_usage`, `empty_response`, `models_list`, plus every error case in its `error_fixtures` map —
and, new in this phase, `stream_success.sse` and `stream_truncated.sse` (truncate a real recording by
hand; that is a legitimate `source: "synthetic"`). The redaction check in
`test_no_fixture_leaks_a_credential` needs a Gemini and an OpenRouter key prefix added to its assertions.

**Golden payloads.** `gemini_general.json` and `openrouter_general.json` from the same fixed six-message
`canonical_history()`. This is §2.2.6's cross-provider correctness test: one canonical history, three
committed payload files, and any adapter change that alters those diffs has to be deliberate. Expect the
Gemini golden to look startlingly unlike the other two — that is the artifact proving the abstraction is
real rather than OpenAI-shaped with extra steps.

### Step 7 — Upstream streaming: `base.py` plumbing + `groq.stream` *(1.5 days)*

> **In plain terms.** Until now the gateway asks a question and waits for the entire answer before doing
> anything. Streaming means reading the answer as it is being written — providers send it as a stream of
> small "here are the next few characters" messages. This step teaches the gateway to read that trickle
> and pass each piece along immediately rather than collecting them all first (collecting them defeats
> the entire point). It also teaches it to notice when the trickle *stops*: a provider that accepts your
> connection and then goes quiet has failed, even though nothing technically errored — that is the
> "slow but not down" case, and without a timer for it the request just hangs.
>
> **After this step.** You can pull tokens out of Groq one at a time in a test. Two tests in the
> conformance suite that have been skipping since Phase 1 start running by themselves, because they were
> written to activate the moment `stream` stopped raising `NotImplementedError`.

Shared, in `HttpProviderAdapter` (§3 designates `base.py` as "protocol + shared HTTP helpers", so this
is its slot — do not invent a `providers/sse.py`):

- An async iterator over SSE frames from `client.stream(...)`, handling multi-line `data:`, comment
  lines, and `[DONE]`.
- Idle-timeout enforcement: `asyncio.wait_for(anext(frames), idle_timeout)`, `TimeoutError` →
  `Unavailable("idle stall")`. The contract puts this obligation on `stream`, and putting it in one
  place is what stops adapter three from forgetting it.
- Mid-stream fault normalization: a truncated body is `httpx.RemoteProtocolError` → `Unavailable`; a
  half-written JSON frame must not escape as a `JSONDecodeError`. Conformance case 4 exists for exactly
  this, and it will start running the moment `stream` stops raising `NotImplementedError`.

Per-adapter: frame → `StreamChunk` mapping. Keep it in each adapter rather than sharing it between Groq
and OpenRouter — their frames genuinely differ (keepalive comments, where `usage` appears), and a shared
mapper would grow a provider flag, which is the leak `base.py`'s docstring warns about.

**Trap: the idle timeout measures gaps between *deltas*, not between bytes.** OpenRouter's keepalive
comments prove the connection is alive while telling you nothing about whether the model is generating.
Resetting the timer on a comment converts "provider stalled" into "request hangs for the full read
timeout", which is the "slow but not down" failure mode `DEFAULT_IDLE_TIMEOUT_S` was introduced to catch.

Conformance cases 3 and 4 activate automatically via `_implements_stream`. Two `pytest.skip`s turning
green with no test edit is the seam paying for itself.

### Step 8 — Server-side SSE framing *(half a day)*

> **In plain terms.** The mirror image of Step 7. That step read a trickle *from* a provider; this one
> writes a trickle *to* your own client, in the standard format browsers understand (Server-Sent Events
> — plain text lines like `event: delta` followed by `data: {...}`). The formatting itself is easy. The
> part that actually bites is the response headers: leave one out and a proxy will helpfully collect
> your entire stream and hand it over as a single lump, producing a gateway that passes every test and
> visibly does not stream in production.
>
> **After this step.** The machinery to push events to a browser exists. Nothing is driving it yet.

`app/streaming/sse.py`: `event:`/`data:` framing for the four §1.1 event types, a heartbeat, and
client-disconnect detection.

The response headers matter more than the framing does:

```
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
X-Accel-Buffering: no
```

`X-Accel-Buffering: no` is not optional — an nginx-family proxy will happily buffer the whole stream and
deliver it as one blob, producing a gateway that passes every test and does not stream in production.
Check the Next.js rewrite in `frontend/next.config.ts` for the same failure, and check it against the
deployed Fly instance rather than against `make dev`.

### Step 9 — The D1 orchestrator *(2.5 days — the hardest step in the phase)*

> **In plain terms.** The hard one, and the reason this phase is interesting. Tokens are already flowing
> onto the user's screen and the provider dies halfway through a sentence. You throw away the half
> answer, send the client a message meaning "forget everything I just said", and start over on a
> different provider — up to three times. Meanwhile you count the tokens you wasted, because the
> provider generated them and charged you for them even though nobody will ever read them.
>
> Almost all the difficulty is in the rules about when *not* to restart: never after you have said the
> message is finished, never when the failure would happen identically on every other provider, and
> never when the user simply closed their laptop (that is not a failure, it is a person leaving).
>
> **After this step.** Streaming that survives a provider dying mid-sentence. This is the single most
> impressive thing in the project and the centre of the Phase 7 chaos demo.

`app/streaming/orchestrator.py`, implementing §1.1's state machine verbatim:

1. Build candidates, emit nothing yet (D13).
2. Open the upstream stream. On the first chunk: emit `meta`, then the first `delta`. Accumulate every
   delta in an in-memory buffer.
3. On a mid-stream fault — reset, 5xx, mid-stream error frame, idle stall — abort upstream.
4. Record the tokens the failed attempt really generated as `wasted_tokens_out`. They were spent even
   though the text is discarded. Providers seldom report usage on an aborted stream, so estimate from
   the discarded text and mark it estimated. Phase 3 commits these against quota; Phase 2 only records
   them.
5. Open the breaker if the fault is breaker-eligible.
6. `attempt < 3` and a candidate remains → emit `restart` with `discarded_chars`, **clear the buffer**,
   reserve the next candidate, back to 2.
7. Attempts exhausted → `done` with `status: "failed"`, plus `partial_content` when the longest buffer is
   non-trivial, so the client can offer "keep partial answer".

The invariants, each of which is a test:

- **Never restart after `done`.** The terminal event makes the message final.
- **Never restart on `BadRequest`, `ContentFiltered`, or `ContextTooLong`.** The first two fail
  identically everywhere; the third re-fits and retries *once against the same provider*, and only
  before the first delta — mid-generation there is nothing to re-fit.
- **Client disconnect is not a fault.** Detect it, abort upstream, record the tokens, persist nothing
  further, do not restart. Handle the cancellation explicitly rather than letting it surface as a
  `CancelledError` traceback in the logs — an unremarkable event should not look like an incident.
- Mint the assistant `message_id` up front, since `meta` carries it. This needs an optional
  `message_id` kwarg on `messages_repo.append` so the row lands with the id already promised to the
  client. That is a repo signature change, not a contract change, and the alternative — a provisional id
  in `meta` corrected in `done` — is a client-side bug generator.

Write `stream:{message_id}:attempts` for observability; do not read it for control flow (D12).

### Step 10 — The collector *(1 day)*

> **In plain terms.** When a stream ends, the answer exists in two places: the user's screen, and a
> variable in memory that is about to be garbage collected. Nothing has been saved. This step writes it
> down — one message row containing the final assembled answer and the model that actually produced it,
> plus a record of every attempt that was made along the way.
>
> **After this step.** Refresh the page after a streamed answer and it is still there, with the correct
> model named underneath it. Without this step, streaming works beautifully and remembers nothing.

`app/streaming/collector.py`: after `done`, assemble the buffer into the assistant message and persist —
one `messages` row with `model_used` = the *final* serving model, one `requests` row with the full
attempt trail, `wasted_tokens_out` summed across discarded attempts, and `attempts`/`substituted` set.

Opens its own session from `app.state.db_session_factory` (D14). Failing to persist must not corrupt the
stream the user already received: log it loudly, do not raise into a finished response.

This is also where D5's cache write lands in Phase 3 — assemble after `done`, write to cache, replay
hits as a synthetic stream. Leave the seam obvious; do not build it.

### Step 11 — Frontend *(2 days)*

> **In plain terms.** The browser half. Read the event stream as it arrives, append characters to the
> message bubble as they come in, and — the part that matters — when a restart happens, **wipe the
> half-written bubble completely** and begin again. The tempting alternative is to try to be clever and
> stitch the two attempts together; that produces half an answer from one model welded to a full answer
> from another, which reads as a broken model rather than a client bug and is miserable to diagnose.
>
> **After this step.** You can watch the whole thing work. A provider failing mid-answer looks like a
> deliberate product behaviour — the bubble resets, the model name changes, the answer completes —
> instead of looking like a crash.

`frontend/lib/sse.ts`: implement `openCompletionStream` against the event types already declared there.
**`EventSource` cannot do this** — it is GET-only and cannot set an `Authorization` header. Use `fetch`
with `response.body.getReader()`, a `TextDecoder`, and manual frame parsing, with the existing
`AbortSignal` wired to the reader so a cancelled turn actually closes the upstream connection.

Reuse `api.ts`'s error handling: a pre-stream failure is still an error envelope, so `GatewayError` and
its `requestId` keep working unchanged (this is D13's payoff on the client side).

`lib/provenance.ts` gains `fromDoneEvent` — and, per `sse.ts`'s own note, that should be the *only*
addition, because `DoneEvent` was defined to be structurally identical to the non-streaming response's
provenance block. If `ModelIndicator` needs new props, something drifted.

Components: `PendingTurn` appends deltas; on `restart` it **clears the bubble entirely and swaps the
indicator** — never splice or diff two attempts, per the client-side contract in §1.1. `ModelIndicator`
renders `served_by` always, the mismatch when `substituted` (`Gemini Flash · llm2 was unavailable`), and
a subtle marker with the attempt trail on hover when `attempts > 1`. `Composer` gets a stream/no-stream
path; keep the non-streaming one working, since it is the fallback and Phase 3's cache-hit replay.

### Step 12 — Tests, ADRs, deploy *(2 days)*

> **In plain terms.** Prove it works, write down *why* it was built this way, and ship it. The ADRs are
> short documents — context, decision, consequences — one per real choice made in this phase. They are
> not bureaucracy: they are the portfolio artifact, because "here is what I chose and here is what it
> cost me" is a much stronger thing to show than working code alone.
>
> **After this step.** Green CI with zero live provider calls, seven decision records, and the whole
> thing running on the deployed URL — streaming properly through the real proxy, which is the one place
> the buffering trap actually shows up.

Covered in §6 and §7 below. Deployment specifics: Fly's proxy must not buffer the event stream, and the
free-tier instance sleeping mid-stream is a real failure mode worth a line in `docs/limitations.md`.

---

## 5. Traps

Collected from the contracts, the existing code's own warnings, and the shape of the work:

1. **Buffering the stream.** The single most likely way to ship a streaming feature that does not
   stream. Sources: an adapter that collects chunks before yielding, a proxy without
   `X-Accel-Buffering: no`, a frontend that awaits the whole response, and Python's own iteration if a
   generator is wrapped in `list()`. Test it end to end with `curl -N`, on the deployed URL.
2. **Holding a DB session across a stream.** Both a correctness bug (FastAPI's teardown) and a capacity
   bug (a free-tier pool exhausted by four concurrent chats).
3. **Branching on `adapter.name` in the router.** `base.py` names this exactly: if the router needs to
   know which provider it is talking to, something that belongs behind the interface has leaked.
   Everything the router needs is on the normalized error's class flags.
4. **Retrying a `BadRequest` down the chain.** Resilience-shaped bug. The flags exist to prevent it and
   are class attributes so no call site can override them.
5. **Counting `EmptyResponse` against the breaker.** Takes a working free-tier provider out of rotation
   for a condition its own docstring calls annoying rather than broken.
6. **Losing the discarded attempt's tokens.** They were really generated and really spent. Dropping
   them makes Phase 3's quota tracker wrong in the exact scenario it exists for, and the miscount is
   invisible until a key gets rate-limited earlier than predicted.
7. **A restart the client splices.** If `onRestart` appends instead of clearing, the user sees half an
   answer from one model welded to a full answer from another, and it will look like a model quality
   problem rather than a client bug.
8. **Idle timeout reset by keepalive comments.** See Step 7.
9. **Dropping OpenRouter's `:free` suffix.** Silently routes to a paid variant. Assert it in a config
   test rather than trusting the YAML.
10. **Gemini's `"model"` role.** Sending `"assistant"` gets a 400 that reads like a payload problem
    because it is one — the golden file is what catches this before a live call does.
11. **Re-blessing a golden file without reading the diff.** The goldens are the cross-provider
    correctness guarantee; a diff accepted because "the test was red" discards the guarantee silently.
12. **Feeding failed attempts into the latency table.** The inversion bug (D11): a provider 429ing in
    80ms measures as the fastest in the fleet, so `auto` learns to prefer whatever is most broken. The
    symptom is not an error — it is a gateway that gets *worse* the longer it runs, which is a miserable
    thing to diagnose after the fact. Update the series on success only, and let the test assert it.

---

## 6. Test matrix

| Layer | Approach |
|---|---|
| Adapter conformance | The existing parameterized suite, three cases. Cases 3 & 4 self-activate. |
| `parse_error` | Every recorded error fixture per provider → expected class *and* expected flags. Gemini's `status` strings and OpenRouter's 402 are the two that would otherwise be discovered in production. |
| Golden payloads | One canonical history → three committed files. §2.2.6. |
| Circuit breaker | `fakeredis` + injected clock. closed→open→half_open→closed; half_open failure → longer cooldown; the half-open probe is exclusive under 20 concurrent callers; Redis down → fails open (D9). |
| Router | Injected fake adapters with scripted failures. Candidate ordering, abort-on-`BadRequest`, `ContextTooLong` re-fit-and-retry-once, attempt cap, skip-on-open-breaker, cross-slot spill (D10), `auto` de-duplication (D11). Breaker skips do **not** consume attempts — assert a chain of three open breakers still reaches candidate four. |
| Latency ranking | Pure table tests over an injected snapshot: a faster candidate is promoted; a candidate under the sample threshold keeps its config position; a cold snapshot reproduces config order exactly; **failed attempts never update the series** (the inversion bug — assert a fast-failing provider does not get promoted); streaming and non-streaming rank on separate series; ranking never promotes a breaker-open candidate. |
| Streaming (adapter) | `MockTransport` serving recorded `.sse` files: ≥2 chunks, terminates, truncated → `Unavailable`, idle stall → `Unavailable`, keepalive comments do not reset the idle timer, and the 10s first-token budget fires independently of the 30s inter-token one (D13). |
| Streaming (orchestrator) | Scripted fault injection mid-stream: `restart` emitted with correct `discarded_chars`, buffer cleared, ≤3 attempts, never restarts after `done`, `ContentFiltered` terminates immediately, client disconnect aborts without persisting. |
| Streaming (endpoint) | Integration: `stream: true` end to end against a mock transport. Assert incremental delivery (chunks arrive before the response completes), the `meta`/`delta`/`done` sequence, and that a pre-first-delta failure returns a JSON error envelope rather than a 200 (D13). |
| Persistence | After a restarted stream: exactly one `messages` row, `model_used` = final serving model, `meta.attempts` = 2, `meta.substituted` correct; one `requests` row with a 2-element attempt trail and non-zero `wasted_tokens_out`. |
| Frontend | Existing `ModelIndicator` vitest suite extended for substitution and attempts; a `sse.ts` frame-parser unit test over a recorded event stream including a split-mid-frame chunk boundary. |

Coverage stays concentrated in `routing/`, `streaming/`, and `providers/*.parse_error` — the three places
where a bug is both likely and invisible.

---

## 7. Documentation

- **ADR-010** Redis-down asymmetry for the breaker (D9), noting the contrast with Contract C's
  quota rule and superseding ADR-009's readiness reasoning.
- **ADR-011** Named-slot spill (D10) — and why `substituted` would otherwise be unreachable.
- **ADR-014** Latency-based `auto` ranking (D11): the successful-attempts-only rule and the failure-bias
  it prevents, TTFT vs. total latency, why the table is in-process rather than in Redis (Contract C is
  frozen), and the standing caveat that ranking without a quota filter leans toward the provider nearest
  its limit until Phase 3 lands.
- **ADR-012** Mid-stream failover: the D1 restart, why the original plan's "never mid-stream" was
  overridden, and the 200-commit boundary (D13) that keeps pre-stream failures debuggable.
- **ADR-013** Hand-rolled breaker and retries over `tenacity`/a library.
- **ADR-015** The attempt cap (D12): why the in-process counter is authoritative, why
  `stream:{message_id}:attempts` is written and deliberately not read, and why a breaker skip is not an
  attempt.
- **ADR-016** Session lifetime on the streaming path (D14): the `yield`-dependency mismatch, the
  free-tier connection-pool argument, and the decision to log-and-swallow a post-`done` persistence
  failure.
- `docs/architecture.md`: the failover loop and the restart state machine as diagrams.
- `docs/limitations.md`: opened here — a restart discards tokens the free tier already charged; two
  attempts on very different free models produce visibly different answers; a sleeping free-tier
  instance can drop a stream mid-flight.

---

## 8. Exit checklist

- [ ] Revoke the Groq key mid-session → messages keep being answered by Gemini, then OpenRouter, and the
      UI names the model that actually served each one
- [ ] Breaker transitions visible in the logs; a re-enabled provider is probed once and closes
- [ ] `curl -N` with `stream: true` delivers tokens incrementally, from all three providers
- [ ] A provider killed mid-stream produces a `restart` event; the UI clears the bubble, swaps the
      indicator, and finishes cleanly with `attempts: 2`
- [ ] Three attempts exhausted → `done` with `status: "failed"` and a usable `partial_content`
- [ ] A pre-stream failure returns the JSON error envelope with a `request_id`, not a 200
- [ ] `requests.attempts` holds the full trail; `wasted_tokens_out` is non-zero after a restart
- [ ] Exactly one `messages` row per logical message, whatever the attempt count
- [ ] Three golden payload files committed; the Gemini one is visibly a different shape
- [ ] `BadRequest` aborts on the first candidate — assert it, don't assume it
- [ ] Breaker skips don't burn attempts: three open breakers still reach candidate four
- [ ] `auto` reorders toward the faster provider after warm-up, and a cold process reproduces config
      order exactly; a fast-failing provider is never promoted
- [ ] `make test` green, zero live API calls; `make lint` and `make typecheck` clean
- [ ] ADR-010…013 written

**Realistic duration:** 15–18 working days, or ~3 weeks part-time. The development plan's original
estimate was ~2 weeks; D1's restart state machine, the breaker arriving with Redis a phase early, three
sets of recorded fixtures, and D11's latency ranking pulled forward from the stretch backlog are what
account for the difference. Steps 9 and 6 are where the time actually goes; latency ranking is about a
day, most of it in the tests that pin down the failure-bias rule.

---

## 9. What Phase 2 hands to Phase 3

Left deliberately unbuilt, with the seam visible:

- `quota/` is still empty; `cache/keys.py` already has every builder it will need.
- The router's candidate filter is a single insertion point in `selection.candidates()` — Phase 3 filters
  by remaining quota *before* the first attempt, and a 429 you predicted is a round trip you did not
  spend. It runs *before* D11's latency sort, which is also the fix for that decision's standing caveat:
  ranking by speed stops favouring the provider nearest its ceiling once exhausted candidates are
  removed from the list rather than merely losing a race.
- The latency table is per-process (D11). If Phase 6+ ever runs more than one instance and the ordering
  visibly diverges between them, sharing it needs a new Contract C key format — a frozen-contract change
  requiring sign-off, deliberately not taken in Phase 2.
- `ModelSpec.reserved_fraction` is still 0.0 everywhere; D8's 50/50 Gemini split is read by
  `quota/lanes.py`, which does not exist.
- `QuotaHint` from `rate_limit_headers` is implemented on every adapter and read by nobody — Phase 3
  uses it to correct drift against ground truth.
- `Usage.estimated` is populated correctly throughout, including for the estimated `wasted_tokens_out`
  of a discarded attempt, which is the one number Phase 3's reconciliation cannot recover after the fact.
- The collector's post-`done` assembly is where D5's stream caching attaches.
