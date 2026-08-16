# Development Plan: Free-Tier Multi-Provider LLM Gateway

A build plan derived from the project overview. Organized as: decisions to lock first → skeleton to stand up → seven shippable phases, each with tasks, exit criteria, and a demo you can point at.

**Guiding rule for the whole build:** every phase ends with something deployed and demoable. No phase is "refactoring week." If a phase can't be demoed, it's scoped wrong.

---

## 0. Before You Write Code: Lock These Decisions

These are the questions the overview flags as open. Deciding them now (and writing the reasoning down in `docs/decisions/`) prevents mid-build churn, and the decision log itself is a portfolio artifact.

| # | Decision | Recommended default | Why |
|---|---|---|---|
| D1 | Mid-stream failover | **Only fail over between messages, never mid-stream.** If a stream dies after first token, surface a clean error with the partial content. | Restarting mid-stream produces incoherent output and duplicated tokens. Simplest defensible behavior. |
| D2 | Specific slot exhausted | **Error with a structured payload** (`{error: "slot_unavailable", slot: "llm2", retry_after_s: 420, suggestion: "auto"}`) rather than silent substitution. | Explicit user intent beats availability. Structured error lets the frontend offer one-click "use auto instead." |
| D3 | Tool-call history across providers | **Out of scope for v1.** On first tool call, set `conversations.pinned_model`; subsequent turns ignore slot selection and return a `model_pinned` warning field. | Documented limitation > broken translation. |
| D4 | Context-window overflow on switch | **v1: truncate oldest non-system messages** with a `[N earlier messages omitted]` marker. Summarization is Phase 7+ stretch. | Truncation is testable; summarization costs quota and adds a failure mode. |
| D5 | Caching + streaming | **Cache only non-streaming responses in v1.** For streaming, buffer the assembled response *after* the stream completes and write it to cache; serve cache hits as a synthetic stream. | Gives you a real answer to the "how do you cache a streaming response?" interview question. |
| D6 | Idempotency | Accept an optional `Idempotency-Key` header; store `(api_key_id, idem_key) → request_id` in Redis for 24h. Replays return the stored response without re-spending quota. | Directly answers the retry question in §13 of the overview. |
| D7 | Auth model | Gateway-issued API keys (`gw_live_...`), stored as SHA-256 hashes; a lightweight session/JWT layer only if you build the Settings UI. | Keeps Phase 1 tiny; BYOK page in Phase 6 can reuse it. |
| D8 | Gemini quota split | Reserve a hard percentage of Gemini's daily budget for the perception lane (start at 70%). Chat routing sees only the remaining 30% as available. | Prevents plain chat from starving file understanding — implement as a `reserved_fraction` column, not a hardcoded rule. |

Write each as a short ADR (context / decision / consequences). Eight files, ~15 lines each.

---

## 1. Repo Structure

Set this up in the first hour; every later phase drops into an existing slot.

```
llm-gateway/
├── app/
│   ├── main.py                  # FastAPI app factory, lifespan, router mounts
│   ├── config.py                # pydantic-settings; all env vars typed
│   ├── api/
│   │   ├── v1/chat.py           # POST /v1/chat/completions
│   │   ├── v1/models.py         # GET /v1/models
│   │   ├── v1/files.py          # POST /v1/files
│   │   ├── keys.py              # BYOK settings endpoints (Phase 6)
│   │   └── admin.py             # usage dashboard data (Phase 7)
│   ├── core/
│   │   ├── auth.py              # API key verification dependency
│   │   ├── errors.py            # exception → HTTP mapping, error envelope
│   │   └── logging.py           # structlog JSON config, request_id binding
│   ├── providers/
│   │   ├── base.py              # ProviderAdapter protocol
│   │   ├── gemini.py
│   │   ├── groq.py
│   │   ├── openrouter.py
│   │   └── registry.py          # slot → adapter mapping, capability lookup
│   ├── routing/
│   │   ├── router.py            # candidate list building, failover loop
│   │   ├── circuit_breaker.py
│   │   └── selection.py         # auto vs. specific-slot policy
│   ├── quota/
│   │   ├── tracker.py           # RPM/RPD/TPM counters, reserve/commit
│   │   └── windows.py           # per-provider reset semantics (rolling vs fixed-time)
│   ├── perception/
│   │   ├── lane.py              # native → extract → local fallback chain
│   │   ├── extractors.py        # Gemini extraction prompts
│   │   └── local.py             # PyMuPDF / pdfplumber / Tesseract
│   ├── memory/
│   │   ├── canonical.py         # canonical message schema
│   │   └── adapters.py          # canonical → provider payload
│   ├── cache/
│   │   ├── exact.py
│   │   └── keys.py              # centralized Redis key builders
│   ├── db/
│   │   ├── models.py            # SQLAlchemy 2.0 async models
│   │   └── repo/                # one module per table group
│   └── keys/
│       └── resolver.py          # resolve_provider_key(), encryption helpers
├── tests/
│   ├── unit/
│   ├── integration/             # httpx mock transports, fakeredis
│   └── conftest.py
├── alembic/
├── docs/
│   ├── decisions/               # ADRs from §0
│   ├── architecture.md          # diagrams
│   └── limitations.md           # the honest-edges doc
├── docker-compose.yml
├── Dockerfile
└── Makefile                     # make dev / test / lint / migrate
```

---

## 2. Foundational Contracts

Define these three before Phase 1 — everything else is an implementation of them.

### 2.1 Provider adapter protocol

```python
class ProviderAdapter(Protocol):
    name: str

    def build_payload(self, msgs: list[CanonicalMessage],
                      model: str, params: GenParams) -> dict: ...

    async def complete(self, payload: dict, key: str) -> Completion: ...

    async def stream(self, payload: dict, key: str) -> AsyncIterator[Chunk]: ...

    def parse_error(self, exc: Exception) -> ProviderError:
        """Normalize to: RateLimited(retry_after) | Unavailable | BadRequest
        | AuthFailed | ContextTooLong. The router only ever sees these."""

    def estimate_tokens(self, payload: dict) -> int: ...
```

Error normalization is the load-bearing part. Providers signal exhaustion inconsistently (429 body shapes, `retry-after` header presence, OpenRouter's 402 for credit issues). Normalize at the edge so the router logic stays provider-agnostic.

### 2.2 Canonical message

```python
CanonicalMessage = {
  "role": "user" | "assistant" | "system",
  "content": [ {"type": "text", "text": str}
             | {"type": "file_ref", "file_hash": str, "extracted": str | None} ],
  "meta": {"provider_used": str|None, "model_used": str|None, "tokens": int|None}
}
```

Store this in Postgres. Never store a provider's request body. The `file_ref` block is what lets the same history render as native multimodal for Gemini and as injected text for Groq.

### 2.3 Redis key schema

Centralize in `cache/keys.py` — scattered f-strings become unmaintainable fast.

```
q:{scope}:{provider}:{model}:rpm      → counter, TTL 60s
q:{scope}:{provider}:{model}:rpd      → counter, TTL = seconds until provider's reset
q:{scope}:{provider}:{model}:tpm      → counter, TTL 60s
cb:{provider}:{model}                 → hash {state, failures, opened_at}
cache:exact:{sha256}                  → JSON response, TTL 1h
extract:{file_hash}                   → extracted text (also mirrored to Postgres)
idem:{api_key_id}:{key}               → request_id, TTL 24h
ratelimit:{api_key_id}:{window}       → counter
```

`{scope}` is `system` or a `user_id` — this is what makes §9.4's quota branching work without double-counting.

---

## 3. Phase-by-Phase Plan

Effort estimates assume part-time work (~10–12 hrs/week). Adjust the ratios, not the ordering.

---

### Phase 1 — Single-Provider Proxy *(~1 week)*

**Goal:** a deployed endpoint that takes an OpenAI-shaped request, calls Groq, logs it, returns a response.

**Tasks**
1. FastAPI skeleton, `config.py` with typed env vars, structured JSON logging with a `request_id` bound per request.
2. Postgres via Supabase/Neon; Alembic migration for `api_keys`, `requests`, `conversations`, `messages`.
3. API key auth dependency: hash lookup, 401 on miss, attach `api_key_id` to request state.
4. `providers/groq.py` implementing the full adapter protocol (including `parse_error`) — resist the temptation to inline it; the protocol is the whole point.
5. `POST /v1/chat/completions` non-streaming. Persist request + response rows.
6. Dockerfile + docker-compose (app, Postgres, Redis) + deploy to Fly.io/Render.
7. CI: ruff, mypy, pytest on push.

**Exit criteria**
- `curl` against the deployed URL with a valid key returns a completion; invalid key returns 401.
- A `requests` row exists with tokens, latency, and status populated.
- Killing the Groq key produces a clean 502 with your error envelope, not a stack trace.

**Trap to avoid:** building the abstraction "later." Write `base.py` first, then make Groq conform to it.

---

### Phase 2 — Multi-Provider Core + Failover + Streaming *(~2 weeks)*

**Goal:** three providers behind one interface, automatic failover, working SSE.

**Tasks**
1. Implement `gemini.py` and `openrouter.py`. Gemini is the awkward one — `system_instruction` is a top-level field, `contents` uses `parts`, roles are `user`/`model`. This is exactly the mismatch §4.7 predicts; let it push shape into `memory/adapters.py`.
2. `providers/registry.py`: config-driven slot table (`llm1..llmN` → provider+model+priority), loaded from YAML so adding a provider is a config change.
3. **Circuit breaker** — hand-rolled, per `(provider, model)`:
   - `closed` → normal. N consecutive failures (default 5) or a 429 → `open`.
   - `open` → skip this candidate entirely for `cooldown` seconds (from `retry-after` when present, else exponential 30s→300s).
   - `half_open` → allow exactly one probe request; success closes, failure re-opens with a longer cooldown.
   - State in Redis so it's shared across app instances. Log every transition.
4. Failover loop in `router.py`: build ordered candidate list → for each, check breaker → attempt → on `RateLimited`/`Unavailable` continue, on `BadRequest` abort immediately (retrying a malformed request against every provider is a bug, not resilience).
5. Streaming: `httpx` streaming client → normalize each provider's chunk format to OpenAI SSE deltas → `StreamingResponse`. **Do not buffer.** Per D1, failover only happens before the first byte is sent; once streaming starts you're committed.
6. Retry policy inside a single provider attempt: `tenacity`, 2 retries, jittered backoff, only on connection errors and 5xx — never on 429 (that's the router's job).

**Exit criteria**
- Revoke the Groq key mid-test; requests transparently succeed via Gemini, and logs show the breaker opening.
- `stream: true` yields tokens incrementally in `curl -N` with no buffering.
- Integration test: mock transport returns 429 for provider A, 200 for provider B; assert B served it and `requests.provider` reflects that.

---

### Phase 3 — Quota-Aware Routing *(~1.5 weeks)*

**Goal:** the router stops guessing and starts knowing.

**Tasks**
1. `quota/tracker.py` with a **reserve → commit/release** pattern:
   - *Reserve* before the call: atomically increment RPM/RPD and reserve estimated tokens (Lua script for atomicity).
   - *Commit* after: adjust the token counter to actual usage from the response.
   - *Release* on failure: decrement so a failed attempt doesn't burn budget.
   
   Naive post-hoc counting undercounts under concurrency and lets you blow through limits — say this out loud in the README.
2. `quota/windows.py`: per-provider reset semantics. Rolling 60s windows for RPM/TPM; RPD needs per-provider config — a fixed daily reset in a specific timezone is *not* the same as a rolling 24h window, and modeling it wrong makes your "available" status wrong for hours.
3. Router integration: filter candidates by remaining quota *before* attempting. A 429 you predicted is a 429 you didn't waste a round-trip on.
4. Gemini reservation (D8): apply `reserved_fraction` so the answer lane sees a reduced budget.
5. `GET /v1/models` returning slot list with live `status` (`available` / `rate_limited` / `unavailable`) plus `resets_at`.
6. Slot selection policy in `selection.py`: `auto` → priority-ordered filtered list; specific slot → single-candidate list, and if it fails quota check, raise the D2 structured error.
7. Your own per-key rate limiting (sliding window in Redis) — independent of upstream limits.
8. Exact-match cache (D5): SHA-256 of `(model_slot, canonical messages, temperature, max_tokens)`; skip when `temperature > 0`; `X-Cache: HIT|MISS` response header.

**Exit criteria**
- Hammer one slot until exhausted; `/v1/models` flips it to `rate_limited` with an accurate `resets_at`, and `auto` routes around it.
- Requesting the exhausted slot explicitly returns the D2 error, not a silent substitution.
- Two identical `temperature: 0` requests: second returns `X-Cache: HIT`, no `requests` row with a provider call.

---

### Phase 4 — Perception Lane *(~2 weeks)*

**Goal:** upload a PDF or image; every model can "see" it.

**Tasks**
1. `POST /v1/files` — accept multipart, enforce size/MIME allowlist, compute SHA-256, store bytes in Supabase Storage (or local volume), return `file_hash`. Content-sniff the actual type; don't trust the client's declared MIME.
2. Chat requests accept `file_refs: [hash]` alongside messages.
3. Three-tier chain in `perception/lane.py`:
   - **Tier 0 — cache:** `file_extractions` lookup by hash. Free, instant.
   - **Tier 1 — native passthrough:** answering model supports this MIME (`capability_registry`) *and* has multimodal quota → attach directly, no extraction.
   - **Tier 2 — extraction:** route to Gemini with a structured extraction prompt (verbatim text, then table/chart/figure descriptions, then a summary). Store result with `extraction_confidence` and `extracted_by_provider`.
   - **Tier 3 — local:** PyMuPDF text layer for PDFs; Tesseract for images; if a PDF's text layer is empty, rasterize pages and OCR them. Mark `extraction_confidence: "low"`.
4. Prompt injection of extracted text: a clearly delimited block (`<document name="..." source="extracted">...</document>`) rather than raw concatenation, so the model can distinguish document content from user instruction. Truncate to fit the target model's context, keeping the head and the summary.
5. Populate `capability_registry` (supports_vision, supports_pdf, max_file_size) from a checked-in YAML, with a startup validation check.

**Exit criteria**
- Same PDF asked about via `llm2` (text-only Groq) and `llm1` (Gemini): both answer correctly; logs show extraction for the first, native passthrough for the second.
- Re-upload the same file → cache hit, zero quota spent.
- Disable Gemini entirely → local OCR path still produces a usable (degraded) answer, and the response carries a `degraded: true` flag.

**Trap:** don't let extraction failures fail the whole request. Every tier failure logs and falls through; only Tier 3 failure surfaces an error.

---

### Phase 5 — Memory & Cross-Provider Translation *(~1.5 weeks)*

**Goal:** conversations survive provider switches.

**Tasks**
1. Persist canonical history; `conversation_id` in requests loads prior turns.
2. Per-provider adapters materialize canonical → provider payload. Test matrix: for each provider, assert a fixed 5-message canonical history (with system prompt + file_ref) produces the correct payload shape. This is where Gemini's top-level `system_instruction` vs. OpenAI's in-array `role: "system"` gets handled once, correctly.
3. Context-window fitting (D4): token-count the assembled history against the target model's limit; drop oldest non-system messages until it fits; insert an omission marker.
4. Tool-call pinning (D3): if a message has tool-call content, set `pinned_model`; router honors it and the response includes `{"warning": "conversation pinned to llm2 due to prior tool use"}`.
5. Write `docs/limitations.md` explaining *why* tool-call translation is out of scope — schema incompatibility, no lossless mapping for parallel calls, and the fact that production gateways handle it by pinning too.

**Exit criteria**
- Start a conversation on `llm2`, switch to `llm1` mid-thread, ask "what did I say first?" → correct answer.
- Feed a 200-message history to a small-context model → truncation happens, no provider error.

---

### Phase 6 — BYOK Settings *(~1.5 weeks)*

**Goal:** §9 implemented end to end.

**Tasks**
1. Migration: `provider_keys` with all §9.9 columns; `user_quota_allocations`; add `quota_scope` to quota state.
2. Encryption at rest — Fernet with a key from env (or KMS if available). Never log, never return; store `last_4` separately for display.
3. `resolve_provider_key(user_id, provider)` per §9.3, called on **every** request. No caching at login — that's what makes §9.6's live effect fall out for free.
4. Quota branching per §9.4: shared path checks personal cap *and* system pool; private path checks only provider limits scoped to that user.
5. Validation endpoint: minimal live call (models-list where available, else 1-token completion), heavily rate-limited (e.g. 5/hour/user), stores nothing on failure.
6. `/v1/models` personalization (§9.7): merge user-key capabilities into the slot list so a private Pro key surfaces a slot others don't see.
7. Settings UI: one row per provider, masked display, Add/Remove, plain-language data-terms disclosure.

**Exit criteria**
- Add a personal Gemini key mid-conversation; the very next message uses it, and quota counters move under `quota_scope=<user_id>` rather than `system`.
- Remove it; next message reverts to shared pool with no session refresh.
- Submit a garbage key → clear error, nothing persisted.
- Grep all logs for the key string → zero hits.

---

### Phase 7 — Polish & Portfolio *(~1.5 weeks)*

This phase is where the interview value gets realized. Don't skip it for more features.

**Tasks**
1. **Usage dashboard:** request volume over time, provider distribution, error rate, cache hit rate, quota utilization per provider, simulated cost (§4.8) using a checked-in price table. Streamlit or a simple React page against `/admin/*`.
2. **README:** architecture diagram, the two-lane diagram, a request-flow walkthrough, and a "Design Decisions" section linking each ADR. Include the failure-mode table — what happens when each component dies.
3. `/metrics` in Prometheus format: request counts by provider/status, latency histogram, breaker state gauge, quota-remaining gauge.
4. **Load/chaos demo:** a script that fires concurrent requests while randomly killing providers, with a recording or screenshots showing graceful degradation. This is your single most persuasive artifact.
5. Idempotency (D6) if not already done.
6. **Message pagination for long conversations:** `GET /v1/conversations/{id}` currently loads the full
   thread unpaginated via `messages_repo.list_for_conversation`, and the frontend renders it in one flat
   list with no windowing — fine at portfolio scale, but payload size and DOM node count both grow
   unbounded with thread length. Add a **second** repo function, keyset-paginated on `seq` (`WHERE seq <
   :before_seq ORDER BY seq DESC LIMIT :n`) — `seq` is already a per-conversation, gap-free, monotonic
   counter (Contract B invariant 2) and `messages(conversation_id, seq)` is already indexed, so this is a
   cursor for free rather than new infra. **Do not touch `list_for_conversation` itself** — it stays
   unpaginated because D4's fitting step needs the *complete* history to decide what to truncate for the
   provider, and that need is independent of how much the UI has paginated into view. Frontend: load the
   latest N messages on open (oldest-first within the page, matching how Slack/Discord/ChatGPT do it),
   fetch older pages on scroll-up via `has_more`/`next_before_seq`, and turn `useConversation` from a
   single fetch into paginated state.
7. `docs/limitations.md` finalized — the honest-edges document.

---

## 4. Testing Strategy

Don't test against live providers in CI; you'll burn quota and get flaky runs.

| Layer | Approach |
|---|---|
| Provider adapters | `httpx.MockTransport` with **recorded real responses** (capture once, commit as fixtures). Include real 429 bodies from each provider — error parsing is where bugs hide. |
| Quota tracker | `fakeredis`; concurrency test firing 50 parallel reserves, asserting no overshoot. |
| Circuit breaker | Time-mocked state machine tests: closed→open→half_open→closed, and half_open failure → longer cooldown. |
| Router | Injected fake adapters with scripted failures; assert candidate ordering and abort-on-BadRequest. |
| Adapters (memory) | Golden-file tests: one canonical history → three expected payload shapes. |
| Perception | Fixture PDF (text layer), scanned PDF (no text layer), PNG; assert correct tier selection with each provider disabled in turn. |
| E2E | Docker-compose smoke test hitting a stubbed provider server. |

Target ~70% coverage, concentrated in `routing/`, `quota/`, and `providers/*.parse_error`.

---

## 5. Risk Register

| Risk | Impact | Mitigation |
|---|---|---|
| Free-tier limits change mid-build (§7 numbers are a snapshot) | Routing assumptions break | Keep all limits in one YAML config, never hardcoded. Add a startup log line printing the loaded limits. |
| Gemini quota consumed by chat, starving file uploads | Core feature degrades | D8 reservation, implemented in Phase 3 *before* Phase 4 needs it. |
| Fly.io/Render free tier sleeps → cold starts | Demo looks broken | Accept it; add a keep-alive ping before demos and note it in the README. |
| Scope creep into tool calls / semantic caching | Phases 5–7 never ship | They're explicitly in the stretch backlog. Ship Phase 7 first. |
| Free-tier training-data terms on uploaded files | Real privacy issue | Visible UI disclosure (§10); consider a per-conversation "no third-party extraction" toggle that forces local-only parsing. |
| Provider returns 200 but garbage/empty | Silent quality failure | Validate non-empty response; treat empty completion as `Unavailable` and fail over. |

---

## 6. Suggested Timeline

| Weeks | Phase | Milestone |
|---|---|---|
| 1 | 1 | Deployed single-provider proxy |
| 2–3 | 2 | Three providers, failover, streaming |
| 4–5 | 3 | Quota-aware routing + `/v1/models` |
| 6–7 | 4 | Perception lane working end to end |
| 8–9 | 5 | Cross-provider conversation memory |
| 10–11 | 6 | BYOK |
| 12–13 | 7 | Dashboard, README, chaos demo |

~13 weeks part-time. A viable portfolio cut exists at the end of Phase 4 (weeks 1–7) if you need something presentable sooner — Phases 5–7 deepen it rather than complete it.

---

## 7. Stretch Backlog (post-v1)

Ordered by value-per-effort:

1. Latency-based routing — track p50 per provider over a rolling window, prefer fastest among available.
2. Summarization-based context compression (replaces D4 truncation).
3. Semantic cache via embedding similarity.
4. Tool-call translation for a *narrow* subset (single, non-parallel calls between two OpenAI-shaped providers) — a deliberate partial solve, framed as such.
5. Prompt-complexity heuristic for `auto` (short → fast provider, long/complex → highest-capability available).
6. Multi-region deploy with Redis replication.

---

## 8. Definition of Done for v1

- [ ] Deployed, publicly reachable, with a demo API key
- [ ] Three providers, automatic failover, circuit breaker with observable state
- [ ] Streaming that doesn't buffer
- [ ] Per-provider/per-model quota tracking with correct reset windows
- [ ] `auto` and specific-slot selection with live status
- [ ] File upload answered correctly by a text-only model
- [ ] Conversation continuity across a provider switch
- [ ] BYOK with per-provider granularity, live effect, encrypted at rest
- [ ] Usage dashboard
- [ ] README with architecture diagrams + 8 ADRs + limitations doc
- [ ] Chaos demo recording
