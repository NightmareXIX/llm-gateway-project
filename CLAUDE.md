# CLAUDE.md — LLM Gateway operating guide

Free-tier multi-provider LLM gateway: a FastAPI service sitting between clients and Gemini/Groq/OpenRouter,
exposing one OpenAI-shaped API while it owns conversation state, routes across logical model slots, fails over
when a free tier runs out, tracks heterogeneous quota (RPM/RPD/TPM) in Redis, and understands uploaded files
through a separate "perception lane" even when the answering model can't. Portfolio/learning project, runs
entirely on free tiers. Full specs: [contracts-and-phase1.md](doc/reference/contracts-and-phase1.md),
[project-overview.md](doc/reference/project-overview.md), [development-plan.md](doc/reference/development-plan.md),
[phase2.md](doc/reference/phase2.md), [phase3.md](doc/reference/phase3.md), [phase4.md](doc/reference/phase4.md),
[phase5.md](doc/reference/phase5.md).
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
  db/{session.py,models.py,repo/{users,conversations,messages,requests,provider_keys,files,extractions}.py}
frontend/            # Next.js App Router + Tailwind; lib/{sse,files}.ts, components/{ModelIndicator,ModelPicker,Composer,AttachmentChip}.tsx
tests/{conftest.py,fixtures/{provider_responses,golden_payloads,files},unit,contract,integration}
scripts/{record_fixtures,chaos_demo,seed_dev}.py
docs/{architecture.md,limitations.md,decisions/}      # ADRs; doc/reference/ holds the source specs
README.md  Makefile  pyproject.toml  docker-compose.yml  Dockerfile  .env.example  .github/workflows/ci.yml
```

## Current phase: Phase 5 — Memory & Cross-Provider Translation

Phase 1 (single-provider proxy), Phase 2 (multi-provider core, failover, streaming), Phase 3
(quota-aware routing), and Phase 4 (the perception lane, below) are done and merged. Phase 5
(conversations surviving a provider switch, per `project-overview.md` §4.7 and
`development-plan.md` §3 Phase 5) is next per the phased build plan. Full step-by-step account,
including the five pre-code decisions (D31–D35) and the eight-step, three-milestone plan:
[phase5.md](doc/reference/phase5.md).

**Status: Milestones A and B complete, Milestone C underway — Steps 1–6 of 8 committed.** Step 1 (D31, the cross-provider
golden matrix, §2.2.6) touched only `tests/`: no `app/` change was needed, meaning `render()`
already agreed with every committed `build_payload` golden — the finding trap 2 warns a real
disagreement would have produced, and did not. `tests/provider_fixtures.py` gained
`ScriptedResolver` (a `render.AttachmentResolver` test double answering only "native, or injected
with a fixed extraction text?" — no database, no Redis, no `PerceptionResolver` import) and the
`gemini_spec()`/`groq_spec()`/`openrouter_spec()`/`general_params()` builders the three per-adapter
payload suites now import rather than redefine. `tests/contract/test_cross_provider_matrix.py`
renders one fixed history through `render()` against all three adapters, with and without an
attachment, and asserts six goldens — the three `*_general` ones and `gemini_attachment` reused
byte-for-byte from the existing per-adapter suites, `groq_attachment` and `openrouter_attachment`
newly committed — plus three structural properties: the system message's position per provider,
the omission marker surviving into all three payload texts identically, and the extracted-document
envelope being byte-identical across the two injected providers. Fixing this step also surfaced
(and fixed) a latent Windows-only bug in every `--bless` script: `Path.write_text` translates `\n`
to `\r\n` on write, which had left three of the four pre-existing goldens carrying CRLF against
`.gitattributes`' own `-text` byte-exactness contract; `provider_fixtures.py::write_golden` is now
the one place any bless script writes a file, forcing `\n`. `make test`, `ruff check`, `ruff format
--check`, and `mypy` are green.

Step 2 (continuity across a provider switch, end to end) touched only
`tests/integration/test_chat_endpoint.py` — the file it names as its sole allowed target — adding
three tests plus a `_narrow_slot` config helper and an `attachment_fleet` fixture (Groq-only `fast`,
Gemini-only `general`) beside the module's existing `_groq_only`/`fleet_script`.
`test_a_thread_survives_a_provider_switch` drives one conversation through three turns and three
genuinely different providers — `fast`/Groq, then `general`/Gemini reached by an in-slot failover,
then `general`/OpenRouter reached by a second failover after Gemini also fails — and asserts turn
one's user text survives into turn three's payload, the three provider shapes differ exactly where
expected (Gemini's top-level `contents`/`parts` on turn two, OpenAI-shaped `messages` on turns one
and three), and the conversation holds six messages in gap-free `seq` order. Every scripted failure
uses `AuthFailed` (not `RateLimited` or `Unavailable`): the router retries an `Unavailable` once on
the same candidate before failing over (`router.py`'s `MAX_SAME_PROVIDER_RETRIES`), which would
have starved D1's 3-attempt cap before OpenRouter was ever reached, and a single `RateLimited`
opens the breaker immediately (`circuit_breaker.py`), which would have turned turn three's second
Groq attempt into a breaker-skip instead of the real failure the test is asserting on — `AuthFailed`
is failover-eligible with neither property, so one scripted fixture buys exactly one real attempt.
`test_a_file_ref_survives_a_provider_switch_from_cache` uploads once, asks about it on `fast` (an
extraction), then asks a follow-up on `general` *without* repeating `file_refs` (render step 1 scans
the whole stored history for `file_ref` blocks). Writing it surfaced a real finding, corrected in
`phase5.md` rather than swept away: the second turn's tier is `cache`, not `native` — `lane.py`'s
tier 0 check returns a stored `llm` reading unconditionally, before tier 1 ever asks whether Gemini
could have read the file itself, so Gemini's second answer also carries the `<document>` envelope
rather than `inline_data`. That is `docs/limitations.md`'s already-documented "cache-beats-native on
layout questions" trade-off, exercised by a test for the first time, and exactly the `cache`-or-
`native` hedge §1's own definition of done already carries.
`test_streaming_and_non_streaming_agree_on_a_switched_thread` runs the same two-turn switch twice —
non-streaming and with the second turn streamed — as two independent conversations (a JSON fixture
and an SSE fixture legitimately differ in wording, so the assertion is on role/provider shape and
the user's own words, not exact assistant text) and checks the `done` event's `served_by` against
the non-streaming twin's. Per the step's own acceptance
criterion, all three were confirmed to keep passing with `pinned=` temporarily deleted from both of
`chat.py`'s `routing.route`/`route_stream` calls — they test memory, not pinning — before that
change was reverted. `make test`, `ruff check`, `ruff format --check`, and `mypy` are green.

Step 3 (D4 under a real history: fitting exercised, truncation disclosed, implementing D34) touched
exactly the six files `phase5.md` names — `memory/canonical.py`, `schemas/chat.py`, `streaming/sse.py`,
`streaming/orchestrator.py`, `streaming/collector.py`, `api/v1/chat.py` — plus tests. `MessageMeta`
gained `messages_dropped: int = 0` (`to_jsonb`/`from_jsonb` via the existing `_int` helper; an absent
key reads back as `0` rather than being backfilled, trap 7 — nobody knows whether a pre-Phase-5 row was
truncated). The same field was threaded through `ChatCompletionResponse`, `DoneEvent`, `StreamResult`
and `_Turn` by mirroring exactly how `degraded`/`extraction_tier` made the same trip in Phase 4:
`_Turn.complete` sets it from `event.report.messages_dropped`, both `done_event()` and `result()` read
it off `_Turn`, and a failed turn — which never reaches `complete` — reports the field's `0` default
rather than a stale number, the same as `degraded`/`extraction_tier` do on that path. `stream_cached_completion`
and `_serve_cache_hit`'s non-streaming twin both pass `messages_dropped=0` explicitly: a cache hit never
re-rendered, so the number belongs to the original turn's own stored row, not the replay (trap 3). Two
new helpers in `tests/integration/test_chat_endpoint.py` — `_shrink_context` (`_narrow_slot`'s sibling:
narrows a slot to one provider and overrides its `context_tokens`, so a real multi-turn history forces D4
truncation without touching `config/providers.yaml` or the fitting algorithm) and `_seed_history` (appends
turns directly through `messages_repo.append`, so a 200-message fixture costs one flush per row rather than
200 HTTP round trips) — back three new tests: `test_a_truncated_answer_discloses_how_much_history_it_dropped`
(non-streaming — response, stored `meta`, and the single omission marker in the outbound payload all
agree), `test_streaming_done_event_discloses_the_same_truncation` (the `done` event's twin), and
`test_a_200_message_history_truncates_without_a_provider_error` — `development-plan.md`'s own exit
criterion, named literally. Two new unit tests in `tests/unit/test_canonical.py` cover the
`MessageMeta` round trip and the absent-key default. `make test`, `ruff check`, `ruff format --check`,
and `mypy` are green.

Step 4 (D35: truncation and the exact cache) touched exactly the three files `phase5.md` names —
`cache/exact.py`, `api/v1/chat.py`, `streaming/collector.py` — plus tests. `is_cacheable` gained a third
parameter, `truncated: bool = False`, refusing to cache whenever it is `True`; the read side at both
call sites in `chat.py` still supplies neither `degraded` nor `truncated` and defaults permissive on
both, since a stored entry is by construction a whole-history answer and a second gate on the read side
would be dead code. The non-streaming write side now passes `truncated=outcome.report.truncated`
alongside the existing `degraded=`. The streaming write side has no parallel `StreamResult.truncated`
field — `Collector._persist_success`'s cache-write gate was extended to `not result.degraded and
result.messages_dropped == 0`, reusing the field Step 3 already threaded onto `StreamResult` rather than
adding a second one carrying the same fact. Two new unit tests in `tests/unit/test_exact_cache.py` cover
`truncated=True` refusing on its own and the two axes (`degraded`/`truncated`) refusing independently.
Two new integration tests in `tests/integration/test_chat_cache.py` —
`test_a_truncated_turn_is_never_cached` and `test_an_untruncated_turn_over_a_long_history_still_caches`
— drive the same seeded-long-history shape Step 3's tests built, but as two separately-seeded
conversations rather than one growing thread: `request_hash` keys on content and never on
`conversation_id` (D19), so two conversations built from the same deterministic `_seed_history` shape
hash identically, which is what lets "the same question asked twice" be asserted without a growing
history changing the hash out from under the test. The first test shrinks the slot's context window
(`_shrink_context`) so the fitting step drops messages on both requests and both come back `X-Cache:
MISS` with two real provider calls; the second leaves the window at its real, large default so nothing
is dropped and the pair still produces a `MISS` then a `HIT` — the gate refusing a truncated write
without breaking caching in general. `make test`, `ruff check`, `ruff format --check`, and `mypy` are
green.

Step 5 (D33's server half: the thread remembers what you picked) touched exactly the two files
`phase5.md` names — `db/repo/conversations.py`, `api/v1/chat.py` — plus tests. `conversations_repo.touch`
gained a fourth keyword, `preferred_slot: str | None = None`, folded into the same `UPDATE` as the
`updated_at` bump rather than a second round trip; passing `None` (Phase 1's create path, and any future
caller that only wants the activity bump) leaves the column untouched, and the function does not compare
the new value against the old one — the WHERE clause already limits the statement to one row, so writing
the same value back costs nothing beyond the UPDATE that was already happening. `chat.py`'s call site now
passes `preferred_slot=body.model` on every turn, with a comment on the D33 distinction at the call site:
a **preference** is what the composer seeds from next time and the user overrides in one click, a **pin**
is what `routing.route`'s `pinned=` argument enforces and the client cannot override at all — the same
column must never carry both facts. New tests: `test_repo_conversations.py` covers `touch` writing the
slot, leaving it alone when `None`, and refusing to move it for a non-owner;
`test_conversations_endpoints.py::test_preferred_slot_follows_the_last_requested_slot` drives two turns
under different slots and asserts `GET /v1/conversations/{id}` reflects the second, and
`test_someone_elses_failed_turn_does_not_move_preferred_slot` confirms a 404'd cross-user turn never
reaches `touch` at all; `test_chat_endpoint.py::test_a_pinned_conversations_preferred_slot_still_tracks_the_request`
extends the existing pin test to assert `preferred_slot` keeps recording what was *asked* for
(`general`) even while the pin silently serves a different model — proving D33 and D3 stay two distinct
facts rather than converging under a pin. No frontend change — `ConversationView`'s seeding from
`preferred_slot` is Step 7. `make test`, `ruff check`, `ruff format --check`, and `mypy` are green.

Step 6 (D32: D3's write path — pinning and the `warning` field) touched exactly the files `phase5.md`
names — `db/repo/conversations.py`, `memory/canonical.py`, `schemas/chat.py`, `streaming/sse.py`,
`streaming/orchestrator.py`, `api/v1/chat.py` — plus tests. `conversations_repo.set_pinned` is
ownership-scoped like every sibling and idempotent for a re-pin to the same model; a re-pin to a
*different* model is refused by the WHERE clause itself (`pinned_model IS NULL OR pinned_model =
:model`) rather than by raising, so an UPDATE that would have moved an existing pin matches zero rows
instead. `canonical.pin_target(history) -> str | None` is D32's pure trigger predicate: the first
message carrying a `tool_call`/`tool_result` block, off a new `_TOOL_BLOCK_TYPES` subset of
`RESERVED_BLOCK_TYPES` that deliberately excludes `summary` — that type is reserved for the unrelated
§2.2.7 seam, and folding it in would fire D3's pin on the wrong feature landing first. Per D32(c), this
is a complete, reachable mechanism whose only deferred part is the trigger: `parse_block` rejects
`tool_call`/`tool_result` at the database boundary, so `pin_target` returns `None` for every history v1
can actually store, and the branch that returns otherwise is exercised only by a hand-built
`CanonicalMessage` in a unit test — content is a plain list, not the JSONB boundary that guard sits at.
The `warning` field (`ChatCompletionResponse` and `DoneEvent`) is built by one shared helper,
`selection.pin_warning(pinned_model, requested_slot, served_slot)`, read by both the non-streaming path
and — via a new `pinned` field threaded onto `orchestrator._Turn`, off `stream_completion`'s existing
`pinned` argument — the streaming `done` event. It is deliberately not `is_substitution` reused under a
new name: that predicate excuses `auto` because ordinary routing choosing on the client's behalf is not
an override, but a *pin* overriding `auto` very much is one — `auto`'s whole promise is "you choose",
and the pin took that choice away too — so `pin_warning` fires whenever the served slot differs from
what was requested, `auto` included, and stays silent only when the request already named the slot the
pin resolves to. `chat.py`'s main success path calls `pin_target([*history, assistant])` and, on a
non-`None` result with `conversation.pinned_model is None`, calls `set_pinned` in the same transaction as
the assistant row — unreachable today for the same reason `pin_target` is, but wired rather than left a
seam. The D19 cache-hit path also discloses `warning`, off a new `pinned_model` parameter on
`_serve_cache_hit`: a hit never re-routes, but a pinned conversation can still have a cached answer sitting
on a different slot than the pin would now enforce, and that is exactly the case the disclosure exists
for. New tests: `test_canonical.py` covers `pin_target` returning `None` for every history v1 can store,
finding a hand-built `tool_call` block, a `tool_result` block, the first match among several, and ignoring
a `summary` block; `test_repo_conversations.py` covers `set_pinned` writing the model, idempotent re-pins,
refusing to move an existing pin, and ownership scoping; `test_chat_endpoint.py` extends
`test_a_pinned_conversation_ignores_the_requested_slot` to assert `warning` is null when unpinned, carries
the disclosure when a pin overrides `auto`, and is null again once a later turn asks for the slot the pin
already resolves to, plus new `test_an_unpinned_turn_carries_no_warning` and
`test_a_pinned_conversations_done_event_carries_the_warning_too` for the streaming twin. No frontend
change — Step 7. `make test`, `ruff check`, `ruff format --check`, and `mypy` are green.

**Scope, from `development-plan.md`:** persist canonical history and load it by `conversation_id`;
a golden-file test matrix asserting one fixed canonical history (system prompt + `file_ref`,
Phase 4's own attachment case extended to Groq and OpenRouter) produces the correct payload shape
per provider — this is where Gemini's top-level `system_instruction` vs. OpenAI-shaped in-array
`role: "system"` gets handled once, correctly; context-window fitting per D4 (already built, now
exercised by real multi-turn history rather than single-request truncation); tool-call pinning per
D3 (`conversations.pinned_model`, still written by nobody); and a `docs/limitations.md` entry
explaining why tool-call translation across providers is out of scope, not just that it is.

**What Phase 4 hands to Phase 5, per `phase4.md` §9:** the render pipeline runs all six steps for
real for the first time, so Phase 5's golden matrix is writable because step 1 resolves something —
`tests/fixtures/golden_payloads/gemini_attachment.json` (Step 9) is the first attachment case, ready
to extend to the other two providers. `MessageMeta` is fully populated for the first time —
provenance from Phase 2, tokens from Phase 3, `extraction_tier`/`degraded` from Phase 4 — so Phase
5's continuity test ("start on `fast`, switch to `general`, ask what I said first") reads a complete
history. D4's fitting step has a real adversary for the first time: a 200-page PDF makes document
truncation a path that runs rather than one that is only tested. Left deliberately unbuilt, with the
seam visible: `conversations.pinned_model` (D3), `memory/summarize.py` (still the seam, `summary`
block type still reserved and rejected), `fitting.FitStrategy` (still one implemented member).
Untouched by design: `keys.idempotency` (D6, Phase 7), `keys_resolution/resolver.py` (BYOK, Phase
6), `config/pricing.yaml` (Phase 7) — `scope` stays `keys.SYSTEM_SCOPE` in both lanes until Phase 6
replaces the one constant.

## Phase 4 — Perception Lane (§4) — complete

**Status: all 12 steps committed, all three milestones done.** **Milestone A** (Steps 1–4, the bytes
land: upload, own, and reference a file — a turn that references one fails loudly until Milestone B
gives the resolver something to do), **Milestone B** (Steps 5–9, the models can see: all four tiers
wired into render, on both the streaming and non-streaming paths, with a natively attached file's
token cost finally counted), and **Milestone C** (Steps 10–12, honest and shippable: cache, frontend,
tests, and this step's ADRs/docs/deploy). Full step-by-step account: [phase4.md](doc/reference/phase4.md)
(D22–D30).

`app/perception/` gained its four modules. `storage.py` — `ObjectStore` (D23), a `Protocol` with
`SupabaseStore` (every deployed environment, over the shared `httpx.AsyncClient`), `LocalStore` (a
dev box), `MemoryStore` (tests); the bucket is private with no download path anywhere in this phase,
and every failure normalizes to `StorageUnavailable`, never a raw `httpx` error or one carrying the
key or the bytes. `extractors.py` — tier 2, a normal provider call pointed at the internal
`perception` slot (D26), running D28's four-section prompt (`Summary` first, `Verbatim text` last,
because `fitting.py` truncates from the tail), guarded by `lock:extract:{hash}` so two simultaneous
uploads of one document spend one provider call, write-through to `file_extractions` then
`extract:{hash}` in Redis. `local.py` — tier 3, PyMuPDF's text layer or rasterize-and-OCR via
Tesseract, entirely inside `asyncio.to_thread` (trap 4), OCR detected at startup rather than
assumed (D30), and also home to `measure()` (D27's page/pixel geometry). `lane.py` —
`PerceptionResolver`, the first real `render.AttachmentResolver`, walking D25's chain (`cache` →
`native` → `llm` → `local`) with `_guarded` enforcing "every tier but the last logs and falls
through" (trap 12) in one place, memoized per request on `(file_hash, native_wanted)` (D22, trap 6),
and carrying D27's token-cost rate table (`TOKENS_PER_TILE = 258`, Google's published rate).

`quota/lanes.py::reserve_perception`/`commit_perception`/`release_perception` fill in the seam
Phase 3 left typed, fencing D8's daily half through Contract C's one `lane:perception` key while
sharing `rpm`/`tpm` against the full published ceiling (D26,
[ADR-027](docs/decisions/ADR-027-perception-quota-under-frozen-contract-c.md)) — a native
passthrough makes *no* perception reservation at all, since its cost already rides in the answering
model's own reservation via `token_cost` (trap 7). `db/repo/files.py` and `db/repo/extractions.py`
are the two new repo modules; `file_extractions` is the one table in the schema with no `user_id`
column, keyed on the hash alone (D24), with an upgrade-only upsert that lets an `llm` row replace a
`local` one but never the reverse — invariant 6's retroactive improvement, implemented. Chat accepts
`file_refs` per message, resolved in one ownership-scoped query before any message is written (D24,
404 never 403); `MessageMeta.extraction_tier` and `RenderReport.degraded`/`extraction_tier` carry
real values on both the streaming and non-streaming paths. Gemini's adapter emits `inline_data`
beside the text part for a native attachment; Groq's and OpenRouter's refusals stay and stay
correct. `cache/exact.py`'s `is_cacheable` dropped its blanket `file_ref` exclusion — `request_hash`
now folds in each attachment's hash alone (D29, reasoned through in that module's own docstring),
with the write-side `degraded` gate doing the safety work a blanket exclusion used to (trap 14). The frontend composer uploads on
selection rather than on send, discloses the perception lane's third-party-extraction privacy trade
before the send that would trigger it, and `ModelIndicator` explains *why* an answer is degraded —
`native` names the answering model, `llm` says "read by another model" because nothing on the wire
names which perception candidate won, `local`/`cache` are sharpened by `degraded`.

Six ADRs landed with Step 12 — `ADR-025` (D22, extraction at render not upload), `ADR-026` (D23/D24,
storage and ownership), `ADR-027` (D26, the daily fence under a frozen Contract C), `ADR-028` (D25,
the tier chain, plus D28's prompt-section ordering), `ADR-029` (D27, attachment token cost), and
`ADR-030` (D30, the local tier's dependencies) — D29's cache-identity reasoning stays where it was
written, in `cache/exact.py`'s own docstring, rather than getting a seventh ADR. Also landed: a
two-lane diagram and a tier-chain flowchart in `docs/architecture.md` replacing the
three-line stub Phase 3 left, a new "The perception lane" section in `docs/limitations.md` (first-turn
extraction latency, the hour-long window where a cached `llm` answer can outlive a newly-available
`native` path, cache-beats-native on layout questions, the OCR page cap, PyMuPDF's AGPL licence, and
the third-party-extraction privacy disclosure), a new Supabase Storage bucket-creation section in
`docs/deploy.md` (create it, keep it private, and watch the Docker build for a timeout on Render's
free tier now that the image carries Tesseract), and a second headline-feature section in
`README.md`. `make test`, `make lint`, `make typecheck`, `make frontend-test` and `make
frontend-lint` all green as of Step 11; Step 12 changed no application code.

**Scope:** upload a PDF or an image, and every model can "see" it — natively for the model that reads
files, via a dedicated extraction call for the model that cannot, and via local OCR/PDF-text-layer
when every provider option is spent, degraded and labelled as such.

**Explicitly NOT in Phase 4:** no file management UI (no browser, no delete, no re-download —
`GET /v1/files/{hash}` returns metadata only), no summarization (D4's fitting step still truncates;
D28's prompt ordering is what keeps a truncated document's summary intact instead), no BYOK (`scope`
is `keys.SYSTEM_SCOPE` at every perception-lane call site, same as the answer lane, until Phase 6),
no audio/video/office formats (the allowlist is PDF, PNG, JPEG, WebP — a format with no tier-3
fallback is a format that fails at 3am rather than degrading), no asynchronous extraction or job
queue (D22 puts it inside the request that needs it — a background worker is a second runtime a
free tier does not have room for), no `pricing.yaml`/simulated cost/`/metrics`/idempotency (still
Phase 7).

**Done means, and verified live end to end across every step:** upload a PDF, ask about it on `fast`
(Groq, text-only) — correct answer, `extraction_tier: "llm"`, a real Gemini extraction in the log.
Ask the same question on `general` with Gemini serving — correct answer, no extraction ran, tier
`native`, and `rpd` spent while `lane:perception` stayed untouched (trap 7, proven on live
counters). Ask a third time — tier `cache`, zero provider calls inside the lane, `X-Cache: HIT`
before the lane even runs. Set `PERCEPTION_LOCAL_ONLY=true` and ask again — the answer still
arrives, worse, `degraded: true`, disclosed as read locally, zero Gemini calls.

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
