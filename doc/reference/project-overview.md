# Project Overview: Free-Tier Multi-Provider LLM Gateway

*A self-contained project brief. Paste this into a new conversation to get full context without re-explaining anything.*

---

## 1. Elevator Pitch

A backend service that sits between client applications and multiple LLM providers (Gemini, Groq, OpenRouter, etc.), giving them a single, stable API to talk to — while the gateway handles provider selection, automatic failover when a provider's free-tier quota runs out, file/image understanding even when the currently active model doesn't support it natively, and lets the user pick "auto" or a specific model.

The entire system is designed to run **completely free**, using only the free tiers of multiple LLM providers plus free-tier hosting/infra. The interesting engineering problem is *not* "call an LLM API" — it's reliably orchestrating several independent, rate-limited, differently-shaped free services into something that behaves like one reliable product.

This is a portfolio/learning project intended to demonstrate backend engineering skill (API design, async processing, caching, rate limiting, circuit breakers, data modeling) combined with applied AI engineering skill (multi-provider abstraction, multimodal handling, prompt/context management).

---

## 2. Problem Statement

If you build a product on a single LLM provider:
- You're stuck when that provider has an outage or rate-limits you.
- Switching providers later means rewriting integration code.
- You can't easily get more free usage than one provider's free tier allows.
- Not all providers offer generous (or any) free multimodal/file-upload support — so file-heavy use cases are hard to support without paying.

This project solves that by acting as a unifying, provider-agnostic layer with automatic failover, and by decoupling "which model answers" from "which model understands files," so the system's file-handling capability isn't bottlenecked by whichever provider happens to be active for chat at that moment.

---

## 3. Core Design Philosophy

- **The gateway owns state, not the providers.** LLM APIs are stateless per-request — they don't remember prior turns. All conversation memory lives in the gateway's own database, in a canonical format, and gets translated into whatever shape the currently-targeted provider expects on every call.
- **Always have a fallback, at every layer.** Provider fails → try next provider. Native file support unavailable → extract via a capable model → if that's also unavailable, fall back to local non-LLM parsing (OCR/PDF text extraction). This "always degrade gracefully, never just fail" principle applies consistently across the whole system.
- **Separate concerns: answering vs. perceiving.** Which model generates the chat response and which model (if any) is used to understand an uploaded file are two independent decisions, each with their own fallback chain.
- **Be honest about the edges.** Some things (e.g., translating tool-calls across providers with incompatible schemas, or guaranteeing consistent answer quality when rotating between very different free models) are hard, unsolved-in-full problems. The project explicitly scopes these rather than pretending to solve them — and documents *why*.

---

## 4. Feature Set

### 4.1 Multi-provider routing with automatic failover
- Client sends requests to one unified endpoint (OpenAI-style `POST /v1/chat/completions` shape is a reasonable default, since most SDKs already speak it).
- Gateway maintains a priority-ordered list of providers/models it can route to.
- If the currently targeted provider/model returns a rate-limit error (HTTP 429) or is down, the gateway automatically retries against the next available option — invisibly to the client, unless the client asked for a *specific* model (see §8).
- Implemented via a **circuit breaker** (open/closed/half-open states) rather than naive immediate retries, to avoid hammering a provider that's clearly unavailable.

### 4.2 Streaming
- Proxy Server-Sent Events / chunked responses through the gateway's own endpoint without buffering the full response first.
- Needs to work correctly even when a mid-conversation failover happens (edge case worth explicitly deciding: do you restart the stream cleanly on a new provider, or only fail over on the *next* message? Simplest and most defensible choice: only fail over between messages, not mid-stream.)

### 4.3 Rate limiting & quota tracking (heterogeneous across providers)
Different providers measure limits differently — RPM (requests/minute), RPD (requests/day), TPM (tokens/minute), and sometimes TPD (tokens/day) — and hitting *any one* of them triggers a 429, independent of the others. The gateway needs a **per-provider, per-model budget tracker**, not a single global counter.
- Backed by Redis, with TTL/reset windows matching each provider's actual reset schedule (e.g., some providers reset daily quotas at a fixed time in a specific timezone, not on a rolling 24h window — this must be modeled per-provider, not assumed).
- Also enforce your *own* per-API-key rate limits for whoever is calling your gateway, independent of the upstream providers' limits.

### 4.4 Caching
- **Exact-match caching**: hash the request (model + messages + params) and cache identical requests for a TTL. Skip caching when `temperature > 0` (or make it configurable) since identical inputs won't reliably produce identical high-value outputs.
- **Extraction caching** (see §4.5): cache file-extraction results by file hash so re-uploading the same file doesn't cost quota twice.
- Stretch goal: semantic caching (cache near-duplicate requests via embedding similarity, not just exact hash match).

### 4.5 File/multimodal handling — the "perception lane"
Not every free-tier model/provider supports file or image input, and even those that do have separate, often stricter, quotas for multimodal requests. Design:

1. **Native passthrough (preferred path):** If the model currently active for this conversation supports the file type natively *and* has quota remaining, send the file directly — no extraction needed.
2. **Extraction fallback:** If not, route the file to a **dedicated multimodal-capable provider** (Gemini is the natural choice — its free tier includes native image/PDF/long-document understanding with a very large context window, which most other free tiers don't match). That provider extracts a structured text description (OCR + content summary for images; text + table/chart summary for PDFs). This extracted text is then injected into the prompt sent to whichever model is actually answering — so a text-only model can still "see" the file's contents indirectly.
3. **Local fallback (last resort):** If even the dedicated extraction provider is out of quota, fall back to non-LLM local tools — Tesseract OCR for images, PyMuPDF/pdfplumber for text-based PDFs. Lower quality, but keeps the system functional instead of failing outright.
4. **Caching:** extraction results are cached by file hash, so the same file uploaded again doesn't re-spend quota.

**Important resource-allocation subtlety:** if the same provider (e.g., Gemini) is used both as a normal chat-answering option *and* as the dedicated file-extraction backend, both uses draw from the *same* daily quota. Recommended approach: deprioritize that provider in the general chat-answering rotation and reserve most of its quota for the file-understanding role it's uniquely good at — let other free text-only providers absorb most of the plain-chat traffic.

### 4.6 Model selection interface (auto + manual)
See §8 — this is significant enough to warrant its own section.

### 4.7 Conversation memory & cross-provider translation
- All messages are stored in the gateway's **own canonical schema** (role + content, provider-agnostic), never in any single provider's exact request format.
- On every outbound call, a **per-provider adapter** translates the canonical history into that provider's expected shape. Necessary because providers disagree on structure — e.g., some put a system prompt as a message with `role: "system"` inside the array, others take `system` as a separate top-level field entirely outside the messages array.
- **Explicitly scoped-out hard case:** translating *tool/function-call* history across providers with incompatible schemas is a genuinely hard, largely-unsolved problem even in production gateways. Reasonable scope for this project: support free-flowing provider switching for plain text conversations; either pin the provider for the remainder of a conversation once a tool call occurs, or clearly document the limitation. Being able to articulate *why* this was scoped out is a stronger interview answer than pretending it's solved.
- **Context-window mismatches:** when switching to a provider/model with a smaller context window mid-conversation, either truncate oldest messages first (simple) or summarize older turns into a compact system message (more advanced, optional stretch goal).

### 4.8 Usage tracking / "cost" tracking
Since everything runs on free tiers, there's no real dollar cost — but tracking *simulated* cost (as if paid rates applied) plus real quota consumption per user/key is still valuable: it's the feature that would let this design scale into a paid/hybrid version later, and it's a strong thing to demo (a small usage dashboard showing request volume, provider distribution, and error rates per key).

---

## 5. Architecture

### 5.1 High-level request flow

```
Client → Auth (API key) → Rate limiter (your own limits)
   → [Model selection: auto or specific] → Router
   → Quota check per candidate provider/model
   → Provider call (streaming, with retry/circuit breaker)
   → Usage logger → Response
```

### 5.2 Two lanes, one system

```
                    ┌───────────────────┐
  file upload   →   │  PERCEPTION LANE   │ → extracted text (cached by file hash)
                    │  (Gemini-first,     │
                    │  local OCR fallback)│
                    └───────────────────┘
                              │
                              ▼
  user message  →   canonical conversation  →  ┌────────────────┐
  + extracted text       history               │  ANSWER LANE    │  → response
                                                │ (auto-routes or │
                                                │  user-selected  │
                                                │  provider/model)│
                                                └────────────────┘
```

---

## 6. Data Model (minimal, extend as needed)

- **`api_keys`** — id, user_id, rate_limit_tier, created_at
- **`conversations`** — id, user_id, created_at, pinned_model (nullable — set if a tool call has locked the conversation to one provider), preferred_model (the user's current selection: `"auto"` or a specific slot)
- **`messages`** — id, conversation_id, role (user/assistant/system), content (canonical format), provider_used, model_used, token_count, created_at
- **`providers`** — name, base_url, priority_order, health_status, supports_streaming
- **`capability_registry`** — provider, model, supports_vision, supports_pdf, max_file_size, notes (kept current manually or via periodic health checks, since these change often)
- **`provider_keys`** — id, owner_type (`system` | `user`), owner_id (null for system-owned), provider, encrypted_key, nickname, validation_status (`valid` | `invalid` | `unverified`), last_validated_at, last_used_at, is_active, created_at. `system`-owned rows back the shared pool; `user`-owned rows are added via the Settings → API Keys flow (see §9) and are picked up automatically by the resolver — no routing changes needed when a user adds one.
- **`provider_quota_state`** — provider, model, quota_scope (`"system"` in v1; a user_id once BYOK keys exist), rpm_used, rpd_used, tpm_used, window_reset_at (Redis-backed; this is the live budget tracker, now scoped so shared-pool and future private-pool usage never get double-counted against each other)
- **`user_quota_allocations`** — api_key_id, provider, model, daily_cap, daily_used, window_reset_at (each gateway user's personal slice of the shared pool — only relevant on the shared-pool path; irrelevant once/if that user switches to a private key)
- **`file_extractions`** — file_hash, extraction_text, extracted_by_provider, extraction_confidence, created_at (the extraction cache)
- **`requests`** — id, api_key_id, conversation_id, provider, model, requested_model_slot (what the user asked for, e.g. `"auto"` or `"llm2"`), tokens_in, tokens_out, latency_ms, status, created_at

---

## 7. Provider Landscape (snapshot — verify current numbers, these change often)

As of mid-2026, roughly:
- **Gemini (Google AI Studio):** Free tier covers Flash/Flash-Lite models (Pro models were moved behind billing in April 2026). Roughly 10-15 requests/minute, ~1,500 requests/day, large token-per-minute allowance, and notably a very large (up to 1M token) context window — the standout free option for long documents and native multimodal (image/PDF) understanding. Caveat: free-tier prompts may be used by Google to improve their models, which matters for anything privacy-sensitive. Quota is enforced per Google Cloud *project*, not per API key — extra keys in the same project don't add quota.
- **Groq:** No-credit-card free tier, open-source models only (Llama, Qwen, Gemma, GPT-OSS variants), notably fast inference (LPU hardware). Limits vary by model — commonly around 30 requests/minute but a fairly tight tokens-per-minute ceiling and daily request caps in the low thousands. Rate limits apply at the organization level, so extra keys don't help. No proprietary models (no GPT, Claude, or Gemini through Groq).
- **OpenRouter:** Aggregates many providers, exposes 25+ models with free (`:free`-suffixed) variants including DeepSeek, Llama, Qwen, Gemma, and even some Gemini models. Around 20 requests/minute; daily cap starts at 50 requests/day and rises to 1,000/day once you've ever purchased at least $10 in credits (a one-time, non-expiring unlock).

**Design implication:** treat these three as your initial provider pool — Gemini as the primary multimodal/perception provider (and a secondary text option), Groq for fast, high-throughput plain-text answering, OpenRouter for model variety and as an additional failover lane.

---

## 8. Model Selection Interface Design

The client-facing interface should expose a small set of **logical model slots**, decoupled from actual provider/model names, so the underlying providers can change without breaking any client integration:

```
GET /v1/models
→ [
    { "slot": "auto", "description": "Gateway picks the best available option" },
    { "slot": "llm1", "provider": "gemini", "model": "gemini-flash", "status": "available" },
    { "slot": "llm2", "provider": "groq", "model": "llama-3.3-70b", "status": "available" },
    { "slot": "llm3", "provider": "openrouter", "model": "deepseek-r1:free", "status": "rate_limited" }
  ]
```

- `status` should reflect *live* quota state (from `provider_quota_state`), so a frontend can grey out a currently-exhausted option rather than let the user pick something that will just fail.
- Client sends `"model": "auto"` or `"model": "llm2"` in the request.

**Behavior per mode:**
- **`auto`** — the router picks using its normal priority/failover logic (quota availability first, then whatever secondary heuristic you choose — e.g., prefer faster providers for short prompts, or the highest-quality available option for complex ones, as a stretch goal).
- **Specific slot (e.g., `"llm2"`)** — the gateway *attempts* to serve the request via exactly that provider/model.
  - **Design decision to make explicitly (worth documenting either way):** if that specific slot is currently out of quota, do you (a) return a clear error telling the client "llm2 is currently rate-limited, try again later or switch to auto," or (b) silently fail over anyway and tell the client which model actually served the response? Option (a) respects explicit user intent; option (b) prioritizes availability. A reasonable default: respect the explicit choice and error out with a clear message — silent substitution when the user asked for something specific is a worse experience than an honest "not available right now."
- Per-conversation "pinning": once a conversation has used tool calls (see §4.7), the `pinned_model` field can override slot selection to keep memory-translation edge cases from ever occurring.

---

## 9. API Key Strategy: Shared Pool by Default, Optional Private Keys via Settings

**Decision:** every user is on the shared pool automatically at login — no setup required. Users who want more headroom, or want usage billed to their own provider account rather than drawn from the shared budget, can add their own API keys in a Settings page. Once added, that user's requests to that specific provider route through their private key and private quota instead — transparently, with no other change to how they use the product.

### 9.1 Default state: shared pool, zero setup
On account creation, a user has no `provider_keys` rows of their own. Every request resolves to the shared `system`-owned keys (see resolver below), subject to their `user_quota_allocations` cap. This is the zero-friction default — the whole point of the free-tier-aggregation design is that a new user gets a working product with no configuration.

### 9.2 Adding a private key (Settings → API Keys)
One row per supported provider (Gemini, Groq, OpenRouter, ...), each showing status (`Using shared pool` / `Using your key`) and an Add/Remove action.

**Add flow:**
1. User pastes their key into a masked input field for that provider.
2. Before saving anything, fire a minimal **validation request** against that provider (a 1-token completion call, or a "list models" call if the provider offers one) to confirm the key is genuinely valid and live.
3. If valid → encrypt, store as `provider_keys` (`owner_type='user'`, `is_active=true`), flip status to "Using your key" immediately.
4. If invalid → clear error ("This key couldn't be verified with Gemini — check that it's active and hasn't been revoked"); nothing gets stored. This matters: silently saving a bad key just means it fails later, mid-conversation, in a much more confusing way for the user.

**Remove flow:** deleting/deactivating the row reverts that provider to the shared pool immediately — no other action needed (see §9.5).

### 9.3 Key resolution logic (the real code path, not just a future seam)
```
resolve_provider_key(user_id, provider):
    user_key = lookup provider_keys WHERE owner_type='user'
               AND owner_id=user_id AND provider=provider AND is_active=true

    if user_key exists:
        return { key: user_key, pool: "private", quota_scope: user_id }
    else:
        system_key = lookup provider_keys WHERE owner_type='system' AND provider=provider
        return { key: system_key, pool: "shared", quota_scope: "system" }
```
The router always calls this — never a raw key directly.

### 9.4 Quota checks branch on the resolver's result
- **Shared pool path:** check the user's personal daily cap (`user_quota_allocations`) *and* the global shared-pool remaining (`provider_quota_state` where `quota_scope='system'`).
- **Private key path:** check only the provider's real limits, scoped to that user (`provider_quota_state` where `quota_scope=<user_id>`) — no artificial per-user cap, since nothing is being shared.

### 9.5 Per-provider granularity, not all-or-nothing
A user can add a private key for one provider and stay on the shared pool for the rest — e.g., their own Gemini key (more headroom for file uploads) while Groq and OpenRouter stay shared. The resolver already supports this naturally, since it resolves per `(user_id, provider)` pair on every call, not once per user globally.

### 9.6 Live effect, no reconnect required
Because resolution happens per-request rather than being cached at login, adding or removing a key takes effect on the user's very next message — including mid-conversation. No re-login or session refresh needed. This is a direct payoff of resolving per-request instead of per-session.

### 9.7 Bonus: private keys can unlock extra model slots
If a user's own key has access to a tier your shared pool doesn't carry (e.g., a paid Gemini Pro key when the shared pool only carries free-tier Flash), their `/v1/models` response can surface that extra slot, visible only to them. This falls out of the design for free — the model registry just also checks the user's own `provider_keys` capabilities when building their personalized slot list, not only the global `capability_registry`.

### 9.8 Security & trust
- Keys encrypted at rest, never logged in plaintext, never surfaced in error messages or usage logs.
- Displayed masked after saving (last 4 characters only, e.g. `sk-...a91c`) — the full key is never shown again after initial entry.
- Plain-language disclosure in Settings: requests made with a private key are billed to and governed by that provider's own terms, not the shared pool's — relevant since data/training-usage terms differ by provider (see §7) and now genuinely belong to the user's own account once they opt in.
- Rate-limit the key-validation endpoint itself, since it's an obvious target for abuse (someone hammering it with garbage input).

### 9.9 Data model additions for this
`provider_keys` gains: `nickname` (optional user-friendly label), `validation_status` (`valid` | `invalid` | `unverified`), `last_validated_at`, `last_used_at`.

---

## 10. Known Constraints & Design Decisions (be upfront about these)

- **Not a production system.** This is intentionally built entirely on free tiers for learning/portfolio purposes — it will have lower throughput, higher latency variance, and lower consistency than a paid setup. That's fine and worth stating plainly rather than overselling it.
- **Multi-key farming on a single provider is out of scope and against most providers' terms.** The value here comes from combining *distinct providers'* independent free offerings, not from generating extra keys/projects on one provider to inflate quota.
- **Answer quality varies by which provider served a given response**, since free-tier models differ significantly in capability. Worth logging `provider_used`/`model_used` per message so this is visible/debuggable, not hidden.
- **Tool-call history translation across providers is explicitly out of scope for v1** (see §4.7).
- **Free-tier data-privacy terms differ by provider** — some may use prompts for model training. Worth a visible disclosure in the UI, especially given the file-upload feature will sometimes route sensitive content to third-party providers for extraction.

---

## 11. Suggested Free Tech Stack

- **API layer:** FastAPI (async support matters for streaming and concurrent provider calls)
- **Rate limiting / quota state / caching:** Redis (Upstash free tier)
- **Persistent storage:** Postgres (Supabase or Neon free tier)
- **Retry/circuit breaker:** `tenacity` for retries, or a hand-rolled circuit breaker (better interview story than importing one)
- **Local file fallback parsing:** PyMuPDF/pdfplumber (PDFs), Tesseract (OCR for images)
- **Deployment:** Docker Compose locally; Fly.io or Render free tier for hosting
- **Observability:** structured JSON logging; optional `/metrics` endpoint (Prometheus format) as a stretch goal

---

## 12. Phased Build Plan

1. **Phase 1 — Single-provider proxy:** one provider, API key auth, request logging, basic deployed endpoint.
2. **Phase 2 — Multi-provider core:** add a second and third provider behind a shared abstraction layer, implement failover + circuit breaker, add streaming support.
3. **Phase 3 — Quota-aware routing:** Redis-backed per-provider/per-model budget tracker (RPM/RPD/TPM), `auto` vs. specific-slot model selection, `/v1/models` endpoint with live status.
4. **Phase 4 — Perception lane:** file upload handling, native-passthrough-first logic, Gemini-based extraction fallback, extraction caching, local OCR/parsing as final fallback.
5. **Phase 5 — Memory & cross-provider translation:** canonical conversation schema, per-provider adapters, explicit scoping/documentation of the tool-call translation limitation.
6. **Phase 6 — BYOK settings:** Settings → API Keys page, key validation flow, resolver + per-provider quota scoping wired in (see §9), masked key display, remove/revert-to-shared flow.
7. **Phase 7 — Polish:** usage dashboard, README with architecture diagrams and the *reasoning* behind each major decision (this matters more than more features).

---

## 13. Why This Project Stands Out (for interviews/portfolio)

This isn't "I called an LLM API." It demonstrates:
- Real backend engineering: async processing, streaming proxies, caching strategy, rate limiting, circuit breakers, structured data modeling.
- A genuinely nontrivial resource-allocation problem: reasoning about several scarce, heterogeneously-measured resources (RPM/RPD/TPM across providers that don't agree on units) simultaneously — closer to what real infra/platform teams deal with than a typical tutorial project.
- Product judgment: an honest, documented decision about what's in scope (plain-text provider switching) vs. explicitly out of scope (tool-call translation across incompatible schemas), plus a real privacy trade-off surfaced rather than hidden (free-tier training-data terms).
- Applied AI engineering: multimodal handling, provider abstraction, prompt/context construction — without pretending model orchestration is a solved problem.

The strongest interview answers this project sets up: *"What happens when a provider is slow but not fully down?" "Why Redis for quota tracking instead of in-memory?" "How do you cache a streaming response, or do you not?" "What's your idempotency story if a client retries a request that already succeeded?" "What did you deliberately not solve, and why?"*

---

## 14. Open Questions / Future Ideas (not required for v1)

- Semantic caching (embedding-similarity cache instead of exact-match).
- Load-based routing: pick the fastest/cheapest *currently available* option dynamically based on recent latency, not just a static priority order.
- Summarization-based context compression when switching to a smaller-context-window model mid-conversation.
- A minimal admin dashboard (even Streamlit) showing request volume, provider distribution, and error rates.
- Extending the "auto" heuristic beyond quota availability — e.g., routing short/simple prompts to faster providers and complex prompts to higher-capability ones.
