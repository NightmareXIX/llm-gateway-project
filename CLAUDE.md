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
  db/{session.py,models.py,repo/{users,conversations,messages,requests,provider_keys,files,extractions}.py}
frontend/            # Next.js App Router + Tailwind; lib/{sse,files}.ts, components/{ModelIndicator,ModelPicker,Composer,AttachmentChip}.tsx
tests/{conftest.py,fixtures/{provider_responses,golden_payloads,files},unit,contract,integration}
scripts/{record_fixtures,chaos_demo,seed_dev}.py
docs/{architecture.md,limitations.md,decisions/}      # ADRs; doc/reference/ holds the source specs
README.md  Makefile  pyproject.toml  docker-compose.yml  Dockerfile  .env.example  .github/workflows/ci.yml
```

## Current phase: Phase 4 — Perception Lane, Step 11 of 12 done

Phase 1 (single-provider proxy), Phase 2 (multi-provider core, failover, streaming), and Phase 3
(quota-aware routing, below) are done and merged. Phase 4 (file/image understanding via a dedicated
perception lane, per `project-overview.md` §4.5 and `development-plan.md` §3) is next per the phased
build plan; its step-by-step plan is [phase4.md](doc/reference/phase4.md) (D22–D30). Milestones A
(Steps 1–4, the bytes land) and B (Steps 5–9, the models can see) are done; Milestone C (honest and
shippable) is under way.

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
3's dedup check depends on.

**Step 3 (`POST /v1/files`) is in.** The upload endpoint, and the only thing in the system that puts
bytes into it. It stores bytes and metadata and *nothing else* — no extraction runs here (D22), so the
route either succeeds or does not and there is no partial-success state to define. `app/api/v1/files.py`
parses the multipart body **itself**, incrementally out of `request.stream()` via `python_multipart`'s
`MultipartParser` and a small `_FilePartReader` callback set, rather than declaring an `UploadFile`
parameter: FastAPI finishes parsing (and spooling to an unbounded `SpooledTemporaryFile`) before the
handler runs, so a cap checked afterwards has already lost — trap 3 is enforced by counting bytes as they
arrive and raising `PayloadTooLarge` from inside `on_part_data`, which abandons the `async for` and never
reads the rest. `sniff_mime` reads the leading 12 bytes (PDF, PNG, JPEG and WebP magic numbers; the WebP
check is why 12 and not 8); **the sniffer's range is the allowlist**, so there is no second list to drift
— an unrecognized format is a 415 rather than something handed to PyMuPDF to find out (trap 2), and a
declared/sniffed mismatch is an info log, because the sniffed type is what gets stored either way. Dedup
runs *before* the store is touched: a hash the caller already owns returns the existing row with
`deduplicated: true` and writes nothing; a hash somebody else uploaded skips `put` (guarded by
`ObjectStore.exists`) but still writes the row, because bytes are global and the right to reference them
is not (D24). 201 either way — the status promises "this hash is now yours to reference", and
`deduplicated` says whether bytes moved. `GET /v1/files/{file_hash}` returns metadata only,
ownership-scoped in the query, 404 on a miss and never 403. `core/errors.py` gained `PayloadTooLarge`
(413) and `UnsupportedMediaType` (415) — both codes were already in `_CODE_BY_STATUS`. `db/repo/files.py`
has `get_owned` and `create_if_absent` (an `ON CONFLICT DO NOTHING` against `uq_files_user_id_file_hash`
returning `(row, created)`, so two simultaneous uploads of one file by one user are two 201s and not a
500) and deliberately **no** "does anybody own this hash" query — whether the object exists is the
store's question, not a table another user could probe. `schemas/files.py` carries
`FileOut`/`FileUploadResponse` and `FILE_HASH_PATTERN`, which Step 4's `InputMessage.file_refs` reuses.
D20's rate limiter now gates uploads too, so `enforce_rate_limit`/`RateLimitDep` moved from
`api/v1/chat.py` to `app/auth/dependency.py` — two route modules sharing a dependency through a third,
rather than one importing the other. Verified end to end against the real Supabase bucket, not only
`tests/integration/test_files_endpoint.py`: a real PyMuPDF-built PDF uploads, re-uploads as a dedup hit
with the same `created_at`, reads back byte-identical through the service-role key and 400s through the
public URL, a PNG named `report.pdf` stores as `image/png`, an `.exe` named `invoice.pdf` is a 415, and
an 11MB body is a 413 — with no service-role key anywhere in the logs.

**Step 4 (`file_refs` on a chat turn) is in — Milestone A is done.** Nothing resolves an attachment
yet, so this step's success condition is a *loud* failure: a turn with an attachment reaches render
step 1 and the default `NoAttachments` resolver raises `NotImplementedError`, exactly as it was written
to since Phase 1 — the last time that assertion is ever green, since Milestone B gives the resolver
something real to do. `schemas/chat.py`'s `InputMessage` gained `file_refs: list[FileHash]`, capped at
4, each pattern-validated against `schemas/files.py::FILE_HASH_PATTERN` (the one import `chat.py` takes
from outside its own module, so a hash's shape is defined once) — a malformed hash is a 422 from
pydantic, never a database round trip. Per-message rather than per-request: a `file_ref` is content,
and content belongs to the message it was attached to. `api/v1/chat.py::_resolve_file_refs` is D24's
gate — one query over the union of every message's hashes via `db/repo/files.py`'s new
`get_owned_many` (`WHERE file_hash = ANY(:hashes) AND user_id = :uid`), run once before a single
message row is written; a hash this caller does not own is a 404 naming it (`code="file_not_found"`,
same rule `GET /v1/files/{hash}` and `conversations` already follow — never a 403, since that would
confirm the hash names real bytes). Each resolved hash becomes a `canonical.file_ref_block` appended
**after** the message's text block, so a stored message reads "what I asked" then "what I attached".
`frontend/lib/types.ts`'s `InputMessage` gained the matching optional `file_refs?: string[]`.

**Step 5 (`quota/lanes.py`'s reserve → commit/release lifecycle) is in — Milestone B is under way.**
The budget D8 fenced off since Phase 3 is now spendable, and nothing spends it yet. `lanes.reserve_perception`
is no longer the typed `NotImplementedError` stub; it walks Contract C's one perception key
(`keys.quota_perception_lane`) against the daily half of `lanes.perception_budget`, and the *shared*
`rpm`/`tpm` counters against their full published ceiling rather than the answer lane's halved one (D26) —
a per-minute collision between a chat turn and an extraction resolves itself in under sixty seconds, so
only the daily window is actually fenced. `lanes.commit_perception`/`release_perception` delegate straight
to `QuotaTracker.commit`/`release`; the reservation itself already knows which keys it touched, so neither
needed lane-specific logic of its own. The one honest awkwardness the phase plan calls out —
`QuotaTracker._effective_limit` bakes `lanes.answer_share` into every limit it computes, which is wrong
for a lane that isn't the answer lane — is resolved by pulling `reserve`'s body into a new
`QuotaTracker.reserve_windows`, driven by an explicit `WindowGrant` (label, ceiling, reset policy, actual
Redis key) per window; `reserve()` is now a thin wrapper that derives its grants from `_budget(spec)` the
way it always did, and `reserve_perception` builds its own — pointing the daily grant's key at
`lane:perception` instead of `:rpd` while sharing `rpm`/`tpm`'s key outright. `Reservation` gained
`counter_keys`, populated alongside `windows`, so `commit`/`release` settle exactly the keys `reserve`
touched instead of re-deriving them from the window label (which would send a perception commit at `:rpd`
that was never incremented); a hand-built `Reservation` with no `counter_keys` — the shape every existing
caller and test used before this step — still falls back to the old derivation. No Lua changed. Verified
end to end against the real `config/providers.yaml`/`config/limits.yaml`: a chat reservation and a
perception reservation on the same Gemini model land on separate daily counters, share the same `rpm`
counter, and `commit_perception` settles the shared `tpm` counter correctly.

**Step 6 (`app/perception/extractors.py`, tier 2) is in.** The half of Gemini's budget Step 5 made
spendable is now spent by something: a model that can read a PDF is asked to describe one for a model
that cannot. Deliberately the least novel module in the phase — same adapters, same breaker, same
normalized errors, same reserve-before/commit-after discipline, pointed at the internal `perception`
slot and paid for through `lanes.reserve_perception`/`commit_perception`/`release_perception`. Four
things are *not* the answer lane's: **the prompt is committed code** (`EXTRACTION_PROMPT`, D28's four
sections with `Summary` first and `Verbatim text` last, because everything downstream truncates from the
tail — trap 10), pinned by a golden payload (`tests/fixtures/golden_payloads/gemini_extraction.json`)
via `_build_payload`, the same discipline every `build_payload` gets; **there is no streaming**, since
an extraction has no reader waiting on its first token; **a total failure is not an error** — a chain
that runs out of candidates returns `None` and the lane will fall to tier 3 (D25, trap 12), so
`ContentFiltered`/`BadRequest` stop the chain and everything failover-eligible moves on, read off
Contract A's class flags; and **one extraction per hash**, guarded by `SET lock:extract:{hash} NX EX 60`
with a token checked before release (trap 16), where a loser polls tier 0 for `LOCK_WAIT_S` and then
extracts anyway rather than hanging the turn. `grade()` returns `high` only for all four headings *plus*
a verbatim block over `MIN_VERBATIM_CHARS`, and **never `low`** — `low` is tier 3's marker and the thing
that sets `degraded`, so a thin-but-real reading must not light the "read by local OCR" disclosure.
Write-through is Postgres first (`db/repo/extractions.py`, new this step — the one table with no
`user_id` column, D24, and an `ON CONFLICT DO UPDATE ... WHERE tier = 'local'` that lets an `llm` row
upgrade a local one but never the reverse, which is invariant 6's retroactive improvement in one
direction and outage protection in the other), then `extract:{hash}` in Redis, where a failure is a log
line. `load_cached` is tier 0 in full (Redis, then Postgres, writing a row-hit back up) and Step 8's
lane will reuse it. Step 9's work arrived early only where Step 6 could not proceed without it:
`gemini.py`'s `_render_attachment` `NotImplementedError` is gone, replaced by `_render_parts` /
`_native_parts` emitting an `inline_data` part beside the message's text part — `estimate_tokens` still
ignores every non-`text` part (trap 9), and Groq's and OpenRouter's refusals stay. `ResolvedAttachment`
still has no `token_cost`/`pages`, and `fitting.py`/`render.py` are untouched: those, plus the full
attachment golden, are still Step 9. Verified end to end against live Gemini, live Upstash and live
Postgres, not only the fixture suite: a real two-page PyMuPDF-built PDF came back with all four sections
grading `high`, `lane:perception` went to 1 while `:rpd` stayed untouched and the shared `:rpm` went to
1, the row and the cache key were written, the lock was released, and the second call was served from
tier 0 with no provider round trip and no new spend.

**Step 7 (`app/perception/local.py`, tier 3) is in.** The last resort, standalone — nothing wires it
into the lane yet, since that seam is Step 8's. It never touches a provider, a breaker or quota; the
only thing it shares with tier 2 is the `Extraction` dataclass and D28's four-section envelope
(`extractors.SECTION_*`, reused rather than duplicated), so a tier-3 reading is shaped identically to
a tier-2 one — `provider`/`model` come back `"local"`/`"local"` rather than `None`, matching
`Extraction`'s own reasoning for why that column isn't nullable. A PDF's embedded text layer wins when
its average density clears `TEXT_LAYER_MIN_CHARS_PER_PAGE`, grading `medium`; below that it rasterizes
at `RASTER_DPI` (150) and OCRs up to `max_ocr_pages` pages, grading `low` and, over the cap, saying in
the text itself which pages were skipped (trap 15). An image goes straight to OCR, `low`. Both PyMuPDF
and Tesseract are synchronous and CPU-bound, so `extract_locally`'s entire body is one
`asyncio.to_thread` call (trap 4) — nothing here ever blocks the event loop. OCR availability is
probed lazily against the real binary (`pytesseract.get_tesseract_version()`), cached for the process
(D30's `ocr_available()`); with it unavailable, a text-layer PDF still reads and everything else
returns `None`, which the lane will read as `FileUnreadable`. `pymupdf`/`pytesseract`/`pillow`/
`python-multipart` were declared in `pyproject.toml` since Step 1 but never actually installed —
`uv.lock` now carries them, and `pyproject.toml` gained a mypy override (`follow_imports = "skip"` on
both third-party modules) since PyMuPDF ships a partial `.pyi` that types the import but not every
call, and pytesseract ships none at all. The three committed fixtures
(`tests/fixtures/files/{text_layer.pdf,scanned.pdf,chart.png}`) are generated by a documented one-liner
in the test module's own docstring rather than hand-crafted. Verified end to end on this machine,
which genuinely has no `tesseract` on its `PATH`: the text-layer PDF extracts without the OCR probe
ever being called, the scanned PDF and the PNG both correctly return `None` under a real "binary
missing" probe, and the OCR-success paths (mocked at the `pytesseract.image_to_string` seam for
determinism, plus one `skipif`-guarded test that runs the real binary when present) all grade `low`
and cap a 40-page synthetic scan at 10 pages.

**Step 8 (`app/perception/lane.py` and wiring the resolver in) is in — the feature works end to end.**
Every seam Phases 1–3 left now carries something: `PerceptionResolver` is the first real
`render.AttachmentResolver`, `route(resolver=…)`/`route_stream(resolver=…)` get their first non-`None`
caller (no router change), `ResolvedAttachment` is built per request, `RenderReport.degraded` is
reachable, and `MessageMeta.extraction_tier` is set. The lane answers one question per file per
candidate — *how does this file reach this model* — walking D25's chain (`cache` → `native` → `llm` →
`local`) with `_guarded` enforcing trap 12 in one place: every tier but the last turns an exception
into a log line and falls through, and only `FileUnreadable` (new in `core/errors.py`, 422,
`code="file_unreadable"`, naming the file) surfaces. Bytes are fetched lazily and at most once per
chain (`_Lazy`), so the common case — a second turn about a document — touches object storage not at
all. **Two decisions the phase plan left implicit.** D25 says tier 0 beats tier 1 because "the cached
text came out of the same model that would have read the bytes" — true of a tier-2 row, false of a
tier-3 one, so a stored `llm` row wins outright while a stored `local` row is held back as a
*fallback* below tier 2; without that, one reading taken during a Gemini outage would serve that
document forever and `repo/extractions.py`'s upgrade clause could never fire. And
`PERCEPTION_LOCAL_ONLY` skips tier 0 as well as tier 2, and does not persist what tier 3 produced —
otherwise the demo answers perfectly from a stored model reading, and a deliberately degraded reading
outlives the demo in a global, content-addressed table. The memo is per-request and keyed on
`(file_hash, native_wanted)` (D22, trap 6), under a per-key `asyncio.Lock`; `deps.get_resolver` builds
it from six *sub-dependencies* rather than off `request.app.state`, because `get_session_factory` is
overridden in tests and a direct call walks past the override. `RenderReport` gained
`extraction_tier`, computed by `render.worst_tier` — which lives in `render.py`, not `lane.py`, since
`lane` reaches `render` transitively through the adapters and the import the other way would close the
loop. It ranks `native` < `llm` < `cache` < `local`, by how directly the answering model saw the
document, with `cache` conservatively below `llm` because the label does not say which tier wrote the
row it replays. `ExtractionTier` moved to `providers/types.py` (beside `AttachmentMode` and
`ExtractionConfidence`, and `canonical.py` imports it) so `ResolvedAttachment` could carry `tier`
without a second copy of the literal. The field is threaded to both paths: `ChatCompletionResponse`,
`DoneEvent`, `StreamResult`, `_Turn` and `MessageMeta`, plus `frontend/lib/{types,sse}.ts`. **One
done-when could not hold as written:** the plan asks for the `meta` frame *before* the lane runs, but
Phase 2 Step 9 emits `meta` on the first delta rather than at attempt start — deliberately, since that
is the D13 boundary — so emitting it earlier would commit a 200 before knowing anything would be sent
and turn every pre-first-byte routing failure into an in-band error. `meta` stays where it is; a
streamed turn whose file is unreadable therefore surfaces as an ordinary 422 envelope, which has its
own test. Verified end to end against live Groq, live Gemini, live Supabase (Postgres + Auth +
Storage) and live Upstash, not only the fixture suite: a real PyMuPDF-built PDF asked on `fast`
answered "4.2 million euros" through a Groq model that cannot open a PDF (`extraction_tier: "llm"`,
`lane:perception` +1, **`rpd` untouched**), the next two turns came back `cache` with no provider call
in the lane, a Gemini-only fleet answered a fresh document `native` — spending `rpd` and **not**
`lane:perception`, which is trap 7 proven on live counters — `PERCEPTION_LOCAL_ONLY` still answered
correctly at tier `local` with zero Gemini calls, and a PNG with OCR disabled was a 422
`file_unreadable`. Step 9 gives that native path a price.

**Step 9 (what a natively attached file costs) is in — Milestone B is done.** The last gap in
D27's arithmetic: a 40-page PDF riding in Gemini's payload was invisible to `estimate_tokens`
(base64, ignored on purpose — trap 9) and measured as the 30-character placeholder
`materialize` produces, so the fitting step thought it was free and the reservation under-counted
it by four orders of magnitude. `ResolvedAttachment` gained `token_cost` and `pages`, and the
number now reaches all three consumers the decision names. **The rate table is in
`perception/lane.py`** (`TOKENS_PER_TILE = 258`, Google's published per-tile rate with the date it
was read, a PDF page billed as an image, and Google's own tile geometry for a larger one) with a
documented fallback for each thing the measurement can fail to learn — never zero, because an
attachment that measures as free is exactly the failure D27 exists to end. **The measurement is in
`perception/local.py`** (`Measurement`/`measure` — page count for a PDF, pixel dimensions for an
image), because that is where PyMuPDF and Pillow already live and because measuring is not
reading; it runs in `asyncio.to_thread` like everything else in that module (trap 4) and returns
an empty `Measurement` rather than raising on a file it cannot open. **`fitting.py` charges the
cost per message rather than per turn** — `_cost` adds `native_tokens()` for the file_refs on
*that* message — so a document leaves the budget with the message that carried it, and an
`injected` attachment contributes nothing because its text is already in what the projector
produced (trap 8). `render.py` adds the same sum to `RenderReport.estimated_tokens` after
`adapter.estimate_tokens`. `extractors._estimated_tokens` deliberately keeps its own coarse
size heuristic: `lane.py` imports `extractors`, so reaching back for the table would close an
import loop for a number that a commit reconciles against reported usage seconds later.
The second golden payload, `tests/fixtures/golden_payloads/gemini_attachment.json`, is the fixed
canonical history plus one native attachment — a committed 200-byte 96x96 PNG
(`tests/fixtures/files/tile.png`, one tile at the published rate, so the test's arithmetic is
checkable by hand), with `canonical_history_with_attachment()` in `tests/provider_fixtures.py`
so Phase 5 can extend the case to the other two providers. Verified end to end against live
Gemini, live Supabase (Postgres + Auth + Storage) and live Upstash, not only the fixture suite: a
real 40-page PyMuPDF-built PDF uploaded, answered `native` on a Gemini-only `general` — "4.2
million euros", correct — with `rpd` +1 and **`lane:perception` untouched** (trap 7 still true),
and Gemini reported **21,297 prompt tokens** for the turn: five figures, against the eight the
placeholder used to measure it at, and the same order of magnitude as the 10,320 the rate table
now reserves.

**Step 10 (cache and the frontend) is in — Milestone C is under way.** The phase becomes demoable to
somebody who has never seen a terminal. **D29 on the backend:** `is_cacheable`'s blanket `file_ref`
exclusion — which Phase 3 wrote deliberately so this would be a Phase 4 decision — is gone, and
`request_hash` covers each `file_ref`'s **hash** instead, projected by a new `_identity_of` so that
`filename`, `mime` and `bytes` (a label the uploader chose, and two functions of the same bytes) do
not enter the key while the block itself still does — "summarize this" with a document and without one
must not collide. `CACHE_FORMAT_VERSION` went to 2, which is what that constant is for. The predicate
is still **one function** shared by the read and write sides, and `degraded` now carries the whole
weight of the attachment argument: the read side runs before anything is resolved and cannot know a
tier, but the one case where two identical requests over identical bytes deserve different answers is
a reading nobody trusts, and that is exactly what the write-side gate refuses (trap 14). One existing
test had to change and the change *is* the feature: `test_a_stored_model_reading_beats_a_native_passthrough`
asked the same question twice and now gets an `X-Cache: HIT` before the lane runs at all, so it asks a
different question and D29 gets three integration tests of its own. **On the frontend**, `lib/files.ts`
is new and holds both ends of one story — the gate a file passes before it is uploaded (the allowlist
mirroring `sniff_mime`'s range, `MAX_FILE_BYTES` mirroring the setting, both duplicated knowingly
because there is no codegen across that boundary and the gateway stays authoritative) and
`describeTier`, the disclosure that comes back. `api.uploadFile` is the only thing on that side that
puts bytes in; `request()`'s JSON `Content-Type` is now conditional on a *string* body, since a
`FormData` must set its own boundary. `useAttachments` (in `hooks.ts`) **uploads on selection, not on
send** — so the hash is in hand when the message is, and a 413 lands on the chip that caused it
seconds before the message exists — mirroring its list into a ref because `add` both counts remaining
slots and starts uploads, which a functional `setState` updater would run twice under StrictMode.
`Composer` owns that hook (a file is part of composing a message), gained an attach button, a chip row,
drag-and-drop with a depth counter, a submit blocked while an upload is in flight, and the privacy line
D29's docs require **before** the send rather than in a settings page. `send(text, attachments)` puts
`file_refs` on the *message*, `PendingTurn` carries them so a retry does not make the user re-attach,
and `applyOptimisticTurn` writes the same text-then-`file_ref` block order the server does.
`ModelIndicator`'s rule 4 gained its *why*: `Provenance.extractionTier` reaches it from all three
adapters, and `describeTier` maps the four tiers to four different guarantees — `native` names the
answering model (it is the reader), `llm` says "read by another model" because **nothing on the wire
carries which perception candidate won** and inventing one is the single thing this component must
never do, and `local`/`cache` are sharpened by `degraded`, since tier 3 is a PDF's own text layer *or*
OCR over a scan and calling the first one OCR would simply be wrong. `MessageList` pairs each user row
with the *immediately* following assistant row (invariant 3 permits consecutive user messages, and
reaching further would attribute an answer's reading to a file it never saw) so a degraded turn's chip
is badged next to the document it is about. `ErrorState` branches on `code` before `status` and renders
413/415/422 from `ATTACHMENT_ERROR_COPY`, shared with the failed chip so the two never describe one
refusal differently. Verified end to end against live Groq, live Gemini, live Supabase and live
Upstash, and then again through a real browser: a PDF uploaded from the composer and asked on `fast`
answered "Revenue was 4.2 million euros." disclosing **read by another model**; the identical request
came back `X-Cache: HIT` with the perception counter unmoved, `attempts: 0` and no tier claimed; the
same question over different bytes was a `MISS` and a different answer; an 11MB file and an `.mp4` were
both refused in the composer **with no request made**; and with `PERCEPTION_LOCAL_ONLY=true` the same
document answered correctly at tier `local`, disclosed as **read locally**, with zero Gemini calls.

**Step 11 (tests and fixtures) is in.** `scripts/record_fixtures.py` gained Gemini's fourth case:
`_extraction_case()` builds a real tier-2 request through the same `extractors._build_payload`/
`_extraction_message` pieces `extract_with_llm` itself calls, over the committed `text_layer.pdf`
fixture — not a hand-shaped body, so the recorded request is provably the one the app sends. Recording
it live replaced `extraction_complete.json`'s Step-6-era hand-authored placeholder, exactly as that
fixture's own note said this script would; the real capture graded `high` and transcribed "Revenue rose
12 percent" (not the placeholder's invented "12%"), which needed one literal-text assertion in
`test_extractors.py` updated to match a genuine live response instead of a guess at one. A full pass
over §6's test matrix against the suite Steps 1–10 had already built found it essentially complete —
including the stampede-guard concurrency test this step calls out by name ("two simultaneous first-turns
on the same document make exactly one extraction call"), which already existed from Step 6 as
`test_two_concurrent_extractions_of_one_hash_make_one_provider_call` — and turned up exactly two real
gaps. `render.worst_tier` had no test exercising more than one attachment, so `test_render.py` gained one
asserting that a turn with a native reading *and* a local one discloses `local`, the worse of the two,
not whichever resolved first. And no test exercised D1's actual mid-stream restart with an attachment
present — every existing failover-and-memo test used a pre-stream 429, never a stream that emitted real
deltas and then died — so `test_perception_lane.py` gained
`test_a_mid_stream_restart_re_renders_but_does_not_re_extract`, scripting Groq's `stream_truncated.sse`
into a genuine restart onto Gemini and asserting the tier-0 memo held across it (`extraction_tier:
"cache"` on the restart, one extraction call total). `make test` (1190 passed, 1 skipped — Tesseract
absent from this machine), `make lint`, `make typecheck`, `make frontend-test` (81 passed) and
`make frontend-lint` all green.

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
