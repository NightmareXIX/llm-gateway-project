# Phase 4 — The Perception Lane

Implementation plan. Derived from `development-plan.md` §3 Phase 4, amended by `project-overview.md` §4.5
and `contracts-and-phase1.md` §2.2 (Contract B invariant 6, render step 1) and §2.3 (Contract C), and
written against the code Phases 1–3 actually shipped rather than against the skeleton it was planned from.

Where the overview and the contracts doc disagree, the contracts doc wins — and it decides the shape of
this phase twice.

**The overview reads like extraction happens when a file is uploaded. The contracts doc says it is
resolved at render time.** Invariant 6 is explicit: a `file_ref` stores only the hash, "extraction is
resolved at render time from `file_extractions`, so improving your extractor retroactively improves old
conversations." That is not a storage detail — it decides *when* the perception lane runs, and therefore
whether native passthrough (tier 1) can exist at all, because tier 1's answer depends on which model is
answering and `POST /v1/files` does not know that. §3's D22 settles it.

**And the development plan's D8 default is not the locked one.** The plan recommends reserving 70% of
Gemini's budget for perception; §1 of the contracts doc froze it at **50/50**, `config/providers.yaml`
declares `reserved_fraction: 0.5`, and `quota/lanes.py` has been halving the answer lane since Phase 3
Step 4. Phase 4 spends the other half. It does not re-open the number.

---

## 1. Scope

**Goal:** upload a PDF or an image, and every model can "see" it — the one that reads files natively by
being handed the bytes, the one that cannot by being handed text that a model which *can* read files
extracted for it, and, when every model is spent, by being handed whatever local OCR could recover,
labelled honestly as degraded.

**In scope**

- `POST /v1/files` — multipart upload, size cap and MIME allowlist enforced on the sniffed type rather
  than the declared one, SHA-256 computed while streaming, bytes in object storage, a row in `files`.
- `app/perception/storage.py` — the object store behind that endpoint. Supabase Storage in deployment, a
  local directory in dev, an in-memory one in tests, behind one Protocol.
- `app/perception/lane.py` — the four-tier chain (cache → native → extraction → local) as the first real
  implementation of `render.AttachmentResolver`, memoized per request.
- `app/perception/extractors.py` — the Gemini extraction prompt and the call that runs it, routed through
  a new **internal** `perception` slot in `config/providers.yaml`, reserving from D8's fenced-off half.
- `app/perception/local.py` — PyMuPDF text layer, rasterize-and-OCR for a PDF with none, Tesseract for
  images. Off the event loop, in a thread.
- `quota/lanes.py::reserve_perception` — the typed seam Phase 3 left, filled in. Plus the commit/release
  twins it needs, which the seam did not declare.
- `db/repo/files.py`, `db/repo/extractions.py`, migration `0004` (`files`, `file_extractions`).
- Chat accepts `file_refs` per message; `render()` finally gets a resolver that resolves something;
  `MessageMeta.extraction_tier` and `RenderReport.degraded` get their first non-default values.
- Native multimodal input for Gemini: `inline_data` in `build_payload`, and the token cost that makes a
  natively attached file measurable against a context window and against quota.
- Frontend: attach a file in the composer, see it on the turn, see when an answer was built on a degraded
  reading of it.

**Explicitly NOT in Phase 4** — pulling any of these forward is how the phase stops being demoable:

- **No file management UI.** No file browser, no delete endpoint, no re-download. A file is referenced by
  the turn that uploaded it; `GET /v1/files/{hash}` returns metadata only.
- **No summarization (D4's other half).** The fitting step still truncates, and Phase 4's only new fitting
  concern is that a truncated document keeps its summary — solved by the extraction prompt's ordering
  (D28), not by a new strategy.
- **No BYOK.** `scope` stays `keys.SYSTEM_SCOPE` at every call site, including the perception lane's own
  reservations. Phase 6 replaces one constant.
- **No audio, no video, no office formats.** The allowlist is PDF, PNG, JPEG and WebP. A format nobody has
  a tier-3 fallback for is a format that fails at 3am rather than degrading.
- **No asynchronous extraction, no job queue, no webhook.** D22 puts extraction inside the request that
  needs it. A background worker is a second runtime on a free tier that has one.
- **No `pricing.yaml`, no simulated cost, no `/metrics`, no idempotency.** Still Phase 7.

**Definition of done:** upload one PDF. Ask about it on `fast` (Groq, text-only) — the answer is correct,
the log shows a Gemini extraction, and the turn's `extraction_tier` is `llm`. Ask the same question on
`general` with Gemini serving — the answer is correct, no extraction ran, and the tier is `native`. Ask it
again on `fast` — tier `cache`, zero provider calls inside the lane. Now set `PERCEPTION_LOCAL_ONLY=true`
(or exhaust Gemini) and ask a fourth time: the answer is worse, it still arrives, `degraded: true` is on
the response, and the UI says the document was read by OCR.

**One Alembic migration** (`0004`), two tables:

- `files` — `file_hash`, `user_id`, `filename`, `mime`, `bytes`, `storage_path`, `created_at`, unique on
  `(user_id, file_hash)`. Ownership lives here (D24).
- `file_extractions` — `file_hash` (primary key), `text`, `extracted_by_provider`, `extracted_by_model`,
  `extraction_confidence`, `tier`, `pages`, `created_at`. Content-addressed and global: the extraction of
  a given byte sequence is the same fact for everyone who holds those bytes.

---

## 2. What Phases 1–3 left cut, and what Phase 4 does to each seam

Almost nothing below is a new call site. Phase 1 built the pipeline this phase fills; if a step here needs
a signature change to something Phase 2 or 3 shipped, stop and check whether the seam was meant to absorb
it. The exceptions are named in §3 and there are three.

| Seam | Where | State today | Phase 4 |
|---|---|---|---|
| `app/perception/` | package | `__init__.py` only | `storage.py`, `lane.py`, `extractors.py`, `local.py` |
| `render()` step 1 | `memory/render.py` | `NoAttachments`, which *raises* if a `file_ref` reaches it | `PerceptionResolver`; `NoAttachments` stays as the default and as the test double |
| `AttachmentResolver` Protocol | `memory/render.py` | declared, one implementation, no real one | Implemented for real; the Protocol does not change |
| `route(resolver=…)` / `route_stream(resolver=…)` | `routing/router.py:324, 614` | parameter exists, is always `None` | First non-`None` caller — **no signature change** |
| `ResolvedAttachment` | `providers/types.py:144` | built by nothing | Built per request; gains `token_cost` (D27) and `pages` |
| `ResolvedAttachment.mode="native"` | all three adapters | every `_render_attachment` raises `NotImplementedError` | Gemini's branch becomes `inline_data`; Groq's and OpenRouter's refusals stay, and stay correct |
| `RenderReport.degraded` | `memory/render.py` | computed from `confidence == "low"`, always `False` | First `True`, and gains `extraction_tier` |
| `MessageMeta.extraction_tier` | `memory/canonical.py:177` | declared, never set | Set on every assistant message answering a turn with attachments |
| `DoneEvent` | `streaming/sse.py:171` | carries `degraded`, not `extraction_tier` | Gains `extraction_tier` — one added optional field |
| `keys.extraction` / `keys.extraction_lock` | `cache/keys.py` | written, no writer | First writer; `EXTRACTION_TTL_S` and `EXTRACTION_LOCK_TTL_S` finally mean something |
| `keys.quota_perception_lane` | `cache/keys.py` | written, no writer | The lane's daily counter (D26) |
| `lanes.reserve_perception` | `quota/lanes.py` | typed signature raising `NotImplementedError` | Implemented, plus `commit_perception` / `release_perception` |
| `lanes.perception_budget` | `quota/lanes.py` | pure, correct, called by nobody | The lane's limit source |
| `ModelSpec.supports_mime` | `providers/types.py` | correct, called by nobody | Tier 1's whole question |
| `ModelSpec.max_file_bytes` | `providers/types.py` | `20000000` on Gemini, `null` elsewhere | Tier 1's second question |
| `config/providers.yaml` tail comment | `config/providers.yaml` | "Perception lane (D8) and long-context slots land here in Phase 4/5" | The `perception` slot lands, marked internal |
| `tests/fixtures/files/.gitkeep` | `tests/fixtures/files/` | empty | Three committed fixtures: text-layer PDF, scanned PDF, PNG |
| `is_cacheable` | `cache/exact.py:87` | any `file_ref` anywhere → not cacheable | D29 decides what replaces that blanket exclusion |
| `docs/architecture.md` "Where this leaves Phase 4" | `docs/architecture.md:167` | three lines promising the seam holds | Replaced by the lane's own diagram |

Two structural additions, both deliberate and both named here rather than discovered in a diff:

- **`app/db/repo/files.py`.** §3 of the contracts doc lists `repo/extractions.py` and not `repo/files.py`,
  because the overview's data model has `file_extractions` and no `files` table. Ownership has to live
  somewhere (D24), and one repo module per table group is the rule the directory already follows.
- **`python-multipart`.** FastAPI cannot parse a multipart body without it, and the failure is an
  assertion at import of the route rather than at request time — see trap 1.

---

## 3. Decisions to settle before writing code

Nine questions the frozen contracts do not answer, continuing Phase 3's numbering (D15–D21 are spent) and
`docs/decisions/`'s (ADR-024 is the last one written). In each case the reasoning, not the verdict, is the
deliverable.

### D22 — When the perception lane runs: at upload, or at render?

The overview's §4.5 reads as an upload-time pipeline; the development plan's tier list reads the same way.
Contract B invariant 6 does not: extraction is "resolved at render time from `file_extractions`."

**Decided: the upload endpoint stores bytes and metadata and nothing else. The lane runs inside render
step 1, per request, per candidate.** Three reasons, in order of force:

1. **Tier 1 cannot exist at upload time.** Native passthrough asks "does the model that is about to answer
   read this MIME natively, and is the file under its size cap?" — and at upload there is no such model.
   Extracting at upload means extracting a document Gemini was about to read natively, which spends the
   scarcest budget in the fleet to produce something nobody needed.
2. **Invariant 6's payoff is retroactive improvement.** A better extractor, a bigger model, a fixed
   prompt — all of it improves every stored conversation the next time one is rendered, because nothing
   about the extraction was frozen into the message.
3. **The failure surface is smaller.** An upload that only writes bytes either succeeds or does not. An
   upload that also calls a provider has a partial-success state — bytes stored, extraction failed — and
   somebody has to decide what the endpoint returns and who retries.

**What this costs, stated plainly because it is the honest cost:** the first turn that asks about a
document pays the extraction inside the chat request. On a large PDF that is a real Gemini call — seconds,
not milliseconds — in front of the answering call. The `cache` tier makes it a once-per-document cost
rather than a per-turn one, and the streaming path emits its `meta` frame before the lane runs so the UI
can say *why* it is waiting. Both go in `docs/limitations.md`.

**The consequence that has teeth:** render runs once per candidate, and up to three times per turn under
failover. The resolver **must** memoize per request, or a failover from Groq to Gemini re-extracts a
document that was extracted forty milliseconds ago. The memo is per `PerceptionResolver` instance, the
instance is per request (`deps.get_resolver`), and the memo key is `(file_hash, mode)` — not `file_hash`
alone, because the same file resolves `injected` for Groq and `native` for Gemini within one turn.

### D23 — Where the bytes live

Render's free plan gives an ephemeral filesystem and a service that sleeps; anything written to disk is
gone on the next deploy. Supabase is already a dependency (Postgres and Auth), its free tier includes
Storage, and the gateway already holds a project URL.

**Decided: a Protocol with three implementations, chosen by one setting.**

```python
class ObjectStore(Protocol):
    async def put(self, path: str, data: bytes, *, mime: str) -> None: ...
    async def get(self, path: str) -> bytes: ...
    async def exists(self, path: str) -> bool: ...
```

`SupabaseStore` (deployment) speaks the Storage REST API over the app's existing shared
`httpx.AsyncClient` — no new SDK, no second connection pool, and the same client every provider adapter
already uses. `LocalStore` (dev) writes under a configured directory. `MemoryStore` (tests) holds a dict.
`FILES_STORAGE_BACKEND: Literal["supabase", "local", "memory"]` picks one at startup, and the choice is
printed in the `startup.complete` line beside the quota flags.

**The bucket is private and stays private.** No public URL is ever generated, no signed URL is ever handed
to a client, and there is no download endpoint in this phase. The only reader of the bytes is the gateway
itself, resolving an attachment for a model. That removes an entire category of "a file hash is a
capability" bug before it can be written.

**Path is content-addressed:** `{hash[:2]}/{hash}`. The two-character shard is for the eventual object
listing rather than for lookup, and dedup falls out — two users uploading the same PDF write the same
object once and own two `files` rows pointing at it.

**`SUPABASE_SERVICE_ROLE_KEY` is a new secret** with more authority than anything the app currently holds:
it bypasses row-level security on the whole project. It is used for exactly one thing (object read/write
on one bucket), it is never logged, and `docs/deploy.md` gains a line saying what it is and why it is not
the anon key.

### D24 — Ownership, dedup, and who may reference a hash

A `file_hash` is 64 hex characters, so it is not guessable — but "not guessable" is not an authorization
model, and the gateway's hard rule is that every conversation read is ownership-scoped in the SQL query
itself.

**Decided: the bytes and the extraction are global and content-addressed; the *right to reference* them is
per user.** `files` is unique on `(user_id, file_hash)` — two users uploading identical bytes get two rows
and one object. `file_extractions` is keyed on `file_hash` alone, because the extracted text of a byte
sequence is a property of those bytes, and re-extracting it per user would spend the scarcest budget in
the fleet to compute the same string twice.

**A `file_ref` is validated at the point it enters a message, in one ownership-scoped query.**
`POST /v1/chat/completions` resolves every hash in `file_refs` with
`WHERE file_hash = ANY(:hashes) AND user_id = :uid` before it writes a single message row; a hash the user
does not own is missing from the result, and a missing hash is a **404**, never a 403 — the same rule, and
the same reasoning, as `conversations`. Nothing downstream re-checks: the lane resolves a `file_ref` that
is already in stored history, and it got there by passing this gate.

### D25 — The tier chain, and what a failure at each tier costs

Four tiers, in the order the overview names them, and the rule the development plan states as a trap:
**every tier failure logs and falls through; only tier 3's failure surfaces.**

| Tier | Name | Condition | Cost |
|---|---|---|---|
| 0 | `cache` | `extract:{hash}` in Redis, else `file_extractions` in Postgres | none |
| 1 | `native` | `spec.supports_mime(mime)` **and** `size <= spec.max_file_bytes` | the answering model's own quota, via the reservation the router already makes |
| 2 | `llm` | a `perception`-slot candidate has budget and a closed breaker | the perception lane's fenced half (D26) |
| 3 | `local` | always available; may still produce nothing | CPU, in a thread |

Four things this table decides that are easy to get wrong:

**Tier 1 makes no perception reservation.** The bytes ride in the answering model's payload, so they are
counted by the reservation `router.py` already makes for that attempt — through `token_cost` (D27), which
is exactly why an attachment needs one. Reserving from the perception lane as well would double-count the
same request against the same real budget.

**Tier 0 is checked before tier 1, and that is deliberate.** A cached extraction costs nothing; a native
passthrough costs tokens in a 1M-token window. When both are available, the free one wins — and the answer
is not meaningfully worse, because the cached text came out of the same model that would have read the
bytes. The one case where this is the wrong call is an image whose question is about layout or colour,
which the extraction summarized away; `docs/limitations.md` records it.

**Tier 2 is a chain, not a call.** The `perception` slot has candidates in order, the breaker applies, and
a `RateLimited` or `Unavailable` from the first moves to the second. A `ContentFiltered` does not — the
same reasoning as the answer lane's: failing over would just launder a refusal.

**Tier 3's failure is an error the user sees.** An empty text layer plus OCR that recovered nothing means
the gateway cannot answer the question that was asked. Answering anyway, from a document nobody read, is
the worst behaviour in the whole design. `core/errors.py` gains `FileUnreadable` (422,
`code="file_unreadable"`), naming the file, and the turn is not written.

### D26 — Perception quota under a frozen Contract C

Contract C gives the perception lane exactly **one** key: `q:{scope}:{provider}:{model}:lane:perception`,
TTL "until reset". The answer lane has four (`rpm`, `rpd`, `tpm`, `tpd`). So the split cannot be
symmetric, and the question is which windows the fence actually applies to.

**Decided: the lane fence is a *daily* fence. Per-minute windows are shared, and the perception lane
checks them against the full published limit.**

- **Daily** (`rpd`, and `tpd` where declared): the perception lane increments `lane:perception` and
  **never** `rpd`. Its limit is `floor(published * reserved_fraction * (1 - headroom))` —
  `lanes.perception_budget`, which has been computing exactly this since Phase 3 and has had no caller.
  The answer lane's `rpd` is already `floor(published * answer_share * (1 - headroom))`. The two halves
  sum to the published limit, neither can spend the other's, and that is D8 implemented rather than
  described.
- **Per minute** (`rpm`, `tpm`): one shared counter, two ceilings. Chat checks it against the halved limit
  it already uses; the lane checks the *same key* against the full published limit. So the lane may use
  whatever minute the chat side has not, chat can never push past half, and the total across both can
  never exceed what the provider actually publishes.

The reasoning for the asymmetry: **starvation is a daily problem.** "Gemini's budget is gone and it is
2pm" is the failure D8 exists to prevent, and it is a failure of the daily counter. A per-minute collision
between a chat turn and an extraction resolves itself in under sixty seconds, and fencing the minute would
mean a lane that refuses to read a document while five requests per minute of Gemini sit unused. The
alternative — four more perception keys — is an amendment to a frozen contract for a problem that does not
exist, and Phase 3's one amendment (the `{window}` segment on `rl:`) was made because the key was
*incorrect*, not because more keys would be tidier.

**The lane gets the same reserve → commit/release lifecycle as the answer lane**, for the same reason
(D17): post-hoc counting undercounts under concurrency. `lanes.py` gains `commit_perception` and
`release_perception` beside the seam's `reserve_perception`, all three delegating to the existing Lua
scripts with the lane's key substituted for the daily counter. **No new Lua.** If the scripts need a
change to accept the lane's key set, that is a signal the key set is wrong, not that the scripts are.

**Which model the lane uses is config, not code.** `config/providers.yaml` gains a `perception` slot whose
candidates are the two Gemini models already declared, in capability order. `Slot` gains
`internal: bool = False`; `registry.slots()` and `GET /v1/models` skip internal slots, and `_validate_slot`
rejects a client asking for one by name with the same 400 it gives a typo. A startup check asserts that a
model appearing in both an answer slot and the `perception` slot declares the *same* `reserved_fraction`
in both places — otherwise the two halves of D8's split stop summing to one and nothing fails loudly.

### D27 — What a natively attached file costs, in tokens

`memory/render.py` names this gap in a comment: "Native bytes are not prompt text, and base64 length is
not a token count. Phase 4 gives `ResolvedAttachment` a token cost to use here." Until it does, a natively
attached 40-page PDF measures as the 30-character string `[application/pdf attachment: q3.pdf]` — so the
fitting step believes it is free and the quota reservation under-counts it by four orders of magnitude.

**Decided: `ResolvedAttachment` gains `token_cost: int`, computed by the lane at resolve time from a
declared per-modality rate, and consumed in three places.**

- `fitting.py` adds the `token_cost` of every native attachment still referenced by a surviving message to
  the measured total. (An `injected` attachment's cost is already in its text and must not be
  double-counted — trap 8.)
- `render.py::materialize` keeps returning its short placeholder string; it is a projection for
  measurement of *text*, and the token cost travels beside it rather than inside it.
- `RenderReport.estimated_tokens` — and therefore the reservation — is `adapter.estimate_tokens(payload)`
  **plus** the native attachments' cost, because an estimator that counts characters in a base64 blob is
  worse than one that ignores it (trap 9).

The rates are Gemini's published ones, in a small table in `perception/lane.py` with the date they were
read and a comment pointing at `config/limits.yaml`'s precedent: a free tier's arithmetic changes, and a
number in one place is a number that can be corrected. An image is a flat cost per tile; a PDF page is
charged as an image. `pages` therefore has to be known before the payload is built, which is why
`ResolvedAttachment` carries it and why the local tier counts pages even when it is not the tier that
answers.

### D28 — What the extraction prompt returns, and why the order matters

The development plan asks for "verbatim text, then table/chart/figure descriptions, then a summary." The
fitting step, when a document does not fit, truncates it — **from the tail**, keeping the head.

Those two facts collide: a summary at the end is the first thing lost, on exactly the documents where a
summary is the only thing that will fit.

**Decided: the extraction returns four labelled sections, summary first.**

```
## Summary
## Structure          (headings, page count, what kind of document this is)
## Figures and tables
## Verbatim text
```

Head-first truncation then degrades gracefully by construction — a 200-page report cut to 4,000 tokens
keeps its summary, its shape and its figures, and loses the body it could never have fitted. **No change
to `fitting.py`'s truncation rule** is needed, which is the point: the ordering decision belongs to the
prompt, and solving it in the fitting step would mean teaching a size algorithm to parse document
structure.

`document_envelope` (`memory/render.py`) is unchanged and stays the single wrapper every provider uses.
The sections live *inside* it.

### D29 — Cache identity when a turn carries a file (extending D19)

`is_cacheable` currently refuses any history containing a `file_ref`, with the comment "Phase 4's
extraction confidence can change underneath a hash that does not cover it." Phase 3 wrote that
deliberately so this would be a Phase 4 decision.

**Decided: `request_hash` covers each `file_ref`'s hash, in message order, and the blanket exclusion is
removed. The `degraded` gate on the write side does the rest.**

The read side runs before anything is resolved, so it cannot know a tier — but it does not need to. A
file's *hash* is exactly the identity of its content, which is what the answer depended on; and the one
case where two identical requests over identical bytes deserve different answers is when the first was
built on a bad reading, which is `degraded=True`, which the write side already refuses to cache.

**The residual, which goes in `docs/limitations.md` rather than being quietly absorbed:** for up to
`EXACT_CACHE_TTL_S` (one hour), an answer cached from an `llm` extraction is served even if a `native`
passthrough would now be available. The window is an hour, the answer was not degraded, and the
alternative is a cache that never hits on the feature this phase exists to build.

### D30 — The local tier's dependencies, and what ships in the image

Tier 3 needs two things that are not pip-installable-and-done.

**PDF text layer and rasterization: PyMuPDF.** The development plan names it, it does both jobs in one
dependency, and it is fast. It is **AGPL-3.0**, which is worth stating rather than discovering: this
project is not distributed as a product and its source is public, so the licence costs nothing here — but
a fork that closes its source has a real problem, and `docs/limitations.md` says so in one line.
`pypdfium2` (BSD) is the swap if that ever matters; the tier is one module and the swap is one function.

**Image OCR: Tesseract**, which is a system binary, not a wheel. `pytesseract` shells out to it. That
means `apt-get install tesseract-ocr` in the Dockerfile — roughly 100MB of layer including the English
language data — and it means the binary is *absent* on a developer's Windows machine unless they install
it separately.

**Decided: OCR is optional at runtime and detected at startup, not assumed.**
`PERCEPTION_LOCAL_OCR_ENABLED: bool = True` gates it; startup probes for the binary once and logs
`perception.ocr_unavailable` at warning level if it is missing. With OCR unavailable, tier 3 still handles
a PDF with a text layer, and an image falls through to `FileUnreadable` rather than to a stack trace —
which is the same "degrade, then be honest" rule the rest of the chain follows.

**And one more flag, for the demo:** `PERCEPTION_LOCAL_ONLY: bool = False`. On, tier 2 is skipped entirely
and every extraction goes local. It exists because the phase's most persuasive demo — "disable Gemini
entirely, the answer still arrives, degraded and labelled" — should not require revoking a key.

---

## 4. Implementation steps

Ordered so each step is independently committable and three internal milestones are demoable rather than
one at the end. Every step names the files it touches and what has to be true before it is finished; a
step is done when `make lint`, `make typecheck` and `make test` are green and its own "done when" list
holds.

- **Milestone A (Steps 1–4):** the bytes land. A file can be uploaded, owned, and referenced by a turn —
  and a turn that references one fails loudly, because nothing resolves it yet.
- **Milestone B (Steps 5–9):** the models can see. All four tiers, wired into render, on both paths.
- **Milestone C (Steps 10–12):** honest and shippable. Cache, UI, tests, docs, deploy.

### Step 1 — Config surface, settings, migration *(1 day)*

> **In plain terms.** Paperwork before mechanism, exactly as Phase 3 Step 1. Two tables to hold what an
> upload produces, a handful of switches, the model the extraction lane will use, and the dependencies
> that make a PDF readable at all.
>
> **After this step.** Nothing behaves differently. But `perception` is a declared slot, the tables exist,
> and every later step can be switched off in one deploy.

**Files:** `pyproject.toml`, `app/config.py`, `.env.example`, `config/providers.yaml`,
`alembic/versions/0004_*.py`, `app/db/models.py`, `app/providers/registry.py`, `Dockerfile`.

- `pyproject.toml`, runtime: `python-multipart>=0.0.18` (trap 1), `pymupdf>=1.24`, `pytesseract>=0.3.13`,
  `pillow>=11` (pytesseract's image handle). Dev: nothing new — `httpx.MockTransport` already covers both
  the Storage API and the Gemini extraction call.
- `Settings`: `FILES_STORAGE_BACKEND: Literal["supabase","local","memory"] = "supabase"`,
  `FILES_LOCAL_DIR: str = ".files"`, `FILES_BUCKET: str = "uploads"`,
  `SUPABASE_SERVICE_ROLE_KEY: SecretStr | None = None` (required when the backend is `supabase` —
  validate that pairing in a model validator, so the failure is at boot and names the missing var),
  `FILE_MAX_BYTES: int = 10_000_000`, `PERCEPTION_ENABLED: bool = True`,
  `PERCEPTION_LOCAL_ONLY: bool = False`, `PERCEPTION_LOCAL_OCR_ENABLED: bool = True`,
  `PERCEPTION_OCR_MAX_PAGES: int = 10`. Each with a docstring saying what turning it off costs; all logged
  in `startup.complete`.
- `config/providers.yaml`: `Slot.internal: bool = False` in `config.py`, and a `perception` slot —
  `gemini-3.6-flash` then `gemini-3.5-flash-lite`, `internal: true`, `reserved_fraction: 0.5` on both,
  matching what `general` and `fast` already declare for the same models. Replace the tail comment that
  promised this. No `config/limits.yaml` change: both models are already in the table.
- Startup validation in `registry.build_registry`: a model appearing in both an answer slot and the
  `perception` slot must declare the same `reserved_fraction` in both (D26). Boot failure, not a warning.
- Migration `0004`: `files` and `file_extractions` per §1 — `files.user_id` FK to `users` on delete
  cascade, unique `(user_id, file_hash)`, index on `file_hash`; `file_extractions.file_hash` primary key,
  `extraction_confidence` CHECK in `('high','medium','low')`, `tier` CHECK in `('llm','local')`, because a
  stored extraction is only ever one of those two — `cache` and `native` never produce a row.
- `Dockerfile`: `tesseract-ocr` and `tesseract-ocr-eng` in the runtime stage (D30), with the layer cost in
  the comment beside it. The image roughly doubles, and Render's free build has to survive it.

**Done when:** `make migrate` applies cleanly and downgrades; the app boots and prints the new settings;
`GET /v1/models` does **not** list `perception`; `"model": "perception"` in a chat request is a 400.

### Step 2 — `perception/storage.py` *(1 day)*

> **In plain terms.** Somewhere to put the bytes that is not the container's disk, because the container's
> disk is gone on the next deploy.
>
> **After this step.** You can round-trip bytes through Supabase Storage from a test, without a network.

**Files:** `app/perception/storage.py`, `app/deps.py`, `app/main.py`, `tests/unit/test_storage.py`.

- The `ObjectStore` Protocol from D23, plus `SupabaseStore`, `LocalStore`, `MemoryStore`.
- `SupabaseStore` uses `app.state.http_client` — the same client the adapters share, so it inherits the
  timeouts and the connection pool. `POST /storage/v1/object/{bucket}/{path}` with
  `Authorization: Bearer {service_role}` and `x-upsert: true`; a 409 on an object that already exists is a
  **success**, not an error, because the path is the content hash and the bytes are therefore identical.
- Errors normalize to a small local exception (`StorageUnavailable`), never a raw `httpx` error, and never
  one carrying the key or the bytes.
- Constructed once in `lifespan`, exposed as `StoreDep`.

**Done when:** a `MockTransport` test round-trips through `SupabaseStore`; a 409 reads as success;
`LocalStore` refuses a path that escapes its root; nothing anywhere logs a byte of content.

### Step 3 — `POST /v1/files` *(1.5 days)*

> **In plain terms.** The upload endpoint. It reads the file in chunks so a 10MB cap cannot be beaten by a
> lying `Content-Length`, checks what the file *actually* is rather than what the browser claimed, hashes
> it, stores it, and writes down that this user owns it.
>
> **After this step.** You can upload a PDF and get a hash back. Uploading it twice is one object and one
> fast response.

**Files:** `app/schemas/files.py`, `app/api/v1/files.py`, `app/db/repo/files.py`, `app/core/errors.py`,
`app/main.py`, `tests/integration/test_files_endpoint.py`.

- Read in 64KB chunks, hashing and counting as you go; abort past `FILE_MAX_BYTES` with a 413
  (`core/errors.py` gains `PayloadTooLarge`) **without** buffering the rest.
- Sniff the type from the leading bytes — `%PDF-`, `\x89PNG`, `\xff\xd8\xff`, `RIFF….WEBP` — and compare
  it to the declared `content_type`. The sniffed type wins and is what gets stored; a mismatch is logged
  at info, and a sniffed type outside the allowlist is a 415 (`UnsupportedMediaType`).
- Dedup: if `(user_id, hash)` already exists, return the existing row with `deduplicated: true` and skip
  the store entirely. If the *object* exists but this user does not own it, write the row and skip the
  upload — same bytes, new owner.
- The endpoint takes the same `RateLimitDep` as chat. An upload is a request; the D20 limiter is keyed on
  the user and does not care what kind.
- `GET /v1/files/{file_hash}` returns metadata, ownership-scoped in the query, 404 on a miss.

**Done when:** an 11MB upload is a 413 and nothing was stored; a `.pdf` that is actually a PNG is stored as
`image/png`; a `.exe` renamed to `.pdf` is a 415; the same file twice is one object, two responses, one
`files` row; another user's hash is a 404 on `GET`.

### Step 4 — `file_refs` on a chat turn *(1 day)*

> **In plain terms.** Teach the chat endpoint that a message can carry files. Nothing reads them yet, so
> this step's success condition is a *loud* failure: a turn with an attachment reaches the render pipeline
> and `NoAttachments` raises, exactly as it was written to.
>
> **After this step.** The whole path from upload to stored `file_ref` block works, and the missing piece
> is the one Milestone B builds.

**Files:** `app/schemas/chat.py`, `app/api/v1/chat.py`, `app/db/repo/files.py`, `frontend/lib/types.ts`,
`tests/integration/test_chat_endpoint.py`.

- `InputMessage.file_refs: list[str] = []`, max 4, each a 64-char lowercase hex string validated by
  pattern — a malformed hash is a 422 from pydantic, not a database round trip.
- Per-message rather than per-request: a `file_ref` is content, content belongs to a message, and a
  conversation where turn three's attachment is indistinguishable from turn one's is a conversation the
  renderer cannot fit correctly.
- One ownership-scoped lookup for every hash in the request, before any message is written (D24). Missing
  → 404 naming the hash.
- Each resolved hash becomes a `canonical.file_ref_block` appended **after** the message's text block, so
  a stored message reads "what I asked" then "what I attached".

**Done when:** a turn with a valid `file_ref` stores a two-block message and then fails with
`NotImplementedError` from `NoAttachments` (asserted, and the last time that assertion is ever green); an
unowned hash is a 404 and no message row was written.

### Step 5 — `quota/lanes.py`: the lane's reservation lifecycle *(1 day)*

> **In plain terms.** Fill in the seam Phase 3 left. The perception lane gets the same
> reserve-before/commit-after discipline the answer lane has, spending the half of Gemini's daily budget
> that has been fenced off and unspendable since Phase 3.
>
> **After this step.** The budget exists and is spendable, and nothing spends it yet.

**Files:** `app/quota/lanes.py`, `app/quota/tracker.py`, `tests/unit/test_quota_lanes.py`.

- `reserve_perception` — the declared signature, unchanged — checks and increments `lane:perception`
  against `perception_budget(spec, limits)`'s daily number with headroom applied, **and** the shared
  `rpm`/`tpm` counters against their *full* published limits (D26). One call to the existing `reserve`
  script with a different key/limit set; no new Lua.
- `commit_perception(reservation, *, tokens_in, tokens_out)` and `release_perception(reservation)`,
  delegating the same way.
- The one honest awkwardness: `QuotaTracker._effective_limit` bakes `lanes.answer_share` into every limit
  it computes, which is right for the answer lane and wrong for this one. Rather than threading a `lane`
  parameter through a method five call sites deep, the lane functions compute their own limits from
  `perception_budget` and hand them to the script directly; `tracker.py` gains one small internal entry
  point for "run reserve with these exact keys and limits". Nothing about the answer lane's path changes.

**Done when:** the seam no longer raises; 50 concurrent perception reserves against a daily fence of 10
grant exactly 10; a chat reservation and a perception reservation against the same model touch the same
`rpm` key and different daily keys; the two daily limits sum to the published limit minus headroom.

### Step 6 — `perception/extractors.py`: tier 2 *(2 days)*

> **In plain terms.** The part where a model that can read a PDF is asked to describe one for a model that
> cannot. It is a normal provider call — same adapters, same breaker, same error normalization — pointed
> at an internal slot and paid for out of a different pocket.
>
> **After this step.** Given bytes and a MIME type, you get back structured text, a confidence, and a row
> in `file_extractions`.

**Files:** `app/perception/extractors.py`, `app/db/repo/extractions.py`, `tests/unit/test_extractors.py`,
`tests/fixtures/provider_responses/gemini/extraction_*.json`.

- The prompt from D28, as a module constant with the section headers spelled out and a short instruction
  block. It is a *prompt*, so it is committed, reviewed, and covered by a golden test on the built
  payload — the same discipline `build_payload` gets.
- The extraction is a `build_payload` call with a single user message carrying a native attachment, then
  `adapter.complete`. It walks `registry.candidates("perception")` in order, checks the breaker, reserves
  via Step 5, commits actual usage, releases on a pre-call abandonment — the answer lane's rules, minus
  streaming, which an extraction has no use for.
- Stampede guard: `SET lock:extract:{hash} NX EX 60` before the call. Losing the lock means waiting briefly
  and re-reading tier 0 rather than making a second identical call; if the wait expires without a result,
  proceed — a duplicated extraction is better than a request that hangs on a dead lock holder.
- Write-through: the `file_extractions` row first (Postgres is the source of truth), then `extract:{hash}`
  in Redis with `EXTRACTION_TTL_S`. Redis failing here is a log line, not an error.
- `confidence`: `high` when the model returned all four sections and a non-trivial verbatim block,
  `medium` when it returned less. Never `low` — `low` is tier 3's marker and the thing that sets
  `degraded`.

**Done when:** a recorded Gemini response yields a stored extraction with `tier="llm"`; a 429 from the
first candidate moves to the second and the breaker opens; a `ContentFiltered` stops the chain; two
concurrent extractions of the same hash make one provider call.

### Step 7 — `perception/local.py`: tier 3 *(1.5 days)*

> **In plain terms.** The last resort, and the one that keeps the feature alive when every free tier is
> spent. It reads a PDF's embedded text if there is any, renders the pages and OCRs them if there is not,
> and OCRs an image directly. It is worse than tier 2, and it says so.
>
> **After this step.** With Gemini switched off entirely, a PDF still produces a usable answer.

**Files:** `app/perception/local.py`, `tests/unit/test_local_extraction.py`,
`tests/fixtures/files/{text_layer.pdf,scanned.pdf,chart.png}`.

- **Everything here runs in `asyncio.to_thread`.** PyMuPDF and Tesseract are synchronous, CPU-bound and
  slow; called directly they block the event loop for every other request on the instance. This is the
  convention's "never block the event loop" with a real cost attached (trap 4).
- PDF: open, read the text layer, count pages. Text above a per-page threshold → `confidence="medium"`,
  formatted into D28's sections as far as it can be — a summary is not available, so that section says so
  rather than being omitted. The envelope's shape should not depend on the tier.
- PDF with no usable text layer: rasterize at 150 DPI and OCR, capped at `PERCEPTION_OCR_MAX_PAGES`
  (default 10 — an unbounded OCR of a 400-page scan is a request that never returns). Pages beyond the cap
  are noted in the text. `confidence="low"`.
- Image: OCR directly, `confidence="low"`. If OCR is unavailable (D30) or recovers nothing meaningful,
  return `None` — which is tier 3 failing, which is `FileUnreadable`.
- The three fixture files are committed, small, and generated by a documented one-liner so they can be
  regenerated rather than trusted.

**Done when:** the text-layer PDF extracts without OCR; the scanned PDF triggers OCR and comes back `low`;
the PNG comes back `low`; a 40-page scan OCRs 10 pages and says so; the whole suite passes with Tesseract
*absent* from the machine (the image path skips, the PDF text path does not).

### Step 8 — `perception/lane.py` and wiring the resolver in *(2 days — the step everything converges on)*

> **In plain terms.** The four tiers become one object that answers the one question render step 1 asks:
> "here are the files and here is the model that will answer — how does each file reach it?" Then it gets
> handed to the router, on both paths, and the pipeline that has been carrying a `resolver` parameter
> since Phase 2 finally has something to put in it.
>
> **After this step.** The feature works end to end.

**Files:** `app/perception/lane.py`, `app/deps.py`, `app/api/v1/chat.py`, `app/memory/render.py`,
`app/streaming/{sse.py,collector.py,orchestrator.py}`, `app/schemas/chat.py`,
`tests/integration/test_perception_lane.py`.

- `PerceptionResolver(store, session_factory, registry, breaker, quota, redis, settings)` implementing
  `AttachmentResolver.resolve(refs, spec)`. Per-request instance via `deps.get_resolver`, memoized on
  `(file_hash, mode)` (D22).
- Tier selection per D25, per file — a turn with a PDF and a PNG against a text-only model may resolve one
  from cache and extract the other.
- Bytes are fetched from the store **only** when a tier needs them: tier 0 needs none, tier 1 needs them
  for the payload, tiers 2 and 3 need them for the reading. They are never persisted, never logged, and
  never attached to a `RenderReport`.
- `RenderReport` gains `extraction_tier: ExtractionTier | None` — the *worst* tier used across the turn's
  attachments, since that is what the disclosure is about. `chat.py` copies it onto
  `MessageMeta.extraction_tier`; `DoneEvent` gains the same optional field; `ChatCompletionResponse` gains
  it beside `degraded`.
- `chat.py` passes `resolver=` to both `routing.route` and `routing.route_stream`. **No router change.**
- Failure containment: any exception from the lane that is not `FileUnreadable` is caught, logged with the
  file hash and the tier that raised, and falls through to the next tier. Only the bottom of the chain
  surfaces (D25).

**Done when:** the §1 definition-of-done sequence passes end to end against mock transports, non-streaming
*and* streaming; a failover from Groq to Gemini within one turn re-resolves the attachment as `native`
without re-extracting anything; the `meta` frame is emitted before the lane runs.

### Step 9 — Native passthrough in the payload, and what it costs *(1.5 days)*

> **In plain terms.** Gemini can actually read the PDF, so stop describing it and hand it over. And make
> the size of that handover something the context-window arithmetic and the quota reservation can both
> see, because right now a 40-page PDF measures as a 30-character placeholder.
>
> **After this step.** `general` answers a document question without an extraction, and the tokens it cost
> are counted.

**Files:** `app/providers/gemini.py`, `app/providers/types.py`, `app/memory/{render,fitting}.py`,
`tests/fixtures/golden_payloads/gemini_attachment.json`, `tests/unit/test_gemini_payload.py`.

- `ResolvedAttachment` gains `token_cost: int = 0` and `pages: int | None = None`.
- `gemini._render_attachment`'s `NotImplementedError` becomes an `inline_data` part
  (`{"inline_data": {"mime_type": …, "data": base64}}`) sitting *beside* the message's text part — which is
  what `_render_text`'s docstring has said the `parts` list was for since Phase 2.
- Groq's and OpenRouter's refusals **stay**, and their messages stay accurate: they cannot read a PDF, and
  a native attachment reaching them means the lane routed one past the perception chain.
- `fitting.py` adds native `token_cost` to its running total (trap 8: injected attachments must not be
  double-counted). `render.py` adds it to `RenderReport.estimated_tokens` after
  `adapter.estimate_tokens`, and never lets the estimator count base64 (trap 9).
- One new golden payload: the fixed canonical history plus one native attachment, for Gemini. The
  committed base64 is a 200-byte fixture image, not a real PDF.

**Done when:** the golden payload is committed and reviewed; a native 40-page PDF reserves a five-figure
token count rather than a two-figure one; a file over `spec.max_file_bytes` never reaches tier 1.

### Step 10 — Cache and the frontend *(2 days)*

> **In plain terms.** Let a document question be answered from cache, and let a person actually attach a
> file in the UI and see what happened to it.
>
> **After this step.** The phase is demoable to somebody who has never seen a terminal.

**Files:** `app/cache/exact.py`, `frontend/components/{Composer,MessageTurn,ModelIndicator}.tsx`,
`frontend/lib/{api,types,hooks}.ts`, `frontend/tests/*`.

- D29: `request_hash` folds in each message's `file_ref` hashes in order; `is_cacheable` drops the blanket
  `file_ref` exclusion and keeps the `degraded` gate. The predicate stays **one function** shared by the
  read and write sides (Phase 3's trap 12, still true).
- Composer: an attach button, a file chip with size and a remove control, client-side rejection of an
  oversized or disallowed file *before* the upload, and an upload that happens on selection rather than on
  send — so the hash is ready when the message is.
- `MessageTurn` already renders a `file_ref` block as a chip. It stays; what it gains is the tier badge
  when the assistant's meta says the reading was degraded.
- `ModelIndicator` already renders `degraded`. It gains the *why*: "read by local OCR" vs "read by
  gemini-3.6-flash" — the same disclosure discipline `served_by` gets, applied to perception.
- Error states: 413 and 415 from the upload render as themselves, not as a generic failure.

**Done when:** a file can be attached, sent, and seen in the thread; an oversized file is refused without a
request; a degraded answer is visibly degraded; `make frontend-test` and `make frontend-lint` are green.

### Step 11 — Tests and fixtures *(2 days)*

**Files:** `tests/` throughout, `scripts/record_fixtures.py`.

- Extend the recorder with a Gemini extraction case: a committed fixture PDF in, the real structured
  response out. It is a fourth case for a provider that already has a recipe, not a new mechanism.
- The full §6 matrix.
- The concurrency test that matters most: two simultaneous first-turns on the same document make exactly
  one extraction call.

### Step 12 — ADRs, docs, deploy *(1.5 days)*

**Files:** `docs/decisions/ADR-025…030`, `docs/{architecture,limitations,deploy}.md`, `README.md`,
`CLAUDE.md`.

Per §7. The deploy half is not paperwork: the Supabase bucket has to be created and kept private, the
service-role key has to reach Render's environment, and the image roughly doubles in size — which on a
free plan is a build that can genuinely time out. Do it once, before the last commit of the phase, not
after.

---

## 5. Traps

1. **`python-multipart` missing.** FastAPI raises at *route definition* time, so the app does not boot and
   the traceback names an assertion rather than a dependency. First line of Step 1.
2. **Trusting the declared MIME.** A browser will happily say `application/pdf` about anything, and the
   allowlist is the only thing standing between the upload endpoint and handing arbitrary bytes to
   PyMuPDF. Sniff, compare, store the sniffed type.
3. **Reading the whole upload to check its size.** A `Content-Length` is a claim. Read in chunks, count as
   you go, and abort mid-stream — otherwise the cap is enforced only after the memory has been spent.
4. **Calling PyMuPDF or Tesseract on the event loop.** They are synchronous and slow, and the instance has
   one loop per worker. Every other request on that worker stops for the duration.
5. **Extracting at upload.** Costs the scarcest budget in the fleet to produce something Gemini did not
   need, and freezes the extraction into a moment (D22).
6. **Not memoizing across failover.** Render runs once per candidate. Three candidates, three extractions
   of the same document, and the third one is the one that finds the daily budget gone.
7. **Reserving perception budget for a native passthrough.** The answering model's own reservation already
   counts it; doing both double-counts one request against one real budget.
8. **Double-counting an injected attachment's tokens.** Its cost is already inside the text `materialize`
   produced. `token_cost` is for native attachments only.
9. **Letting `estimate_tokens` see base64.** A 6MB PDF is ~8M base64 characters; a character-based
   estimator turns that into a two-million-token estimate and the reservation fails closed on every
   candidate. Native cost comes from the page/tile rate, never from the payload's length.
10. **A summary at the end of the extraction.** Truncation keeps the head, so a tail-summary is the first
    thing lost on exactly the documents that need it (D28).
11. **Raw concatenation instead of the envelope.** `document_envelope` exists so a model can tell a
    document from an instruction. A PDF that says "ignore your previous instructions" is not hypothetical.
12. **A tier failure failing the request.** Every tier but the last logs and falls through. The whole
    design philosophy is one sentence — always degrade, never just fail — and this is where it is either
    true or decorative.
13. **A degraded answer that does not say so.** Worse than the degradation. `confidence="low"` →
    `RenderReport.degraded` → `MessageMeta.degraded` → the indicator, and the chain has to hold on both the
    streaming and non-streaming paths.
14. **Caching a degraded answer.** D29's whole safety argument rests on the write-side `degraded` gate.
15. **Unbounded OCR.** A 400-page scan at 150 DPI is minutes of CPU and a request that times out. Cap it,
    and say in the text that you capped it.
16. **Forgetting the extraction lock.** Two users uploading the same PDF at the same moment should spend
    one provider's quota. `keys.extraction_lock` has existed since Phase 1 for this.
17. **The service-role key in a log line, an error body, or a `RenderReport`.** It bypasses row-level
    security on the entire project. Same handling as a provider key, which is to say: it appears in exactly
    one module.
18. **A `file_ref` whose hash the user does not own.** Ownership is checked once, at the point the ref
    enters a message, in an ownership-scoped query — never by fetching and then comparing.
19. **`reserved_fraction` drifting between slots.** The same Gemini model is declared in `general`, `fast`
    and now `perception`. If the three disagree, D8's two halves stop summing to one and nothing fails. The
    Step 1 startup check is the whole defence.

---

## 6. Test matrix

| Layer | Approach |
|---|---|
| Upload endpoint | Size cap enforced mid-stream (11MB → 413, nothing stored); sniffed type overrides declared; disallowed type → 415; dedup returns one object and two responses; the hash matches `sha256` of the fixture; an unowned hash → 404 on `GET`. |
| `ObjectStore` | `MockTransport` round-trip for `SupabaseStore`; 409-on-existing is success; `LocalStore` refuses path traversal; `StorageUnavailable` on a 500, never a raw `httpx` error. |
| `perception/extractors.py` | Recorded Gemini responses only. Structured output parsed into four sections; a missing section → `confidence="medium"`; 429 on candidate 1 → candidate 2; `ContentFiltered` stops the chain; two concurrent extractions of one hash → one provider call; the built payload as a golden file. |
| `perception/local.py` | The three committed fixtures. Text-layer PDF → no OCR, `medium`; scanned PDF → OCR, `low`; PNG → OCR, `low`; 40-page scan → 10 pages OCR'd and said so; **the whole module's tests pass with Tesseract absent** (the image path skips). |
| `perception/lane.py` | The tier matrix as a table test: each tier forced by disabling the ones above it, against each of Groq/Gemini, asserting the chosen tier, the `degraded` flag and the provider-call count. Memoization: three candidates, one extraction. Tier-3 failure → `FileUnreadable`, no message row. |
| Quota | 50 concurrent perception reserves against a fence of 10 grant exactly 10; a chat and a perception reserve share `rpm` and split the day; the two daily limits sum to published-minus-headroom; a native passthrough makes **no** perception reservation. |
| Render / fitting | A native attachment's `token_cost` reaches `estimated_tokens`; an injected one is not double-counted; a document too large for the budget is truncated from the tail with its summary intact; `extraction_tier` is the worst tier across the turn. |
| Adapters | Gemini golden payload with `inline_data`; Groq and OpenRouter still raise on a native attachment, with their own messages. |
| Cache | Two identical requests over the same file hash → `HIT`, zero provider calls, zero lane calls; the same question over *different* bytes → `MISS`; a degraded answer is never written. |
| Streaming | The `meta` frame precedes the lane; `extraction_tier` and `degraded` arrive on `done`; a mid-stream restart does not re-extract. |
| Frontend | Oversized file refused before upload; the chip renders; a degraded turn shows the reason; 413/415 render as themselves. |

Coverage concentrates in `perception/` and in the tier-selection table — a wrong tier is invisible in the
answer and expensive in the budget.

---

## 7. Documentation

- **ADR-025** Extraction at render, not at upload (D22): invariant 6's retroactive-improvement payoff, why
  tier 1 cannot exist at upload time, the latency this moves into the request, and the memoization it
  forces.
- **ADR-026** File storage and ownership (D23/D24): private bucket, content-addressed paths, global bytes
  and global extractions against per-user reference rights, and why there is no download endpoint.
- **ADR-027** Perception quota under a frozen Contract C (D26): why the fence is daily and the minute is
  shared, the two-ceilings-one-counter arrangement, and why four more keys was the wrong fix.
- **ADR-028** The tier chain and its failure rule (D25): what each tier costs, why cache beats native, why
  only tier 3's failure surfaces, and what `FileUnreadable` means.
- **ADR-029** Attachment token cost (D27): why base64 length is not a token count, where the rate table
  lives, and the three consumers of the number.
- **ADR-030** The local tier's dependencies (D30): PyMuPDF's AGPL, Tesseract as a system binary, runtime
  detection over assumption, and the image-size cost of shipping it.
- `docs/architecture.md`: replace "Where this leaves Phase 4" with the two-lane diagram from
  `project-overview.md` §5.2 drawn against the real code — upload → store, then render step 1's tier
  decision inside the failover loop that is already diagrammed there.
- `docs/limitations.md`: first-turn extraction latency; the one-hour window where a cached `llm` answer
  outlives an available `native` path; cache-beats-native on layout questions; the OCR page cap; PyMuPDF's
  licence; and the free-tier privacy point the overview's §10 and the risk register both raise — an
  uploaded document is sent to Google for extraction, and the UI has to say so before it happens.
- `README.md`: the perception lane as the second headline feature, with the one-sentence version of why
  answering and perceiving are two independent decisions.
- `CLAUDE.md`: current phase, the new `app/perception/` contents, the two new repo modules, and Phase 5 as
  next.

---

## 8. Exit checklist

- [ ] Upload a PDF; ask about it on `fast` (text-only Groq) → correct answer, the log shows a Gemini
      extraction, `extraction_tier: "llm"`
- [ ] The same PDF on `general` with Gemini serving → correct answer, **no** extraction ran, tier `native`
- [ ] Ask a third time → tier `cache`, zero provider calls inside the lane
- [ ] Re-upload the same file → one object, one new `files` row, zero quota spent
- [ ] `PERCEPTION_LOCAL_ONLY=true` → the answer still arrives, `degraded: true`, and the UI says the
      document was read by OCR
- [ ] Tier 3 recovering nothing → 422 `file_unreadable`, and no assistant message was written
- [ ] Two simultaneous first-turns on the same document → exactly one extraction call
- [ ] A turn that fails over Groq → Gemini extracts once, not twice
- [ ] Gemini's perception half is spendable and its answer half is untouched by it — assert both counters
- [ ] An 11MB upload is a 413; a PNG named `.pdf` is stored as `image/png`; a `.exe` named `.pdf` is a 415
- [ ] Another user's `file_hash` in `file_refs` is a 404 and writes nothing
- [ ] A native 40-page PDF reserves a token estimate in the right order of magnitude
- [ ] The Gemini attachment golden payload is committed and reviewed
- [ ] `make test` green with zero live API calls **and** with Tesseract absent from the machine
- [ ] `make lint`, `make typecheck` clean; `make frontend-test`, `make frontend-lint` green
- [ ] The built image still deploys on Render's free plan with Tesseract in it
- [ ] ADR-025…030 written

**Realistic duration:** 16–19 working days, or ~3.5 weeks part-time. The development plan's estimate was
~2 weeks; the difference is Step 8 (the lane is where every earlier seam meets, and the failure paths
outnumber the happy one), the local tier's fixtures and its thread discipline, and the fact that this is
the first phase to add a *second* kind of stored artifact — bytes — with its own storage, ownership and
lifecycle questions. Steps 8 and 7 are the two that will overrun.

---

## 9. What Phase 4 hands to Phase 5

Phase 5 is memory and cross-provider translation, and most of what it needs is load-bearing by the end of
this phase rather than merely declared:

- The render pipeline finally runs all six steps for real. Phase 5's golden matrix — one fixed canonical
  history with a system message *and a `file_ref`*, three provider payloads — is writable because step 1
  resolves something. `tests/fixtures/golden_payloads/` gets its first attachment case here (Step 9) and
  Phase 5 extends it to all three providers.
- `MessageMeta` is fully populated for the first time: provenance from Phase 2, tokens from Phase 3,
  `extraction_tier` and `degraded` from this one. Phase 5's continuity test ("start on `fast`, switch to
  `general`, ask what I said first") reads a history that is complete.
- D4's fitting step has a real adversary. Until now the only oversized thing in a history was a long
  conversation; a 200-page PDF makes document truncation a path that runs rather than one that is tested.
- Left deliberately unbuilt, with the seam visible: `conversations.pinned_model` is still written by
  nobody (D3 is Phase 5); `memory/summarize.py` is still the §2.2.7 seam and the `summary` block type is
  still reserved and rejected by `parse_content`; `fitting.FitStrategy` still has one implemented member.
- Still untouched by design: `keys.idempotency` (D6, Phase 7), `keys_resolution/resolver.py` (BYOK,
  Phase 6), `config/pricing.yaml` (Phase 7). `scope` is threaded through the perception lane's
  reservations exactly as it is through the answer lane's, and is `keys.SYSTEM_SCOPE` at every call site —
  so Phase 6 replaces one constant in two lanes rather than one.
