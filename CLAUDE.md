# CLAUDE.md — LLM Gateway operating guide

Free-tier multi-provider LLM gateway: a FastAPI service sitting between clients and Gemini/Groq/OpenRouter,
exposing one OpenAI-shaped API while it owns conversation state, routes across logical model slots, fails over
when a free tier runs out, tracks heterogeneous quota (RPM/RPD/TPM) in Redis, and understands uploaded files
through a separate "perception lane" even when the answering model can't. Portfolio/learning project, runs
entirely on free tiers. Full specs: [contracts-and-phase1.md](doc/reference/contracts-and-phase1.md),
[project-overview.md](doc/reference/project-overview.md), [development-plan.md](doc/reference/development-plan.md),
[phase2.md](doc/reference/phase2.md), [phase3.md](doc/reference/phase3.md), [phase4.md](doc/reference/phase4.md).
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
`rl:{user_id}:{rpm|rpd}:{window_start}` (the `{window}` segment amended in with sign-off in Phase 3
Step 10 — one key cannot address two windows; [ADR-022](docs/decisions/ADR-022-our-own-rate-limiting.md)),
`jwks:supabase`, `stream:{message_id}:attempts`. `scope` is `system` or a
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
frontend/            # Next.js App Router + Tailwind; lib/sse.ts, components/ModelIndicator.tsx, components/ModelPicker.tsx
tests/{conftest.py,fixtures/{provider_responses,golden_payloads,files},unit,contract,integration}
scripts/{record_fixtures,chaos_demo,seed_dev}.py
docs/{architecture.md,limitations.md,decisions/}      # ADRs; doc/reference/ holds the source specs
README.md  Makefile  pyproject.toml  docker-compose.yml  Dockerfile  .env.example  .github/workflows/ci.yml
```

## Current phase: Phase 4 — Perception Lane, Step 2 of 12 done

Phase 1 (single-provider proxy), Phase 2 (multi-provider core, failover, streaming), and Phase 3
(quota-aware routing, below) are done and merged. Phase 4 (file/image understanding via a dedicated
perception lane, per `project-overview.md` §4.5 and `development-plan.md` §3) is next per the phased
build plan; its step-by-step plan is [phase4.md](doc/reference/phase4.md) (D22–D30). Milestone A
(Steps 1–4, the bytes land) is in progress.

**Step 1 (config surface, settings, migration) is in.** Nothing behaves differently yet — this step is
paperwork before mechanism, exactly as Phase 3 Step 1 — but `perception` is now a declared slot, the
tables exist, and every later step can be switched off in one deploy. `pyproject.toml` gained
`python-multipart`, `pymupdf`, `pytesseract`, `pillow`. `app/config.py`'s `Settings` gained
`FILES_STORAGE_BACKEND`/`FILES_LOCAL_DIR`/`FILES_BUCKET`/`SUPABASE_SERVICE_ROLE_KEY` (D23 — paired by a
boot-time model validator, so a `supabase` backend without the key fails at startup rather than on the
first upload), `FILE_MAX_BYTES`, and the three `PERCEPTION_*` switches (`ENABLED`, `LOCAL_ONLY`,
`LOCAL_OCR_ENABLED`) plus `PERCEPTION_OCR_MAX_PAGES`, all printed in `startup.complete`. `Slot` gained
`internal: bool = False`, and `config/providers.yaml` declares the `perception` slot (D26) — the same
two Gemini models `general` and `fast` already route to, `reserved_fraction: 0.5` matching both,
`internal: true`. `registry.build_model_specs` gained a startup check that fails boot if a model's
`reserved_fraction` disagrees across the slots that share it (trap 19), and `ProviderRegistry.slots()` /
`GET /v1/models` now skip internal slots while `.candidates()` still resolves them by name —
`api/v1/chat.py::_validate_slot` gives a client naming `perception` explicitly the same 400 a typo gets.
Migration `0004` adds `files` (per-user ownership, unique on `(user_id, file_hash)`, D24) and
`file_extractions` (content-addressed and global, keyed on `file_hash` alone, D22) — mirrored in
`app/db/models.py` as `File`/`FileExtraction`. The `Dockerfile`'s runtime stage installs
`tesseract-ocr`/`tesseract-ocr-eng` ahead of Step 7's local tier (D30).

**Step 2 (`app/perception/storage.py`) is in.** `ObjectStore` (D23) is a `Protocol` — `put`/`get`/`exists`
on a content-addressed `path` — with three implementations: `SupabaseStore` (every deployed environment,
over `app.state.http_client` so it shares the pool and timeouts every provider adapter already uses),
`LocalStore` (a dev box without Storage configured, path-traversal-checked against its root), and
`MemoryStore` (tests — a dict, so a byte never touches a disk or the network in CI). `object_path(hash)`
is the `{hash[:2]}/{hash}` sharding D23 specifies. Every failure normalizes to `StorageUnavailable`,
never a raw `httpx` error and never one carrying the key or the bytes. `build_store` picks a backend from
`FILES_STORAGE_BACKEND` and is called once from `main.py`'s lifespan (`app.state.object_store`, mirroring
`build_registry`); `deps.py` gained `get_store`/`StoreDep`. Verified against a real Supabase Storage
bucket while building this step, not just the `MockTransport` suite in `tests/unit/test_storage.py`: the
live API wraps a missing object *and* a non-upserted duplicate as a literal HTTP 400 with the true status
as a string inside the JSON body (`{"statusCode": "404", ...}` / `{"statusCode": "409", ...}`) rather than
using that status directly — `SupabaseStore`'s internal `_logical_status` unwraps it, and without that
unwrapping `exists()` would have raised on every ordinary miss instead of returning `False`, which Step
3's dedup check depends on. Step 3 (`POST /v1/files`) is next.

## Phase 3 — Quota-Aware Routing (§4) — complete

**Status: all 11 steps committed, all three milestones done.** **Milestone A** (the router knows: a
candidate that cannot be served is skipped before the round trip), **Milestone B** (the client can
see: live status, a working picker), and **Milestone C** (caching, the gateway's own rate limiting,
and Step 11's tests/ADRs/docs). Config surface, `quota/windows.py`, `quota/tracker.py` +
`quota/scripts/{reserve,commit,release}.lua`, `quota/lanes.py`, both router paths (`route`/`route_stream`)
reserving before each attempt and committing/releasing after, and a `ContextVar` sink in `providers/base.py`
(`publish_hint`/`take_hint`) published by `HttpProviderAdapter._request`/`_stream_events` and drained by the
router after every attempt into `QuotaTracker.apply_hint`, which corrects a counter upward only,
disambiguating `rpm`/`rpd` and `tpm`/`tpd` by the hint's reported reset duration. `GET /v1/models`
(`schemas/models.py` + `api/v1/models.py`, mounted in `main.py`) reports per-candidate and per-slot status
from the breaker's hash and the quota tracker's counters with zero upstream calls — including
`CircuitBreaker.peek`, a read-only sibling of `allows` added so a status read cannot claim a half-open
breaker's one probe slot. The frontend `ModelPicker` (`frontend/components/ModelPicker.tsx`) replaces
`hooks.ts`'s `DEFAULT_SLOT` constant against that live status, and `ErrorState`/`api.ts` render a
`rate_limited` response as a wait rather than a failure. `app/cache/exact.py` (D5/D19) — `request_hash`
over the requested slot, full canonical history and generation knobs; `is_cacheable`, shared verbatim by
the read and write sides; `ExactCache.get`/`put`, failing open in both directions. The non-streaming path
in `api/v1/chat.py` checks the cache before `routing.route`; the streaming path
(`streaming/orchestrator.py::stream_cached_completion`) replays a hit as `meta` → `delta`* → `done` framed
identically to a live stream, chunked on whitespace via `cache/exact.py::chunk_for_replay`, with no
artificial delay. `streaming/collector.py::Collector` writes the cache after a live stream's `done` and
gained `persist_cache_hit` for the replay's own write; `usage/logger.py::record_cache_hit` is the fourth
facade function alongside `record_success`/`record_failure`/`record_stream_failure`, since a hit never
routed and has no `ModelSpec` to log one off. `X-Cache: HIT|MISS|BYPASS` on every response, both paths.
Step 10 (D20, our own rate limiting) is in: `core/errors.py::TooManyRequests` (429, `code="rate_limited"`,
`Retry-After` in delta-seconds), `deps.py::RateLimiter` — a two-bucket sliding window over
`rl:{user_id}:{window}:{window_start}` built on `windows.sliding_count`, keyed on `user_id`, limits from
`limits.yaml`'s `gateway:` block by tier, failing **open** (the opposite of quota's D15 rule, and ADR-022
says why), refunding a rejected request's own increment — composed with the principal into `RateLimitDep`
in `api/v1/chat.py`, which applies it to `POST /v1/chat/completions` and nothing else. Step 11
(tests, ADRs, docs, deploy) is in: seven ADRs (`docs/decisions/ADR-018` through `ADR-024`, covering
D15–D19 and D21 — D20's `ADR-022` landed with Step 10), the reserve/commit/release lifecycle
diagram in `docs/architecture.md`, a new quota/caching/rate-limiting section in
`docs/limitations.md`, and a root `README.md` (new this step) carrying the "why a Lua script and
not a pipeline" answer. Writing `ADR-018` (D15) surfaced a real gap: `/readyz` had never been wired
to fail closed on Redis, so `main.py`'s handler now returns 503 (`code="redis_unavailable"`) when
Redis is unreachable and `QUOTA_ENFORCEMENT` is true — see `ADR-018` for why that is the correct
reading of D15's "closed at the candidate is closed at the request" rule, not scope creep. Full
step breakdown: [phase3.md](doc/reference/phase3.md).

**Scope:** the router stops guessing and starts knowing. A candidate that cannot be served is skipped before
the round trip rather than after the 429, `GET /v1/models` reports live status a client can render, identical
deterministic requests are answered from cache, and the gateway enforces its own limits on its own users.

**Explicitly NOT in Phase 3:** no perception lane (Phase 4 writes `quota_perception_lane()`; this phase only
reserves the budget and builds the accounting seam), no idempotency (D6 is Phase 7), no BYOK (`scope` is
threaded as a real parameter but is `keys.SYSTEM_SCOPE` at every call site until Phase 6), no `pricing.yaml` /
simulated cost / `/metrics` (Phase 7), no semantic cache (exact-match only, per D5).

**Done means:** hammer the `fast` slot until Groq's RPD is spent — `/v1/models` flips it to `rate_limited`
with an accurate `resets_at`, `auto` routes around it without a wasted round trip, and asking for `fast`
explicitly still gets an answer (from a different model, disclosed) rather than a structured refusal — D2
was overridden to silent failover, so quota knowledge only changes *when* the spill happens. Two identical
`temperature: 0` requests: the second returns `X-Cache: HIT`, writes a `cache_hit=true` row, and makes no
provider call.

## Conventions

Python 3.12 · async everywhere (never block the event loop) · SQLAlchemy 2.0 declarative + `AsyncSession` ·
pydantic v2 and pydantic-settings (config fails loudly at startup on a missing var) · structlog JSON with
`request_id`/`user_id` bound as contextvars · ruff + mypy strict · pytest + pytest-asyncio ·
`httpx.MockTransport` for all upstream HTTP.

**Deployment:** Supabase (Postgres + Auth), Upstash (Redis), **Render** (the FastAPI service,
`render.yaml`), Vercel (the frontend). Migrations run in the container's start command, not a
pre-deploy hook — Render's free tier has none. Moved off Fly.io in Phase 3 when its free allowance ran
out; [ADR-017](docs/decisions/ADR-017-render-as-deploy-target.md) has what that changed, and
[docs/deploy.md](docs/deploy.md) is the runbook.

## Hard rules

- **Never call a live provider API from tests.** Record fixtures once via `scripts/record_fixtures.py`; tests read them.
- **Never write an f-string Redis key outside `app/cache/keys.py`.** Every key comes from a builder there.
- **Never store a provider's request body.** Canonical schema only — anything else makes each provider switch a migration.
- **Every conversation read is ownership-scoped in the SQL query itself** (`WHERE id = :cid AND user_id = :uid`). No fetch-then-check; a miss is 404, not 403.
- **Phase 2+ seams are typed signatures raising `NotImplementedError`** — never silently-passing stubs. The signature and its return type are the contract; only the body is deferred.
