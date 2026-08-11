# CLAUDE.md — LLM Gateway operating guide

Free-tier multi-provider LLM gateway: a FastAPI service sitting between clients and Gemini/Groq/OpenRouter,
exposing one OpenAI-shaped API while it owns conversation state, routes across logical model slots, fails over
when a free tier runs out, tracks heterogeneous quota (RPM/RPD/TPM) in Redis, and understands uploaded files
through a separate "perception lane" even when the answering model can't. Portfolio/learning project, runs
entirely on free tiers. Full specs: [contracts-and-phase1.md](doc/reference/contracts-and-phase1.md),
[project-overview.md](doc/reference/project-overview.md), [development-plan.md](doc/reference/development-plan.md).
Where the overview and the contracts doc disagree, the contracts doc wins.

## Locked decisions (§1) — do not relitigate

- **D1** Mid-stream failover: restart the stream on a new provider, discard the partial, emit `restart`, max 3 attempts.
- **D2** Specific slot exhausted: same as D1 — silently fail over, then disclose which model actually served it.
- **D3** Tool-call history: out of scope for v1. First tool call sets `conversations.pinned_model`; router honors it.
- **D4** Context overflow: truncate oldest non-system messages, insert an omission marker. Summarization designed, not built.
- **D5** Caching + streaming: cache non-streaming directly; for streams, assemble after `done` and replay hits as a synthetic stream.
- **D6** Idempotency: optional `Idempotency-Key` header → Redis map to `request_id`, 24h TTL.
- **D7** Auth: Supabase Auth (JWT verified against JWKS) for humans, gateway `gw_live_` API keys for programmatic use; both collapse to one `Principal`. Quota keys on `user_id`, never `api_key_id`.
- **D8** Gemini quota split: 50/50 answer lane vs. perception lane, configurable per environment.

Response provenance (`served_by`, `substituted`, `attempts`) is always emitted and always rendered — that
disclosure is what makes D1/D2 honest. It is load-bearing, not UI polish.

## The three frozen contracts (§2)

**FROZEN. Any change to a signature, error class, invariant, or key format requires asking the user first.**

### A — `ProviderAdapter` (`app/providers/`)
Protocol: `build_payload` (pure — no I/O, clock, or randomness; golden-file testable), `complete`, `stream`
(yields as chunks arrive, no buffering, enforces `idle_timeout`), `parse_error`, `extract_usage`,
`estimate_tokens`, `validate_key`, `rate_limit_headers`. Supporting frozen dataclasses: `ModelSpec`,
`GenParams`, `Usage`, `Completion`, `StreamChunk`.

Every provider quirk dies inside `parse_error`. The router only ever sees these normalized errors, each
carrying `retryable_same_provider` / `failover_eligible` / `breaker_eligible`:

| Error | same | failover | breaker |
|---|---|---|---|
| `RateLimited` (429, quota body; `retry_after_s`) | ✗ | ✓ | ✓ |
| `Unavailable` (5xx, reset, timeout, idle stall) | ✓ | ✓ | ✓ |
| `AuthFailed` (401/403 — also alert, it's an ops problem) | ✗ | ✓ | ✓ |
| `BadRequest` (400, malformed — our bug) | ✗ | ✗ | ✗ |
| `ContextTooLong` (`limit_tokens`; retry once after re-truncation) | ✓ | ✗ | ✗ |
| `ContentFiltered` (failover would just launder a refusal) | ✗ | ✗ | ✗ |
| `EmptyResponse` (HTTP 200, nothing usable — common on free tiers) | ✓ | ✓ | ✗ |

### B — Canonical message schema (`app/memory/canonical.py`)
`CanonicalMessage(id, conversation_id, role, content: list[ContentBlock], meta: MessageMeta, created_at, seq,
schema_version)`. Blocks: `text`, `file_ref`, `omission_marker`, reserved `tool_call`/`tool_result`,
future `summary`. Six invariants, enforced in code and DB constraints where possible:

1. At most one `system` message per conversation, always `seq = 0`.
2. `seq` is unique per conversation and gap-free.
3. Roles alternate after the system message — tolerate consecutive users, never consecutive assistants.
4. `content` is never empty; an empty generation is an error, not a stored message.
5. `meta.provider_used` non-null on every assistant message, null on every user message.
6. `file_ref` stores only the hash — never bytes, never extracted text (resolved at render time).

Rendering is one six-step pipeline in `app/memory/render.py`: resolve attachments → materialize → budget →
fit (D4) → adapt via `build_payload` → `RenderReport`. Every provider payload in the system comes out of it.

### C — Redis key schema (`app/cache/keys.py`)
`q:{scope}:{provider}:{model}:{rpm|rpd|tpm|tpd}`, `:res:{req_id}`, `:lane:perception`, `cb:{provider}:{model}`,
`cache:exact:{sha256}`, `extract:{file_hash}`, `lock:extract:{file_hash}`, `idem:{user_id}:{idem_key}`,
`rl:{user_id}:{window_start}`, `jwks:supabase`, `stream:{message_id}:attempts`. `scope` is `system` or a
`user_id`, keeping shared-pool and BYOK usage from cross-contaminating. Reservation is a single Lua script
(atomic check-and-increment; a pipeline overshoots under concurrency). Redis down → fail **closed** on quota,
**open** on caching and our own rate limiting.

## Repo structure (§3) — new files go in their designated slot, nowhere else

```
alembic/{env.py,versions/}
config/{providers.yaml,limits.yaml,pricing.yaml}
app/
  main.py  config.py  deps.py
  api/{v1/{chat,models,files,conversations}.py, auth.py, keys.py, admin.py, health.py}
  schemas/{chat,models,files,keys,errors}.py        # pydantic request/response only
  core/{errors,logging,ids,clock,crypto}.py
  auth/{principal,jwt,api_keys,dependency}.py
  providers/{types,errors,base,groq,gemini,openrouter,registry}.py
  routing/{router,selection,circuit_breaker}.py
  streaming/{sse,orchestrator,collector}.py
  quota/{tracker,windows,lanes}.py + quota/scripts/*.lua
  memory/{canonical,render,fitting,summarize}.py
  perception/{lane,extractors,local,storage}.py
  cache/{keys,client,exact}.py
  keys_resolution/resolver.py
  usage/{logger,metrics}.py
  db/{session.py,models.py,repo/{users,conversations,messages,requests,provider_keys,extractions}.py}
frontend/            # Next.js App Router + Tailwind; lib/sse.ts, components/ModelIndicator.tsx
tests/{conftest.py,fixtures/{provider_responses,golden_payloads,files},unit,contract,integration}
scripts/{record_fixtures,chaos_demo,seed_dev}.py
docs/{architecture.md,limitations.md,decisions/}      # ADRs; doc/reference/ holds the source specs
Makefile  pyproject.toml  docker-compose.yml  Dockerfile  .env.example  .github/workflows/ci.yml
```

## Current phase: Phase 1 (§4)

**Scope:** authenticated, persistent, single-provider (Groq) non-streaming chat, deployed. Supabase JWT +
gateway API keys, first Alembic migration (`users`, `api_keys`, `conversations`, `messages`, `requests`),
error envelope, full canonical schema + invariants, ownership-scoped repos, `providers/{types,errors,base}.py`
before `groq.py`, render pipeline with all six steps present, `POST /v1/chat/completions` returning a top-level
`served_by`, conversation CRUD endpoints, an ugly-but-real Next.js UI with `ModelIndicator`, tests, deploy.

**Explicitly NOT in Phase 1:** no streaming, no failover, no quota enforcement, no file handling. Redis runs in
compose but is unused. Unused columns (`substituted`, `attempts`, `wasted_tokens_out`) get added now anyway.

**Done means:** a logged-in user sends a message, gets a Groq answer, refreshes, history is still there, and a
`requests` row records provider, model, tokens, latency, and status.

## Conventions

Python 3.12 · async everywhere (never block the event loop) · SQLAlchemy 2.0 declarative + `AsyncSession` ·
pydantic v2 and pydantic-settings (config fails loudly at startup on a missing var) · structlog JSON with
`request_id`/`user_id` bound as contextvars · ruff + mypy strict · pytest + pytest-asyncio ·
`httpx.MockTransport` for all upstream HTTP.

## Hard rules

- **Never call a live provider API from tests.** Record fixtures once via `scripts/record_fixtures.py`; tests read them.
- **Never write an f-string Redis key outside `app/cache/keys.py`.** Every key comes from a builder there.
- **Never store a provider's request body.** Canonical schema only — anything else makes each provider switch a migration.
- **Every conversation read is ownership-scoped in the SQL query itself** (`WHERE id = :cid AND user_id = :uid`). No fetch-then-check; a miss is 404, not 403.
- **Phase 2+ seams are typed signatures raising `NotImplementedError`** — never silently-passing stubs. The signature and its return type are the contract; only the body is deferred.
