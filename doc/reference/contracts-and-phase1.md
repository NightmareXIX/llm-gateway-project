# LLM Gateway — Locked Decisions, Foundational Contracts, Repo Structure & Phase 1

Supersedes §0–§2 and Phase 1 of the earlier development plan. Phases 2–7 from that document still stand, with the amendments noted in §1.

---

## 1. Locked Decisions (final)

| # | Decision | Final call |
|---|---|---|
| D1 | Mid-stream failover | **Restart the stream on a new provider.** Server discards the partial output from the failed attempt, emits a `restart` control event, and begins generating again from the same canonical history. Max 3 attempts per message. |
| D2 | Specific slot exhausted | **Same behavior as D1 — silently fail over**, then report which model actually served the response. The requested slot is still recorded so the mismatch is visible. |
| D3 | Tool-call history | Out of scope for v1. First tool call sets `conversations.pinned_model`; router honors it thereafter. |
| D4 | Context-window overflow | Truncate oldest non-system messages, insert an omission marker. Summarization is designed for but not built (see §2.2.7). |
| D5 | Caching + streaming | Cache non-streaming responses directly. For streaming, assemble the full text after `done` and write it to cache; cache hits replay as a synthetic stream. |
| D6 | Idempotency | Optional `Idempotency-Key` header → Redis map to `request_id`, 24h TTL. |
| D7 | Auth | **Supabase Auth (email/password + OAuth) for human users**, JWT verified against JWKS in FastAPI. Gateway-issued API keys remain as a secondary programmatic surface. Both collapse to a single `Principal` object. Details in §1.2. |
| D8 | Gemini quota split | **50/50** between the answer lane and the perception lane, configurable per environment. |

Because D1 and D2 both now resolve to "switch and disclose," a single mechanism serves both: **the response always carries provenance, and the client always renders it.** That disclosure is what makes silent substitution honest rather than sneaky — it's the load-bearing part of the design, not a UI nicety.

### 1.1 Mid-stream failover mechanics

This is the most intricate runtime behavior in the system. Specify it precisely before building.

**Wire protocol.** SSE, OpenAI-compatible deltas plus three custom events:

```
event: meta
data: {"attempt": 1, "slot": "llm2", "provider": "groq", "model": "llama-3.3-70b",
       "requested_slot": "llm2", "conversation_id": "...", "message_id": "..."}

event: delta
data: {"choices":[{"delta":{"content":"Hel"}}]}

event: restart
data: {"reason": "provider_unavailable", "failed": {"provider":"groq","model":"llama-3.3-70b"},
       "next": {"slot":"llm1","provider":"gemini","model":"gemini-flash"},
       "attempt": 2, "discarded_chars": 412}

event: done
data: {"served_by": {"slot":"llm1","provider":"gemini","model":"gemini-flash"},
       "requested_slot": "llm2", "substituted": true, "attempts": 2,
       "usage": {"tokens_in": 812, "tokens_out": 340, "wasted_tokens_out": 96},
       "degraded": false, "status": "ok"}
```

**Server-side state machine per streamed message:**

1. Build candidate list (router). Reserve quota on candidate 1. Emit `meta`.
2. Stream deltas to client while accumulating them in an in-memory buffer.
3. On a mid-stream fault — connection reset, provider 5xx, a mid-stream error frame, or an idle-token timeout (no delta for N seconds, default 30) — abort the upstream request.
4. **Commit actual consumed quota for the failed attempt.** Tokens already generated were really spent; they count against RPD/TPM even though the text is discarded. Record them as `wasted_tokens_out`. Release only the unused portion of the reservation.
5. Open the circuit breaker for that `(provider, model)` if the fault is a breaker-eligible class.
6. If `attempt < 3` and another candidate is available: reserve on the next candidate, emit `restart`, clear the buffer, go to step 2.
7. If attempts are exhausted: emit `done` with `status: "failed"` and, if the longest partial buffer is non-trivial, include it as `partial_content` so the client can offer "keep partial answer."

**Rules that keep this from getting out of hand:**

- **Never restart after `done`.** Once the terminal event is sent, the message is final.
- **Never restart on `BadRequest` or `ContextTooLong`.** Those will fail identically on every provider. `ContextTooLong` triggers re-truncation and *one* retry against the same provider, not a failover.
- **Idle timeout is a fault.** A provider that accepts the connection and then stalls is the "slow but not down" case; treat a 30s gap between deltas as a failure and move on.
- **Client disconnect is not a fault.** Detect it, abort upstream, commit tokens, persist nothing further.
- **One `messages` row per logical message**, with `model_used` = the final serving model. Attempt history goes in the `requests` row as a JSONB `attempts` array — that's your debugging surface for "why did this answer look weird?"

**Client-side contract:** on `restart`, clear the in-progress assistant bubble entirely, swap the model indicator, and resume appending. Do not try to splice or diff the two attempts.

**Frontend indicator.** Below every assistant message, always render `served_by`. When `substituted` is true, render the mismatch too — e.g. `Gemini Flash · llm2 was unavailable`. When `attempts > 1`, a subtle marker with the attempt trail on hover. When `degraded` is true (local OCR fallback), say so.

### 1.2 Auth model (D7 expanded)

Since this is a chat product with per-user history, not just a machine-to-machine proxy:

- **Supabase Auth** handles registration, password hashing, email verification, OAuth, and password reset. You never touch a password. It's on the same free-tier Postgres you're already using.
- FastAPI verifies the Supabase JWT against the project's JWKS endpoint (cache the JWKS, refresh on `kid` miss). Extract `sub` → `user_id`.
- A local `users` table mirrors `id`, `email`, `tier`, `created_at`, populated on first authenticated request (upsert-on-login). Keeps foreign keys local and lets you add app-specific fields without touching the auth schema.
- **Gateway API keys stay** — they're what makes this a *gateway* rather than a chat app, and they're the surface your own rate limiting was designed around. Format `gw_live_<32 random chars>`, stored as SHA-256, prefix + last 4 kept in plaintext for display.
- Both paths produce the same object, and nothing downstream knows or cares which was used:

```python
@dataclass(frozen=True)
class Principal:
    user_id: UUID
    auth_method: Literal["session", "api_key"]
    api_key_id: UUID | None
    tier: str                    # "free" | "plus" — drives user_quota_allocations
```

- **Quota and rate limiting key on `user_id`**, not `api_key_id`. A user with three API keys shouldn't get three times the budget. Amend `user_quota_allocations` accordingly: `(user_id, provider, model)` rather than `(api_key_id, ...)`. Keep `api_key_id` on `requests` for attribution.
- Row-level ownership checks on every conversation read/write. `SELECT ... WHERE id = :cid AND user_id = :uid` — never fetch first and check after.

---

## 2. Foundational Contracts

These three contracts are the spine. Everything in Phases 2–7 is an implementation against them, so get them right before writing provider code.

### 2.1 Contract A — `ProviderAdapter`

#### 2.1.1 Supporting types

```python
# app/providers/types.py
from dataclasses import dataclass, field
from typing import Literal, AsyncIterator, Protocol

@dataclass(frozen=True)
class ModelSpec:
    slot: str                    # "llm1" — the client-facing identity
    provider: str                # "groq"
    model: str                   # "llama-3.3-70b-versatile" — the wire name
    context_window: int
    max_output_tokens: int
    supports_streaming: bool
    supports_vision: bool
    supports_pdf: bool
    supports_system_field: bool  # top-level system field vs. in-array system message
    max_file_bytes: int | None
    priority: int                # lower = tried first in auto mode
    reserved_fraction: float = 0.0   # Gemini: 0.5 held for the perception lane

@dataclass(frozen=True)
class GenParams:
    temperature: float = 1.0
    max_tokens: int | None = None
    top_p: float | None = None
    stop: list[str] = field(default_factory=list)
    stream: bool = False

@dataclass(frozen=True)
class Usage:
    tokens_in: int
    tokens_out: int
    estimated: bool = False      # True when the provider didn't report usage

@dataclass(frozen=True)
class Completion:
    text: str
    usage: Usage
    finish_reason: Literal["stop", "length", "content_filter", "error"]
    raw_id: str | None           # provider's own response id, for support tickets

@dataclass(frozen=True)
class StreamChunk:
    delta: str
    finish_reason: str | None = None
    usage: Usage | None = None   # some providers send usage only on the final chunk
```

#### 2.1.2 Normalized error hierarchy

The router only ever sees these. Every provider-specific quirk dies inside `parse_error`.

```python
class ProviderError(Exception):
    provider: str
    model: str
    retryable_same_provider: bool
    failover_eligible: bool
    breaker_eligible: bool
    raw: dict | None

class RateLimited(ProviderError):        # 429, or a quota-exhausted body
    retry_after_s: float | None
    # same=False, failover=True, breaker=True

class Unavailable(ProviderError):        # 5xx, connection reset, timeout, idle stall
    # same=True (bounded retries), failover=True, breaker=True

class AuthFailed(ProviderError):         # 401/403, revoked or invalid key
    # same=False, failover=True, breaker=True  — and alert; this is an ops problem

class BadRequest(ProviderError):         # 400, malformed payload — YOUR bug
    # same=False, failover=False, breaker=False

class ContextTooLong(ProviderError):     # a BadRequest subtype worth separating
    limit_tokens: int | None
    # same=True after re-truncation, failover=False

class ContentFiltered(ProviderError):    # provider refused on safety grounds
    # same=False, failover=False — failing over just launders a refusal

class EmptyResponse(ProviderError):      # HTTP 200, zero usable content
    # same=True (once), failover=True, breaker=False
```

`EmptyResponse` matters more than it looks: free-tier endpoints return 200-with-nothing often enough that without it you'll ship empty assistant messages and blame the model.

#### 2.1.3 The protocol

```python
class ProviderAdapter(Protocol):
    name: str

    # ---- payload construction -------------------------------------------
    def build_payload(self, messages: list[CanonicalMessage],
                      spec: ModelSpec, params: GenParams,
                      attachments: list[ResolvedAttachment]) -> dict:
        """Pure function. No I/O, no clock, no randomness — so it's golden-file
        testable. Handles system-prompt placement, role renaming, content-block
        shaping, and native file embedding."""

    # ---- execution -------------------------------------------------------
    async def complete(self, payload: dict, key: str,
                       timeout: float) -> Completion: ...

    async def stream(self, payload: dict, key: str,
                     timeout: float, idle_timeout: float
                     ) -> AsyncIterator[StreamChunk]:
        """Must yield chunks as they arrive — no internal buffering. Must raise
        a normalized ProviderError, including for mid-stream faults. Must
        enforce idle_timeout between chunks."""

    # ---- normalization ---------------------------------------------------
    def parse_error(self, exc: Exception | httpx.Response) -> ProviderError: ...

    def extract_usage(self, response_body: dict) -> Usage:
        """Falls back to an estimate with estimated=True when absent."""

    def estimate_tokens(self, payload: dict) -> int:
        """Pre-call estimate for quota reservation. Cheap approximation is fine
        (chars/4 or tiktoken); accuracy is reconciled at commit time."""

    # ---- operations ------------------------------------------------------
    async def validate_key(self, key: str) -> KeyValidation:
        """Cheapest possible liveness check. Used by the BYOK flow (Phase 6)
        and by a startup health check."""

    def rate_limit_headers(self, response: httpx.Response) -> QuotaHint | None:
        """Providers that return x-ratelimit-remaining-* let you correct your
        local counters against ground truth. Opportunistic, never required."""
```

#### 2.1.4 Per-provider implementation notes

Capture these as tests, not comments.

**Groq** — OpenAI-compatible; the reference implementation. System prompt as an in-array message. Returns `x-ratelimit-remaining-requests` and `-tokens` headers → wire into `rate_limit_headers` and use them to correct drift in your own counters. Rate limits are org-level, so a second key changes nothing.

**Gemini** — the shape-divergent one, and therefore the one that proves the abstraction is real:
- Endpoint path carries the model; the API key goes in a header, not the body.
- `system_instruction` is a **top-level field**, not a message.
- Messages are `contents: [{role, parts: [...]}]`, and the assistant role is `"model"`, not `"assistant"`.
- Files ride as `inline_data: {mime_type, data: <base64>}` parts (or the Files API for large uploads).
- Streaming needs an explicit SSE flag on the endpoint; chunk shape differs from OpenAI's.
- Quota is enforced per Google Cloud *project* — extra keys in one project add nothing.

**OpenRouter** — OpenAI-compatible, but: model names carry a `:free` suffix that must never be dropped; expects `HTTP-Referer` and `X-Title` headers for attribution; may return **402** for credit issues, which is a `RateLimited`, not an `AuthFailed`; upstream model availability changes without notice, so `EmptyResponse` and `Unavailable` are more common here than elsewhere.

#### 2.1.5 Adapter conformance suite

Write one parameterized test module that every adapter must pass. Adding a fourth provider later then means implementing an interface and running an existing suite.

1. `build_payload` on a fixed 6-message canonical history matches a committed golden JSON file.
2. Each recorded provider error fixture maps to the expected normalized class with the expected flags.
3. `stream` yields ≥2 chunks and terminates on a recorded SSE fixture.
4. A truncated/half-written SSE fixture raises `Unavailable`, not a decoding error.
5. A 200-with-empty-content fixture raises `EmptyResponse`.
6. `estimate_tokens` lands within ±25% of reported usage on the fixture set.
7. `build_payload` is pure: calling it twice yields identical output.

### 2.2 Contract B — Canonical Conversation Schema

#### 2.2.1 Why it exists

The gateway owns conversational state; providers are stateless. Storing any provider's request body means every future provider switch is a migration. The canonical form is the only thing that goes in Postgres.

#### 2.2.2 Structure

```python
ContentBlock =
    | {"type": "text", "text": str}
    | {"type": "file_ref", "file_hash": str, "filename": str,
       "mime": str, "bytes": int}
    | {"type": "omission_marker", "omitted_count": int, "reason": "context_truncation"}
    | {"type": "tool_call", ...}      # reserved, v1 pins instead of translating
    | {"type": "tool_result", ...}    # reserved

@dataclass
class CanonicalMessage:
    id: UUID
    conversation_id: UUID
    role: Literal["system", "user", "assistant"]
    content: list[ContentBlock]
    meta: MessageMeta
    created_at: datetime
    seq: int                    # monotonic per conversation; ordering key
    schema_version: int = 1

@dataclass
class MessageMeta:
    provider_used: str | None
    model_used: str | None
    slot_used: str | None
    requested_slot: str | None
    substituted: bool = False
    attempts: int = 1
    tokens_in: int | None = None
    tokens_out: int | None = None
    wasted_tokens_out: int = 0
    degraded: bool = False
    extraction_tier: Literal["cache","native","llm","local"] | None = None
```

`meta` is what feeds the frontend's model indicator, and it's why the D1/D2 disclosure is free at render time — it's already persisted.

#### 2.2.3 Invariants

Enforce in code and, where possible, in DB constraints:

1. At most one `system` message per conversation, always `seq = 0`.
2. `seq` is unique per conversation and gap-free.
3. `role` alternates user/assistant after the system message (tolerate consecutive user messages; never consecutive assistants).
4. `content` is never empty. An empty generation is an error, not a stored message.
5. `meta.provider_used` is non-null for every assistant message and null for every user message.
6. `file_ref` blocks store only the hash — never the bytes, never the extracted text. Extraction is resolved at render time from `file_extractions`, so improving your extractor retroactively improves old conversations.

#### 2.2.4 Storage

```sql
messages (
  id            uuid primary key,
  conversation_id uuid not null references conversations(id) on delete cascade,
  seq           integer not null,
  role          text not null check (role in ('system','user','assistant')),
  content       jsonb not null,
  meta          jsonb not null default '{}',
  schema_version smallint not null default 1,
  created_at    timestamptz not null default now(),
  unique (conversation_id, seq)
);
create index on messages (conversation_id, seq);
```

Keep `schema_version` from day one. Migrating stored JSONB without a version tag is miserable.

#### 2.2.5 The render pipeline

One function, six ordered steps. Every provider payload in the system comes out of here.

```python
async def render(history: list[CanonicalMessage],
                 spec: ModelSpec,
                 params: GenParams,
                 adapter: ProviderAdapter) -> tuple[dict, RenderReport]:
```

1. **Resolve attachments.** For each `file_ref`: if `spec` supports the MIME natively and multimodal quota remains → mark for native embedding. Otherwise look up `file_extractions` by hash and prepare an injected text block. (Phase 4 fills this in; Phase 1–3 the branch exists and returns "no attachments.")
2. **Materialize.** Convert blocks to text/parts. Injected extractions get wrapped in a delimited envelope so the model can tell document content from user instruction:
   `<document name="q3.pdf" source="extracted" confidence="high">…</document>`
3. **Budget.** `context_window − max_output_tokens − safety_margin(5%)` = input budget.
4. **Fit (D4).** If over budget: keep the system message and the last user message unconditionally; drop oldest non-system messages, oldest first, in whole pairs; insert one `omission_marker`. If it still doesn't fit, truncate the largest injected document block before touching more messages. If a single message exceeds the budget alone → raise `ContextTooLong`.
5. **Adapt.** Hand off to `adapter.build_payload` for provider-specific shaping.
6. **Report.** Return a `RenderReport` (`messages_dropped`, `documents_truncated`, `estimated_tokens`, `attachments_native`, `attachments_injected`) for logging and for the frontend's degradation notice.

#### 2.2.6 Cross-provider correctness

The rendering path is the one place where a provider switch can silently corrupt a conversation. Golden test: one fixed canonical history containing a system message, a file_ref, and five turns → three committed payload files, one per provider. Any adapter change that alters those diffs must be deliberate.

#### 2.2.7 Summarization seam (built later, designed now)

Reserve it so it drops in without restructuring:

- New block type `{"type": "summary", "text": str, "covers_seq": [start, end], "generated_by": str}`.
- Step 4 gains a strategy switch: `TRUNCATE` (v1) | `SUMMARIZE` (later). Same interface, different implementation.
- Summaries are cached on the conversation (`conversation_summaries` table keyed by `covers_seq`), generated asynchronously after a turn completes, and use the cheapest available model — never blocking the user's request.
- Invariant to preserve: a summary block replaces the messages it covers; it never coexists with them in a rendered payload.

### 2.3 Contract C — Redis Key Schema

Every key is produced by a builder in `app/cache/keys.py`. No f-strings anywhere else.

| Key | Type | TTL | Written by | Purpose |
|---|---|---|---|---|
| `q:{scope}:{provider}:{model}:rpm` | int | 60s | quota | requests this minute |
| `q:{scope}:{provider}:{model}:rpd` | int | until provider reset | quota | requests today |
| `q:{scope}:{provider}:{model}:tpm` | int | 60s | quota | tokens this minute |
| `q:{scope}:{provider}:{model}:tpd` | int | until reset | quota | tokens today (where applicable) |
| `q:{scope}:{provider}:{model}:res:{req_id}` | hash | 120s | quota | in-flight reservation, for release |
| `q:{scope}:{provider}:{model}:lane:perception` | int | until reset | quota | lane sub-counter for the 50/50 split |
| `cb:{provider}:{model}` | hash | 1h | breaker | `{state, failures, opened_at, cooldown_s, probe_holder}` |
| `cache:exact:{sha256}` | string | 1h | cache | serialized response |
| `extract:{file_hash}` | string | 24h | perception | extracted text (Postgres is source of truth) |
| `lock:extract:{file_hash}` | string (NX) | 60s | perception | stampede guard on concurrent identical uploads |
| `idem:{user_id}:{idem_key}` | string | 24h | api | → `request_id` |
| `rl:{user_id}:{window_start}` | int | 2× window | api | your own rate limiting |
| `jwks:supabase` | string | 12h | auth | cached signing keys |
| `stream:{message_id}:attempts` | int | 300s | streaming | restart counter (D1 cap) |

**Scope** is `system` or a `user_id` — the mechanism that keeps shared-pool and BYOK usage from cross-contaminating (§9.4 of the overview).

**Atomicity.** Reservation must be a single Lua script that checks all limits and increments all counters, or returns which limit blocked. Check-then-increment across separate round trips will overshoot under concurrency, and "why Redis and not in-memory" plus "why Lua and not a pipeline" are two of the better questions this project sets you up to answer.

```lua
-- reserve.lua  KEYS: rpm, rpd, tpm, res  ARGV: rpm_max, rpd_max, tpm_max, est_tokens, req_id, ttl
-- returns {1} on success, or {0, "rpd"} naming the blocking limit
```

**Commit/release.** After the call, a second script reconciles: set the token counters to actual usage, delete the reservation key. On failure, subtract the reservation and delete. On mid-stream abort (D1), commit the tokens actually generated and release the rest.

**Degradation.** If Redis is unreachable: fail *closed* on quota (refuse rather than blow through a provider's limit and get the key banned), fail *open* on caching and your own rate limiting. Write that asymmetry into `docs/decisions/`.

---

## 3. Complete Repo Structure

```
llm-gateway/
├── README.md
├── Makefile                        # dev, test, lint, migrate, seed, docker-up
├── pyproject.toml                  # ruff + mypy + pytest config lives here
├── docker-compose.yml              # api, postgres, redis
├── Dockerfile
├── .env.example
├── .github/workflows/ci.yml
│
├── alembic/
│   ├── env.py
│   └── versions/
│
├── config/
│   ├── providers.yaml              # slot table: slot → provider/model/limits/capabilities
│   ├── limits.yaml                 # RPM/RPD/TPM per provider+model, reset semantics
│   └── pricing.yaml                # simulated-cost table for usage tracking
│
├── app/
│   ├── main.py                     # app factory, lifespan, middleware, router mounts
│   ├── config.py                   # pydantic-settings; typed env; loads config/*.yaml
│   ├── deps.py                     # shared FastAPI dependencies
│   │
│   ├── api/
│   │   ├── v1/
│   │   │   ├── chat.py             # POST /v1/chat/completions
│   │   │   ├── models.py           # GET  /v1/models
│   │   │   ├── files.py            # POST /v1/files
│   │   │   └── conversations.py    # list/read/rename/delete history
│   │   ├── auth.py                 # session bootstrap, /me
│   │   ├── keys.py                 # gateway API keys + BYOK settings (Phase 6)
│   │   ├── admin.py                # usage dashboard data (Phase 7)
│   │   └── health.py               # /healthz, /readyz, /metrics
│   │
│   ├── schemas/                    # pydantic request/response models only
│   │   ├── chat.py
│   │   ├── models.py
│   │   ├── files.py
│   │   ├── keys.py
│   │   └── errors.py               # the error envelope
│   │
│   ├── core/
│   │   ├── errors.py               # AppError hierarchy + exception handlers
│   │   ├── logging.py              # structlog JSON, request_id contextvar
│   │   ├── ids.py                  # ULID/UUID helpers
│   │   ├── clock.py                # injectable time source (testability)
│   │   └── crypto.py               # Fernet wrapper, key hashing
│   │
│   ├── auth/
│   │   ├── principal.py            # Principal dataclass
│   │   ├── jwt.py                  # Supabase JWKS fetch/cache/verify
│   │   ├── api_keys.py             # gw_live_ generation, hashing, lookup
│   │   └── dependency.py           # get_principal() — the single entry point
│   │
│   ├── providers/
│   │   ├── types.py                # ModelSpec, GenParams, Completion, StreamChunk, Usage
│   │   ├── errors.py               # normalized ProviderError hierarchy
│   │   ├── base.py                 # ProviderAdapter protocol + shared HTTP helpers
│   │   ├── groq.py
│   │   ├── gemini.py
│   │   ├── openrouter.py
│   │   └── registry.py             # loads providers.yaml → slot map, capability lookup
│   │
│   ├── routing/
│   │   ├── router.py               # candidate list, failover loop, attempt bookkeeping
│   │   ├── selection.py            # auto vs. specific-slot policy, pinning
│   │   └── circuit_breaker.py      # Redis-backed 3-state breaker
│   │
│   ├── streaming/
│   │   ├── sse.py                  # event framing, heartbeat, client-disconnect detect
│   │   ├── orchestrator.py         # D1 restart state machine
│   │   └── collector.py            # buffer assembled text → cache + persistence
│   │
│   ├── quota/
│   │   ├── tracker.py              # reserve / commit / release
│   │   ├── windows.py              # rolling vs. fixed-time-of-day resets per provider
│   │   ├── lanes.py                # 50/50 answer vs. perception split
│   │   └── scripts/                # reserve.lua, commit.lua, release.lua
│   │
│   ├── memory/
│   │   ├── canonical.py            # CanonicalMessage, ContentBlock, invariants
│   │   ├── render.py               # the six-step render pipeline
│   │   ├── fitting.py              # D4 truncation (+ summarization seam)
│   │   └── summarize.py            # stub in v1
│   │
│   ├── perception/
│   │   ├── lane.py                 # cache → native → llm → local chain
│   │   ├── extractors.py           # Gemini extraction prompts
│   │   ├── local.py                # PyMuPDF, pdfplumber, Tesseract
│   │   └── storage.py              # file bytes in/out, hashing, MIME sniffing
│   │
│   ├── cache/
│   │   ├── keys.py                 # every Redis key builder
│   │   ├── client.py               # redis.asyncio pool, script loading
│   │   └── exact.py                # request-hash cache
│   │
│   ├── keys_resolution/
│   │   └── resolver.py             # resolve_provider_key(user_id, provider)
│   │
│   ├── usage/
│   │   ├── logger.py               # requests-table writer, simulated cost
│   │   └── metrics.py              # Prometheus collectors
│   │
│   └── db/
│       ├── session.py              # async engine, session factory
│       ├── models.py               # SQLAlchemy 2.0 models
│       └── repo/
│           ├── users.py
│           ├── conversations.py
│           ├── messages.py
│           ├── requests.py
│           ├── provider_keys.py
│           └── extractions.py
│
├── frontend/                       # Next.js (App Router) + Tailwind
│   ├── app/
│   │   ├── (auth)/login/page.tsx
│   │   ├── chat/[id]/page.tsx
│   │   └── settings/keys/page.tsx
│   ├── components/
│   │   ├── MessageList.tsx
│   │   ├── ModelIndicator.tsx      # the D1/D2 provenance chip
│   │   ├── ModelPicker.tsx         # slots + live status from /v1/models
│   │   └── FileUpload.tsx
│   └── lib/
│       ├── sse.ts                  # handles meta/delta/restart/done
│       └── supabase.ts
│
├── tests/
│   ├── conftest.py
│   ├── fixtures/
│   │   ├── provider_responses/     # recorded real responses, incl. every error shape
│   │   ├── golden_payloads/        # canonical history → per-provider payloads
│   │   └── files/                  # text PDF, scanned PDF, PNG
│   ├── unit/
│   ├── contract/                   # the adapter conformance suite
│   └── integration/
│
├── scripts/
│   ├── record_fixtures.py          # one-time real API capture
│   ├── chaos_demo.py               # Phase 7 demo driver
│   └── seed_dev.py
│
└── docs/
    ├── architecture.md
    ├── limitations.md
    └── decisions/                  # ADR-001 … ADR-008
```

---

## 4. Phase 1 — Detailed Implementation Plan

**Scope:** authenticated, persistent, single-provider chat, deployed. No streaming, no failover, no quota, no files. But every seam those need is already cut.

**Definition of done:** a logged-in user sends a message from a real (ugly) UI, gets an answer from Groq, refreshes the page, and the conversation is still there. A `requests` row records provider, model, tokens, latency, and status.

### Step 1 — Scaffolding *(half a day)*

- `pyproject.toml` with FastAPI, uvicorn, httpx, sqlalchemy[asyncio], asyncpg, alembic, pydantic-settings, structlog, python-jose, redis, pytest, pytest-asyncio, ruff, mypy.
- `docker-compose.yml`: app + postgres + redis. Redis is unused in Phase 1 — start it anyway so nothing about the topology changes later.
- `Makefile`: `dev`, `test`, `lint`, `migrate`, `revision`.
- `app/config.py` with typed settings: `DATABASE_URL`, `REDIS_URL`, `SUPABASE_URL`, `SUPABASE_JWT_AUDIENCE`, `GROQ_API_KEY`, `ENCRYPTION_KEY`, `ENV`. **Fail loudly at startup on a missing var** — a 500 three hours into a demo because of a typo'd env name is a bad afternoon.
- `app/core/logging.py`: structlog JSON renderer, `request_id` contextvar bound by middleware, `user_id` bound after auth. Every log line carries both from here on.

### Step 2 — Database schema *(half a day)*

First Alembic migration:

```sql
users (id uuid pk, email text unique not null, tier text not null default 'free',
       created_at timestamptz default now())

api_keys (id uuid pk, user_id uuid fk→users on delete cascade,
          key_hash text unique not null, key_prefix text not null, last_4 text not null,
          nickname text, is_active bool default true,
          last_used_at timestamptz, created_at timestamptz default now())

conversations (id uuid pk, user_id uuid fk→users on delete cascade,
               title text, preferred_slot text default 'auto', pinned_model text,
               created_at timestamptz default now(), updated_at timestamptz default now())

messages (id uuid pk, conversation_id uuid fk→conversations on delete cascade,
          seq int not null, role text not null check (role in ('system','user','assistant')),
          content jsonb not null, meta jsonb not null default '{}',
          schema_version smallint default 1, created_at timestamptz default now(),
          unique (conversation_id, seq))

requests (id uuid pk, user_id uuid fk→users, api_key_id uuid null fk→api_keys,
          conversation_id uuid null fk→conversations,
          requested_slot text, served_slot text, provider text, model text,
          substituted bool default false, attempts jsonb default '[]',
          tokens_in int, tokens_out int, wasted_tokens_out int default 0,
          latency_ms int, status text, error_code text,
          cache_hit bool default false, created_at timestamptz default now())
```

Indexes: `conversations(user_id, updated_at desc)`, `messages(conversation_id, seq)`, `requests(user_id, created_at desc)`, `requests(provider, created_at desc)`.

Columns like `substituted`, `attempts`, and `wasted_tokens_out` are unused in Phase 1 — add them now anyway. Migrations on a live free-tier Postgres are more annoying than three extra columns.

### Step 3 — Auth *(1 day)*

- Supabase project, email/password enabled.
- `app/auth/jwt.py`: fetch JWKS, cache in-process (Redis later) with a `kid`-miss refresh, verify signature/exp/aud, return claims.
- `app/auth/api_keys.py`: generate `gw_live_<32>`, SHA-256 store, constant-time compare, prefix+last4 for display.
- `app/auth/dependency.py::get_principal()` — checks `Authorization: Bearer` for a JWT, falls back to `X-API-Key`, returns `Principal`, raises 401 otherwise. Upserts the local `users` row on first sight of a JWT.
- Tests: valid JWT → principal; expired → 401; bad signature → 401; valid API key → principal; revoked key → 401.

### Step 4 — Error envelope *(2 hours)*

One shape for every failure, defined before the first endpoint:

```json
{"error": {"code": "slot_unavailable", "message": "human readable",
           "request_id": "01J...", "details": {}}}
```

Exception handlers for `AppError`, `RequestValidationError`, and a catch-all that logs the traceback and returns a generic 500 with the `request_id`. Never leak internals; always return the `request_id` so a user report is traceable to a log line.

### Step 5 — Canonical schema + repos *(1 day)*

- `app/memory/canonical.py`: the dataclasses and a `validate(messages)` enforcing §2.2.3's invariants. Full implementation now, not a stub.
- `app/db/repo/messages.py`: `append(conversation_id, role, content, meta)` allocating `seq` inside the transaction (`SELECT max(seq) ... FOR UPDATE` or a per-conversation counter — concurrent appends to one conversation must not collide).
- `app/db/repo/conversations.py`: `create`, `get_owned(id, user_id)`, `list_for_user`, `touch`.
- **Every read is ownership-scoped in the query.** No fetch-then-check.

### Step 6 — Provider layer *(1.5 days)*

Write `providers/types.py`, `providers/errors.py`, and `providers/base.py` **first**, then `groq.py` against them. This is the step where discipline pays off later — implementing Groq directly and abstracting it in Phase 2 costs more than doing it in this order now.

- `base.py`: shared async httpx client with connection pooling, sane timeouts (connect 5s, read 60s), and a `_request` helper mapping transport exceptions to `Unavailable`.
- `groq.py`: `build_payload` (pure), `complete`, `parse_error`, `extract_usage`, `estimate_tokens`, `validate_key`, `rate_limit_headers`. Leave `stream` raising `NotImplementedError` — Phase 2.
- `registry.py`: parse `config/providers.yaml` into `ModelSpec`s. Phase 1 has one entry; the file already has the shape for three.
- `scripts/record_fixtures.py`: hit Groq once for each of — success, 401 with a bad key, 400 with a malformed body, 429 if you can provoke one — and commit the responses under `tests/fixtures/provider_responses/`. Do this while you have a working key and never call the live API from tests again.

### Step 7 — Render pipeline v1 *(half a day)*

`memory/render.py` with all six steps present. Step 1 (attachments) returns empty, step 4 (fitting) does simple truncation against `ModelSpec.context_window`. The full signature and `RenderReport` exist from day one, so Phases 4 and 5 fill in bodies rather than rewrite call sites.

### Step 8 — The chat endpoint *(1 day)*

`POST /v1/chat/completions`, non-streaming:

1. `get_principal` dependency.
2. Validate body (`schemas/chat.py`): `messages` or `conversation_id` + a new message; `model` slot (default `"auto"`); generation params.
3. Load or create the conversation, ownership-checked. Persist the user message.
4. Load canonical history.
5. Resolve slot → `ModelSpec` (registry; Phase 1 always resolves to Groq).
6. `render()` → payload.
7. `adapter.complete()`, wall-clocked.
8. On success: persist the assistant message with full `meta` (`provider_used`, `model_used`, `slot_used`, `requested_slot`, `substituted=false`, `attempts=1`, tokens). Write the `requests` row. Return an OpenAI-shaped response **plus a top-level `served_by` object** — the field the frontend indicator reads, present from the very first response so the client contract never changes.
9. On `ProviderError`: write a `requests` row with `status='error'` and the error code, return the mapped HTTP error.

Also ship `GET /v1/conversations`, `GET /v1/conversations/{id}` (with messages), `DELETE /v1/conversations/{id}` — the UI needs them immediately and they're trivial now.

### Step 9 — Minimal frontend *(1.5 days)*

Deliberately ugly, deliberately real. Next.js + Supabase JS client:

- Login page (Supabase Auth UI is fine).
- Conversation list sidebar.
- Message view with a composer.
- **`ModelIndicator.tsx` under each assistant message**, reading `served_by`. It renders one provider in Phase 1 and needs zero changes to handle substitution and restarts in Phase 2 — build it now so the contract is exercised from the start.

### Step 10 — Tests *(1 day)*

- Unit: canonical invariants, `build_payload` golden file, every recorded error fixture → expected normalized class, truncation logic.
- Integration: `httpx.MockTransport` returning the success fixture → full endpoint flow with a test Postgres; assert persistence, `seq` ordering, `requests` row contents.
- Auth: the five cases from Step 3.
- Ownership: user B requesting user A's conversation gets 404 (not 403 — don't confirm existence).

### Step 11 — Deploy *(half a day)*

- Dockerfile: multi-stage, non-root, `uvicorn` with `--workers 2`.
- Fly.io or Render; Supabase Postgres, Upstash Redis.
- Run migrations as a release command, not at app start.
- `/healthz` (liveness) and `/readyz` (DB + Redis reachable).
- GitHub Actions: lint → typecheck → test → deploy on main.

### Step 12 — Phase 1 exit checklist

- [ ] Register, log in, send a message, get an answer
- [ ] Refresh → history intact; second browser/user can't see it
- [ ] `served_by` is rendered under every assistant message
- [ ] Invalid JWT → 401 with the error envelope and a `request_id`
- [ ] Revoked Groq key → clean 502, `requests` row with `status='error'`, no traceback leaked
- [ ] Every `requests` row has tokens and latency populated
- [ ] `make test` green in CI; zero live API calls during tests
- [ ] ADR-001…008 written

**Realistic duration:** 9–11 working days, or ~2.5 weeks part-time.

---

## 5. What to Learn Before Starting Phase 1

Roughly in dependency order. The goal is working familiarity, not mastery — you'll learn the rest by hitting it.

### Must have before writing a line *(~2–3 weeks if new to most of it)*

**1. Python async fundamentals** — `async`/`await`, the event loop, `asyncio.gather`, `asyncio.wait_for` and timeouts, async context managers and generators, and the single most important practical rule: *never call a blocking function inside an async handler* (it stalls the whole loop; use `run_in_executor` for things like Tesseract later). Async generators specifically, since streaming in Phase 2 is `async def stream() -> AsyncIterator[Chunk]`.

**2. FastAPI + Pydantic v2** — path/query/body binding, the dependency-injection system (this is how `get_principal` works and it's used everywhere), `lifespan` for startup/shutdown resources, exception handlers, middleware, and `pydantic-settings` for config. Pydantic v2 specifically — v1 tutorials are still everywhere and the APIs differ.

**3. HTTP + httpx** — the async client, connection pooling and why you reuse one client, timeout granularity (connect vs. read vs. pool), status-code semantics, and headers. Skim SSE now even though you don't build it until Phase 2; it shapes how you write the adapter.

**4. SQLAlchemy 2.0 async + Alembic** — the 2.0 declarative style (very different from 1.x tutorials), `AsyncSession`, transaction boundaries and when to commit, relationship loading strategies, and autogenerate migrations plus how to fix what autogenerate gets wrong. Enough Postgres to be dangerous: indexes, foreign keys with cascade, `JSONB`, `timestamptz`, unique constraints.

**5. Auth concepts** — what a JWT is, why signature verification matters, JWKS and key rotation, access vs. refresh tokens, and why you hash API keys instead of storing them. Plus the Supabase Auth flow specifically: client-side sign-in, JWT in the `Authorization` header, server-side verification.

**6. Docker basics** — writing a Dockerfile, layer caching, docker-compose for multi-service local dev, and env-var injection.

**7. Testing in Python** — `pytest` fixtures and parameterization, `pytest-asyncio`, `httpx.MockTransport` for faking upstream HTTP without a live network, and a test database per run. Fixture-based provider mocking is the single technique this project leans on hardest.

### Learn just-in-time *(don't front-load)*

- **Structured logging** (`structlog`, contextvars) — one afternoon, do it during Step 1.
- **SSE and streaming responses** — before Phase 2.
- **Redis data types, TTLs, and Lua scripting** — before Phase 3. Focus on why `EVAL` gives atomicity that a pipeline doesn't.
- **Circuit breaker pattern** — read the Nygard/Fowler description before Phase 2; it's an afternoon of reading and the implementation is ~80 lines.
- **Tokenization** (`tiktoken`, why provider token counts differ) — before Phase 3's estimation work.
- **PDF/OCR libraries** — before Phase 4.
- **Prometheus metrics format** — before Phase 7.

### Skip for now

Kubernetes, message queues, gRPC, ORM performance tuning, vector databases. None of them are on the critical path, and reaching for them early is the most common way portfolio projects stall before shipping anything.

### A calibration note

If async Python and SQLAlchemy 2.0 are both new, budget three weeks of learning before Phase 1 and expect Phase 1 itself to take four weeks rather than 2.5. That's normal and it's still the right ordering — the alternative is learning them *while* debugging a five-provider routing bug, which is much worse.
