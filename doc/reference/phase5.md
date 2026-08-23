# Phase 5 — Memory & Cross-Provider Translation

Implementation plan. Derived from `development-plan.md` §3 Phase 5, amended by `project-overview.md` §4.7
and `contracts-and-phase1.md` §2.2.5–§2.2.7, and written against the code Phases 1–4 actually shipped
rather than against the skeleton it was planned from.

**Read this first, because it changes what the phase is.** Phase 5's four tasks in `development-plan.md`
describe machinery that Phases 1–4 already built. Canonical history is persisted and loaded by
`conversation_id` — `api/v1/chat.py` has done it since Phase 1. Per-provider materialization exists and is
the *only* way a payload gets built — `memory/render.py` step 5. D4's fitting is implemented, algorithmic
and unit-tested — `memory/fitting.py`. The router honours `conversations.pinned_model` and has since
Phase 2 — `routing/selection.py:119`.

So Phase 5 is not "build the memory layer." It is **the phase that proves the memory layer, closes the
three seams it was allowed to leave open, and tells the user the truth about what it forgot.** That is a
smaller phase than Phases 2–4, and an honest one; the alternative is inventing work to fill a slot in a
timeline. What it must not become is a week of tests with nothing to demo — §1's definition of done is
written to prevent exactly that, and Steps 3 through 7 are all user-visible.

Three things genuinely do not exist yet, and each is a real gap rather than a formality:

1. **The cross-provider golden matrix (§2.2.6) is one third built.** Three `*_general` goldens exist, one
   `gemini_attachment` golden exists, and every one of them is asserted against `adapter.build_payload`
   directly rather than against `render()`. The property §2.2.6 actually cares about — one stored history
   crossing three providers without silent corruption — has never been asserted at all.
2. **Nothing writes `pinned_model`, and nothing tells a client a conversation is pinned.** The read side is
   complete and tested. The write side, and the `warning` field the plan and the overview's §8 both call
   for, are absent.
3. **Truncation is invisible.** `RenderReport.messages_dropped` is computed, logged, and then dropped on
   the floor. `render.py`'s own docstring says the report exists because "an answer built on two thirds of
   a conversation should say so" — and no response field, no stored `meta` key and no UI element says so.

Two smaller gaps the survey turned up, both of which belong here because both are "the gateway owns the
conversation's state" (overview §3) and neither belongs anywhere else:
`conversations.preferred_slot` is written once at creation and never updated, and the frontend never reads
it — so a thread you set to `fast` reopens as `auto`.

---

## 1. Scope

**Goal:** a conversation is a gateway-owned object that survives being answered by three different models
with three different context windows — and says out loud what it had to drop to do it.

**In scope**

- `tests/contract/test_cross_provider_matrix.py` — §2.2.6's real assertion: one fixed canonical history,
  driven through `render()`, against all three providers, with and without an attachment. Six goldens, two
  of them new.
- Integration proof of continuity: start a thread on `fast`, switch to `general`, ask what was said first,
  and assert the second provider's payload actually carried turn one. The same for a `file_ref` — Phase 4's
  handoff is what made this writable, and it is the strongest demo this phase has.
- **Truncation disclosure.** `RenderReport.messages_dropped` reaches `MessageMeta`,
  `ChatCompletionResponse`, the `done` event and the frontend indicator — the same three hops `degraded`
  and `extraction_tier` already take.
- **Truncation and the cache (D35).** A turn whose history was truncated is not written to the exact cache,
  for the same reason a `degraded` one is not.
- **`preferred_slot` persistence (D33).** The thread remembers the slot you last used on it, the API
  updates it, the composer seeds from it.
- **D3's write path (D32).** `conversations_repo.set_pinned`, the trigger predicate, the `warning` field on
  both response shapes, and the frontend disclosure that renders it.
- `docs/limitations.md` — the tool-call entry rewritten from a verdict into the reasoning behind it.
- `docs/architecture.md` — "one history, three shapes": the diagram this phase is named for.
- ADR-031, ADR-032, ADR-033.

**Explicitly NOT in Phase 5** — pulling any of these forward is how the phase stops being demoable:

- **No tool calls.** Not the schema, not the wire format, not an execution loop. D3 is locked, and this
  phase implements the *pin*, which is the mechanism that makes the absence safe. `tool_call` and
  `tool_result` stay in `RESERVED_BLOCK_TYPES`; `parse_block` keeps rejecting them.
- **No summarization.** `memory/summarize.py` stays the §2.2.7 seam, `FitStrategy` keeps one implemented
  member, and the `summary` block type stays reserved. D4 says truncate; this phase makes truncation
  *visible*, which is the honest alternative to making it unnecessary.
- **No message pagination.** `list_for_conversation` stays unpaginated, deliberately — the fitting step
  needs the complete history to decide what to drop, and that need is independent of how much of it the UI
  has scrolled into view. The paginated *sibling* function is Phase 7 task 6, which forbids touching
  `list_for_conversation` itself for this exact reason.
- **No title generation.** `conversations.title` stays nullable; the UI keeps deriving a display name.
- **No BYOK.** `scope` stays `keys.SYSTEM_SCOPE` at every call site. Phase 6 replaces one constant.
- **No new provider, no new slot, no `pricing.yaml`, no `/metrics`, no idempotency.** Phases 6 and 7.
- **No change to any frozen contract signature.** Every field added below is an *additive optional* one on
  a wire model or on `MessageMeta`, whose `from_jsonb` is already lenient about absent keys. If a step
  seems to need a Contract A, B or C signature change, stop and ask — §2's table says which seam was meant
  to absorb it.

**Definition of done — one thread, demoed live:**

1. Start a conversation on `fast` (Groq). Say something distinctive. Get an answer.
2. Switch the picker to `general` and ask *"what did I say first?"* — the answer quotes turn one and
   `served_by` names a different provider than turn one's. Exhaust or revoke that provider and ask a third
   time: the slot's failover spills to OpenRouter, and the thread still recalls turn one — three providers,
   one history. (`config/providers.yaml` carries two public slots, `general` and `fast`; the third provider
   is reached by failover *within* a slot, not by a third slot name. Do not invent one.)
3. Attach a PDF on turn one under `fast`, then ask about it on `general`: the same stored `file_ref`
   renders as injected text for one and as `inline_data` for the other, with no second upload and no second
   extraction (`extraction_tier` goes `llm` → `cache` or `native`).
4. Reload the page. The thread reopens on the slot you last used, not on `auto`.
5. Drive a 200-message history into a small-context slot: it answers, the response carries
   `messages_dropped: 148`, the UI says "148 earlier messages omitted", the payload carries exactly one
   omission marker, no provider ever returned `ContextTooLong`, and nothing was written to the cache.
6. Pin a conversation (a direct row write, as `test_a_pinned_conversation_ignores_the_requested_slot`
   already does) and ask for a different slot: the answer comes from the pinned model and the response
   carries `warning: "conversation pinned to gemini/gemini-3.6-flash due to prior tool use"`.

**No Alembic migration.** Nothing here adds a column. `MessageMeta` grows one key inside an existing JSONB
column, which `from_jsonb` already tolerates in both directions.

---

## 2. What Phases 1–4 left, and what Phase 5 does to each seam

| Seam | Where | State today | Phase 5 |
|---|---|---|---|
| Canonical history persisted, loaded by `conversation_id` | `api/v1/chat.py`, `db/repo/messages.py` | **done since Phase 1** | proved by Step 2; not touched |
| `render()`'s six steps | `memory/render.py` | **done**; all six run for real since Phase 4 | not touched |
| `fitting.fit` (D4) | `memory/fitting.py` | **done**, unit-tested | exercised by a real multi-turn history (Step 3); algorithm not touched |
| `selection.candidates(pinned=…)` | `routing/selection.py:119` | **done since Phase 2** | not touched — Step 6 only gives it something to honour |
| `conversations.pinned_model` | `db/models.py:176` | column exists, **no writer** | `set_pinned` writes it (Step 6) |
| `conversations.preferred_slot` | `db/models.py:172` | written at create, **never updated, never read by the UI** | updated per turn, seeded into the composer (Step 5) |
| `RenderReport.messages_dropped` | `memory/render.py` | computed, logged, **discarded** | reaches `meta`, both response shapes, the UI (Step 3) |
| `MessageMeta` | `memory/canonical.py:161` | 11 fields | gains `messages_dropped: int = 0` |
| `ChatCompletionResponse` / `DoneEvent` | `schemas/chat.py`, `streaming/sse.py:172` | carry `degraded`, `extraction_tier` | gain `messages_dropped` and `warning` |
| `is_cacheable(degraded=…)` | `cache/exact.py:102` | two dimensions | gains `truncated` (D35) — same write-side shape as `degraded` |
| golden matrix (§2.2.6) | `tests/unit/test_*_payload.py` | three per-adapter `build_payload` goldens plus one attachment golden | one `render()`-level matrix, six goldens (Step 1) |
| `provider_fixtures.canonical_history_with_attachment` | tests | built, used by one provider | used by all three |
| `RESERVED_BLOCK_TYPES` | `memory/canonical.py:95` | `tool_call`, `tool_result`, `summary` rejected | **unchanged** — D32 explains why the pin's trigger needs no unfreezing |
| `memory/summarize.py` | module | typed seam, raises | **unchanged** |
| `docs/limitations.md` "out of scope" | `docs/limitations.md:215` | one paragraph on tool calls | a section, carrying the reasoning (Step 8) |

**No new application module.** Everything below lands in a file that already exists. If a step is producing
a new file under `app/`, that is a signal to re-read this table.

---

## 3. Decisions to settle before writing code

Five questions the frozen contracts do not answer, continuing Phase 4's numbering (D22–D30 are spent) and
`docs/decisions/`'s (ADR-030 is the last written). In each case the reasoning, not the verdict, is the
deliverable.

### D31 — Does the golden matrix measure `build_payload`, or `render()`?

§2.2.6 says "one fixed canonical history containing a system message, a file_ref, and five turns → three
committed payload files, one per provider." The existing goldens call `adapter.build_payload` directly.
That is the right test for what those files were written to protect — `build_payload` is pure, so a golden
on it is a golden on a function with no dependencies — but it cannot express §2.2.6's actual claim, for one
reason: **a `file_ref` has no payload shape until something decides whether it is native or injected, and
that decision is render step 1, not `build_payload`.**

**Decision: the matrix drives `render()`, with a scripted resolver.** A new
`tests/contract/test_cross_provider_matrix.py` renders one history through all three adapters and asserts
six committed goldens. The resolver is a test double in `tests/provider_fixtures.py` that answers tier 1's
question and nothing else — `spec.supports_mime(mime)` → native with bytes, otherwise injected with a fixed
extraction text. No database, no Redis, no `PerceptionResolver`.

Three consequences, all wanted:

- **The three existing `*_general` goldens are reused byte-for-byte, not copied.** The matrix reads the same
  files `tests/unit/test_*_payload.py` reads. If `render()` produces one different byte from a direct
  `build_payload` call on a history that needs no fitting, that is a real bug in the pipeline's
  transparency, and this matrix is the thing that finds it.
- **`tests/contract/` is the right directory**, not `tests/unit/`. This asserts an agreement between three
  implementations of one protocol, which is what that directory already holds
  (`test_adapter_conformance.py`).
- **The per-adapter payload tests stay exactly as they are.** They cover purity, clamping, role mapping and
  refusal shapes. They are not superseded and must not be deleted or folded in.

The scripted resolver rather than `PerceptionResolver`: a golden that needs Postgres, Redis and object
storage to produce a diff is a golden nobody runs on a red build. `PerceptionResolver`'s own tier logic is
already covered by `tests/integration/test_perception_lane.py`, which is where it belongs.

### D32 — What writes `pinned_model`, when v1 cannot store a tool call?

The circular problem. `development-plan.md` says "if a message has tool-call content, set `pinned_model`."
But `tool_call` and `tool_result` are in `RESERVED_BLOCK_TYPES`, `parse_block` rejects them, and
`ChatCompletionRequest` is `extra="forbid"` with no `tools` field — so no history v1 can store, and no
request v1 can accept, will ever carry tool content. Writing a trigger against a condition that cannot
occur and calling the phase done is the kind of thing that reads fine in a commit and is discovered to be
theatre in an interview.

Three options:

- **(a) Leave the whole thing as a seam and document it.** Honest — and it leaves the overview's §8
  ("per-conversation pinning") with a hole, plus a `warning` field the overview specifies and nothing ever
  emits.
- **(b) Unfreeze the reserved block types enough to accept and store tool content.** That is Phase 5
  quietly becoming the tool-call phase. D3 is locked; refuse.
- **(c) Build the pin as a complete, reachable mechanism whose *trigger* is the one deferred part.**

**Decision: (c).** Concretely, in descending order of how load-bearing each piece is:

- **`conversations_repo.set_pinned(session, *, conversation_id, user_id, model) -> bool`** — real,
  ownership-scoped inside the SQL like every sibling, returning `bool` like `touch` and `rename`. Idempotent
  when re-pinning to the same model. Re-pinning to a *different* model raises rather than silently
  relocating a conversation — a pin exists precisely because that history cannot move, so an UPDATE that
  moved one would defeat the feature. Enforce it in the WHERE clause (`pinned_model IS NULL OR pinned_model
  = :model`) and let a zero rowcount on an existing conversation be the signal.
- **`pin_target(history) -> str | None`** — a pure predicate beside the invariants in
  `memory/canonical.py`. Returns the `"{provider}/{model}"` of the first message whose content carries a
  reserved tool block, else `None`. This is a real implementation over a real input domain; it is v1's
  *storage* rules, not this function, that keep it returning `None`. Unit-tested directly against
  hand-constructed blocks — `CanonicalMessage.content` is a plain list, so a test can build the input
  `parse_block` will not accept from JSON, which is exactly the seam a phase-later feature needs.
- **The `warning` field is live today and must be demoed.** `ChatCompletionResponse.warning: str | None`
  and `DoneEvent.warning: str | None`, emitted whenever `conversation.pinned_model` is set and the
  requested slot names something else — the wording from `development-plan.md`, with the real model name in
  it. `tests/integration/test_chat_endpoint.py:796` already pins a conversation by direct row write and
  asserts the routing half; Step 6 extends that same shape to the disclosure half.

So the only unreachable line in Phase 5 is one branch of `pin_target`, and it is unreachable because a
locked decision says so. That is a documented limitation. A silently-passing `set_pinned` would not be.

### D33 — `preferred_slot` is a UI default, not a routing input

`conversations.preferred_slot` is set at creation from the first request's `model` and never updated;
`ConversationView` initialises its picker to the constant `DEFAULT_SLOT` and never reads the column. Two
halves of one bug: pick `fast` on turn nine, reload, and you are silently back on `auto`.

The tempting fix is to make `preferred_slot` a routing input — "if the request omits `model`, use the
thread's preference." **Reject that.** `ChatCompletionRequest.model` defaults to `"auto"`, so the server
cannot distinguish "omitted" from "explicitly asked for auto", and inventing a sentinel to tell them apart
changes the request contract to solve a frontend state problem.

**Decision: `preferred_slot` is display state the gateway happens to own.** The request body's `model` stays
the only routing input, unchanged and unconditional. Per turn, when `body.model != conversation.preferred_slot`,
the column is updated — folded into the existing `touch()` UPDATE, since it is the same unit of work and
costs no extra round trip. The frontend seeds its picker from `conversation.preferred_slot` on load and
falls back to `DEFAULT_SLOT` for a thread that does not exist yet.

The distinction from `pinned_model` is the whole point, and belongs in the code comments: a **preference**
is what the UI offers you next time and you override it in one click; a **pin** is a constraint the router
enforces and the client cannot override at all. Two columns, because they are two different kinds of fact.

### D34 — Truncation is disclosed on the wire, exactly like `degraded`

D4 chose truncation over summarization because truncation is testable. The cost it accepted is that a user
can get an answer built on two thirds of their conversation. `render.py` already treats that as something
owed to the user — `RenderReport`'s docstring calls `messages_dropped` "the honest-degradation story D4
owes" — and then the number stops at the log line.

**Decision: `messages_dropped` takes the same three hops `extraction_tier` took in Phase 4**, and copying
that path *is* the implementation:

1. `MessageMeta.messages_dropped: int = 0`, in `to_jsonb` and `from_jsonb` — so re-opening the thread
   tomorrow still says the answer was built on a partial history.
2. `ChatCompletionResponse.messages_dropped: int = 0` and `DoneEvent.messages_dropped: int = 0`, sourced
   from `outcome.report` and from the orchestrator's `_State` respectively — `orchestrator.py:515` is the
   exact pair of lines to mirror.
3. The frontend indicator renders it beside `degraded`.

**An integer, not a boolean.** `RenderReport.truncated` already exists as the derived property and the
frontend can derive it too; "148 earlier messages omitted" and "some earlier messages were omitted" are
different sentences, and only one of them is worth reading. `documents_truncated` stays report-only —
Phase 4's `degraded`/`extraction_tier` pair already covers "your document did not fully reach the model",
and a fourth attachment-shaped field on the wire buys nothing the third does not.

### D35 — A truncated answer must not be cached

Newly reachable because of this phase, and the interesting bug in it.

`request_hash` folds in the **requested slot** and the **full canonical history**, deliberately and per
ADR-023: the *served* model is an accident of a failover race, not part of the question asked. That
reasoning is correct and stays. But under `auto`, the served model decides the context window, the context
window decides the input budget, and the budget decides how much of that history the model was actually
shown. So two byte-identical `temperature: 0` requests can produce one answer built on the whole thread
(Gemini, 1M window) and one built on its last twenty turns (Groq, 128k) — and ADR-023's hash cannot tell
them apart, so the second request is served the first one's answer for an hour.

Folding the served model or its window into the hash would reverse ADR-023 for a case ADR-023 already
weighed. Refusing to cache under `auto` would throw away most of the cache's value.

**Decision: the write side declines to cache a turn whose history was truncated.** `is_cacheable` gains a
`truncated: bool = False` parameter, defaulted for the read side and supplied by the write side from
`outcome.report.truncated`, mirroring exactly how `degraded` works today (D29, trap 14). The reasoning is
the same reasoning: an answer the gateway itself knows was built on an incomplete input is precisely the
answer that must not be replayed for an hour to someone who might have got the complete one.

Non-obvious, and worth writing into the docstring: this is **not** symmetric with the read side, and must
not be. The read side cannot know whether a hit *would* have been truncated without rendering against a
candidate it has not chosen yet — and if the entry exists at all, it was written by a turn that was not
truncated. A stored entry is by construction a whole-history answer. One gate on the write side is
sufficient; a second on the read side would be dead code.

---

## 4. Implementation steps

Eight steps, three milestones. Each is a commit, each leaves the suite green, and each names the files it
is allowed to touch.

**Milestone A — one history, every provider** (Steps 1–2). The §2.2.6 property is asserted, and continuity
across a switch is proved end to end.

**Milestone B — the thread remembers, and says what it forgot** (Steps 3–5). Truncation becomes visible,
the cache stops replaying partial answers, and the slot you picked survives a reload.

**Milestone C — the honest edges** (Steps 6–8). The pin's write path, the frontend, and the documentation
that explains why the hard case is not solved.

Before starting: `make test`, `make lint`, `make typecheck`, `make frontend-test`, `make frontend-lint` all
green on `main`. If any is red, that is the first commit and it is not part of this phase.

### Step 1 — The cross-provider golden matrix *(1.5 days)*

Implements D31. **Touches only `tests/`.** If this step edits anything under `app/`, the pipeline disagreed
with the adapters and that disagreement is the finding — stop and report it before changing either.

1. In `tests/provider_fixtures.py`, add a scripted resolver double implementing
   `render.AttachmentResolver`:
   - `resolve(refs, spec)` returns one `ResolvedAttachment` per ref, deduplicated by hash.
   - `mode="native"` with `data=attachment_bytes()` and the D27 `token_cost` when
     `spec.supports_mime(mime)` and the byte count is within `spec.max_file_bytes`; otherwise
     `mode="injected"` with a fixed, committed extraction text and `confidence="high"`.
   - The extraction text is a module constant, multi-line, and short enough to read in a diff — it will
     appear verbatim inside `document_envelope` in two goldens, and a golden nobody can read is a golden
     everybody re-blesses.
   - No I/O of any kind. Follow `canonical_history()`'s discipline: fixed hashes, fixed text, nothing
     derived from a clock.
2. Add `tests/contract/test_cross_provider_matrix.py`. For each of the three providers, two cases:
   - **`{provider}_general`** — `canonical_history()` through `render()`, asserted against the *existing*
     golden file. Reuse the same `ModelSpec`/`GenParams` the per-adapter payload tests build, so the
     comparison is apples to apples; lift them into `provider_fixtures.py` if they are currently local to
     those modules, and update those modules to import them.
   - **`{provider}_attachment`** — `canonical_history_with_attachment()` through `render()` with the
     scripted resolver, asserted against a golden. `gemini_attachment.json` exists and must not change.
     `groq_attachment.json` and `openrouter_attachment.json` are new.
3. Add three assertions that are the actual point of the matrix, not incidental to it:
   - **The system message lands in the right place.** Gemini's payload has a top-level `system_instruction`
     and no `system` role inside `contents`; Groq's and OpenRouter's have `messages[0].role == "system"` and
     no top-level field. Assert this structurally, not only via the golden diff — a golden says "this
     changed", and this assertion says *what* is wrong when it does.
   - **The omission marker survives into every provider's payload text.** It has no native representation
     anywhere, so it must be rendered into prose identically three times.
   - **The document envelope is byte-identical across the two injected providers.** Extract the
     `<document …>` block from each payload's text and assert equality. This is the one thing
     `document_envelope`'s docstring promises and no test currently checks across providers.
4. Add a `bless` entrypoint following the convention in `tests/unit/test_gemini_payload.py:409` —
   `python -m tests.contract.test_cross_provider_matrix` rewrites the six files via `fx.dump_golden`. Keep
   the existing per-adapter bless scripts working.
5. `RenderReport` is worth asserting once here too: `attachments_native == 1` for Gemini, `1` injected for
   the other two, and `estimated_tokens` strictly greater in the Gemini case (D27's tile cost, which no
   character estimator can see).

**Done when:** six goldens committed, the three pre-existing ones unchanged, `make test` green, and running
the bless script produces no diff.

### Step 2 — Continuity across a provider switch, end to end *(1 day)*

**Touches only `tests/integration/test_chat_endpoint.py`** (and `tests/conftest.py` if a fixture is needed).
This is the test that `development-plan.md`'s exit criterion names and that no existing test performs:
`test_a_second_turn_sends_the_whole_history` never switches provider, and `test_failover_crosses_providers`
never sends a second turn.

1. **`test_a_thread_survives_a_provider_switch`** — three turns on one `conversation_id`. Turn one requests
   `fast`; turn two requests `general`; turn three requests `general` again with the mock transport failing
   the first two candidates so the slot spills to OpenRouter. There are only two public slots, so the third
   provider is reached by failover, not by a third slot name — which also makes the test a better one, since
   it proves history survives a *substitution* and not only a user's deliberate switch. Capture each outbound
   payload. Assert: turn one's user text appears in turn three's payload; the shapes are the three different
   provider shapes (Gemini `contents`/`parts` on turn two, OpenAI-shaped `messages` on turns one and three);
   each assistant row's `meta.provider_used` names its own provider; and the conversation holds exactly six
   messages in gap-free `seq` order.
2. **`test_a_file_ref_survives_a_provider_switch_from_cache`** — Phase 4's handoff, cashed. Upload once,
   reference the hash on turn one under `fast`, ask about it again on turn two under `general` *without*
   repeating `file_refs` (render step 1 scans the whole history for `file_ref` blocks, not only the current
   turn's message). Assert: one `files` row, one stored `file_ref` block, Groq's payload carries the
   `<document …>` envelope, and the second turn ran no extraction (assert on the tier: `llm` then — the
   finding this test surfaced, corrected here rather than swept away — `cache`, not `native`: `lane.py`'s
   tier 0 check returns a stored `llm` reading unconditionally, *before* tier 1 ever asks whether the second
   candidate could have read the file natively, so Gemini's second answer also carries the envelope rather
   than `inline_data`. That is exactly §1's own DoD item 3, which already hedges with "`extraction_tier` goes
   `llm` → `cache` *or* `native`" rather than promising native outright — this is the `cache` half of that
   hedge, and it is `docs/limitations.md`'s "cache-beats-native on layout questions" line, exercised rather
   than only documented. No second call to the perception slot, either way.
3. **`test_streaming_and_non_streaming_agree_on_a_switched_thread`** — the same switch with `stream: true`
   on the second turn, asserting the `done` event's `served_by` and the persisted history match the
   non-streaming twin. The two paths render through the same `render()` call, and this is the test that
   keeps that true.
4. Keep every existing test in the module passing unchanged. If one needs editing to accommodate a new
   fixture, that is a signal the fixture is wrong.

**Done when:** the three tests pass, and deleting `pinned=` from `routing.route`'s call in `chat.py` does
*not* make any of them fail (they must be testing memory, not pinning).

### Step 3 — D4 under a real history: fitting exercised, truncation disclosed *(1.5 days)*

Implements D34. Touches `memory/canonical.py`, `schemas/chat.py`, `streaming/sse.py`,
`streaming/orchestrator.py`, `streaming/collector.py`, `api/v1/chat.py`, and tests.

1. `MessageMeta.messages_dropped: int = 0`. Add it to `to_jsonb` and to `from_jsonb` via the existing
   `_int("messages_dropped", 0)` helper. Docstring: what it means, and that a non-zero value on a stored
   assistant row means that answer was built on a partial history — a fact that survives a page reload.
2. `ChatCompletionResponse.messages_dropped: int = 0`, populated in `_to_response` from
   `outcome.report.messages_dropped`. A D19 cache hit passes `0` — nothing was rendered.
3. `DoneEvent.messages_dropped: int = 0`. Mirror `extraction_tier` exactly: `_State` gains the field, it is
   assigned from `event.report` at `orchestrator.py:515`, and it is read at both `done` construction sites.
   `Collector` copies it onto the persisted `MessageMeta` alongside `degraded`.
4. The non-streaming path writes it onto the assistant `MessageMeta` in `chat.py`, beside `degraded` and
   `extraction_tier`.
5. Tests:
   - Unit: `MessageMeta` round-trips the new key; a stored row *without* it reads back as `0`.
   - Integration, non-streaming: drive a history past a small-context slot's budget and assert the response
     carries a non-zero `messages_dropped`, the assistant row's `meta` carries the same number, the outbound
     payload contains exactly one omission marker, and no `ContextTooLong` was raised. Build the long
     history through the repo directly rather than through 200 HTTP round trips.
   - Integration, streaming: the `done` event carries the same number for the same history.
   - The 200-message case from `development-plan.md`'s exit criteria, as its own named test.

**Done when:** the exit criterion "feed a 200-message history to a small-context model → truncation happens,
no provider error" is a green test, and the number is visible on the wire.

### Step 4 — Truncation and the exact cache *(0.5 day)*

Implements D35. Touches `cache/exact.py`, `api/v1/chat.py`, `streaming/collector.py`, and tests.

1. `is_cacheable` gains `truncated: bool = False`, returning `False` when it is true. Extend the docstring
   with D35's asymmetry argument — specifically *why* the read side does not pass it and why that is not an
   oversight.
2. Non-streaming write side in `chat.py`: pass `truncated=outcome.report.truncated` beside the existing
   `degraded=`.
3. Streaming write side: `Collector` already gates on `result.degraded` before writing the cache
   (`collector.py:148`). It needs the same truncation fact, which reaches it the way `degraded` does — via
   the result object populated from the report in Step 3. Route it through the same field, not a second
   parallel one.
4. Tests: two identical `temperature: 0` requests whose history exceeds the budget produce two `X-Cache: MISS`
   responses and no cache entry; the same pair under a large-window slot still produces a HIT (the gate must
   not have broken caching generally).

**Done when:** a truncated turn never writes an entry, and `tests/unit/test_exact_cache.py` and
`tests/integration/test_chat_cache.py` are both green with the new case added.

### Step 5 — `preferred_slot`: the thread remembers what you picked *(1 day)*

Implements D33's server half. Touches `db/repo/conversations.py`, `api/v1/chat.py`, tests.

1. Extend `conversations_repo.touch` — or add `touch(…, preferred_slot: str | None = None)` — so the
   activity bump and the preference update are one UPDATE. Keep `clock_timestamp()`. Do not add a second
   round trip; the docstring on `touch` explains why that UPDATE is where this belongs.
2. `chat.py` passes `body.model` on every turn. The repo writes it only when it differs, so an unchanged
   preference costs nothing extra in the statement.
3. Comment, at the call site, on the D33 distinction: this is a display preference, the request body is
   still the only routing input, and `pinned_model` is the column that constrains routing.
4. Tests: a second turn requesting a different slot updates `preferred_slot`; `GET /v1/conversations/{id}`
   reflects it; someone else's conversation still 404s and writes nothing; a `pinned_model` conversation's
   `preferred_slot` still tracks what the user *asked* for, not what served it (they are different facts and
   the test should say so).

**Done when:** `GET /v1/conversations/{id}` returns the last slot the user actually requested on that thread.

### Step 6 — D3's write path: pinning and the `warning` field *(1.5 days)*

Implements D32. Touches `db/repo/conversations.py`, `memory/canonical.py`, `schemas/chat.py`,
`streaming/sse.py`, `streaming/orchestrator.py`, `api/v1/chat.py`, tests.

1. `conversations_repo.set_pinned(session, *, conversation_id, user_id, model) -> bool` — ownership-scoped
   in the SQL, with `(pinned_model IS NULL OR pinned_model = :model)` in the WHERE clause so a re-pin to a
   different model cannot succeed. Document why a moved pin is worse than a failed one.
2. `canonical.pin_target(history) -> str | None` — the pure predicate from D32. Beside the invariant
   helpers. Its docstring carries D3: what it is for, why it returns `None` for every history v1 can store,
   and what would have to change for it not to.
3. `ChatCompletionResponse.warning: str | None = None` and `DoneEvent.warning: str | None = None`. Built by
   one shared helper (`selection.py` or `chat.py`, whichever keeps the streaming and non-streaming paths
   reading from the same function — `_is_substitution` already sets the precedent and the reasoning). The
   text is `development-plan.md`'s: `f"conversation pinned to {model} due to prior tool use"`. Emitted when
   `conversation.pinned_model` is set **and** the requested slot is not the one that pin resolves to.
4. `chat.py` calls `pin_target(history)` after a successful turn and, on a non-`None` result with
   `conversation.pinned_model is None`, calls `set_pinned` in the same transaction as the assistant row.
5. Tests:
   - Unit: `pin_target` returns the right model for a hand-constructed `tool_call` block, `None` for every
     history the schema can actually produce, and the first match when several exist.
   - Unit: `set_pinned` is idempotent for the same model, returns `False` for a different one, and `False`
     for someone else's conversation.
   - Integration: extend the shape of `test_a_pinned_conversation_ignores_the_requested_slot` — the pinned
     turn now also carries the `warning`, an unpinned turn carries `warning: null`, and a pinned turn that
     *asked for* the pinned slot carries no warning either (nothing was overridden).
   - Integration, streaming: the `done` event carries the same warning.

**Done when:** a pinned conversation discloses its pin on both paths, and the only unexercised line in the
feature is `pin_target`'s tool-block branch — reachable in a unit test, unreachable through the API, and
documented as such.

### Step 7 — Frontend: truncation, pinning, and the remembered slot *(1.5 days)*

Touches `frontend/lib/types.ts`, `frontend/lib/provenance.ts`, `frontend/components/ModelIndicator.tsx`,
`frontend/components/ConversationView.tsx`, `frontend/lib/hooks.ts`, `frontend/tests/`.

1. `types.ts`: `messages_dropped` and `warning` on `ChatCompletionResponse` and `DoneEvent`;
   `messages_dropped` on `MessageMeta`.
2. `provenance.ts`: `Provenance` gains `messagesDropped: number` and `warning: string | null`, populated in
   all four constructors (`fromMessageMeta`, `fromCompletion`, `fromDoneEvent`, `fromMetaEvent` — the last
   one has no report yet, so `0`/`null`, the same way it already handles `degraded`).
3. `ModelIndicator.tsx`: render "N earlier messages omitted" when `messagesDropped > 0`, and the pin warning
   when present. Follow the component's existing rule — these are *disclosures*, in the same visual register
   as the degraded notice, not errors.
4. `ConversationView.tsx`: seed `modelSlot` from the loaded conversation's `preferred_slot`, falling back to
   `DEFAULT_SLOT`. Handle the load race — the conversation arrives after first paint, so the picker must
   adopt the stored slot once, without stomping a choice the user made in the meantime. `NewConversation.tsx`
   keeps `DEFAULT_SLOT`; there is no thread to remember yet.
5. `hooks.ts:599` currently writes an optimistic `preferred_slot: done.requested_slot` into the local cache.
   That guess is now the server's answer — keep the optimistic write (it is correct and avoids a refetch
   flash) and add a comment saying it now mirrors a persisted column rather than inventing one.
6. Tests: `ModelIndicator.test.tsx` gains a truncation case and a pin-warning case; add a test that the
   composer opens on a thread's stored slot.

**Done when:** `make frontend-test` and `make frontend-lint` are green, and the definition-of-done steps 4
and 5 are visible in the browser.

### Step 8 — ADRs, docs, and the limitations entry *(1 day)*

No application code. Three ADRs, following the existing `context / decision / consequences / why` shape:

- **ADR-031 — The cross-provider golden matrix** (D31). Why the matrix renders instead of building a
  payload, why the existing goldens are reused rather than duplicated, and why the per-adapter tests stay.
- **ADR-032 — Pinning without tool calls** (D32). The circular problem, the three options, and why a
  complete mechanism with one deferred trigger beats both a pure seam and an unfrozen block type. This is
  the ADR most worth writing well — it is the "what did you deliberately not solve, and why" answer in
  §13 of the overview.
- **ADR-033 — Truncation is disclosed, and not cached** (D34 + D35 together; they are one argument about
  one fact reaching two destinations). Include D35's asymmetry note in full.

D33 does not get an ADR — it is a bug fix with a comment, and `docs/decisions/` should not fill with
entries for the obvious. Say that in ADR-031's or ADR-033's consequences if it helps a reader wondering
where it went.

Docs:

- **`docs/limitations.md`** — promote the tool-call paragraph in "Explicitly out of scope for v1" to its own
  section carrying the reasoning `development-plan.md` task 5 asks for: schema incompatibility between
  providers, the absence of any lossless mapping for *parallel* calls specifically, the fact that production
  gateways solve this by pinning too, and what the gateway does instead (pin, disclose, never translate).
  Add a short paragraph on truncation-as-disclosed-degradation and the cache's D35 gate.
- **`docs/architecture.md`** — a new "Phase 5: one history, three shapes" section, after the Phase 4 lanes
  diagram. One canonical history, three payload shapes, with the system message rendered in its two
  positions — that single divergence is the whole reason the canonical schema exists, and it should be
  visible in one glance.
- **`README.md`** — extend the feature section with cross-provider continuity, framed around the demo:
  one thread answered by three different providers, still remembering its first turn.
- **`CLAUDE.md`** — via the `update-claude-md` skill, not by hand.

**Done when:** all five `make` targets green and every document above updated in one commit.

---

## 5. Traps

1. **Do not touch `list_for_conversation`.** Phase 7 task 6 says so explicitly, and the reason is this
   phase's own subject matter: the fitting step needs the complete history to decide what to drop. A
   paginated read here would silently truncate before D4 ever saw the messages, and the omission marker
   would then under-report.
2. **The golden matrix must not lower the bar to make itself pass.** If `render()` disagrees with a
   committed `build_payload` golden, the finding is the disagreement. Do not re-bless the file, and do not
   loosen the assertion to a subset comparison.
3. **`messages_dropped` on a cache hit is `0`, not the number the original turn recorded.** Nothing was
   rendered this turn. The original number lives on the original turn's own row, which is exactly the split
   `extraction_tier` already makes on a hit (`chat.py`'s `_serve_cache_hit` sets it to `None` and says why).
4. **`pin_target` runs against the stored history, not the request body.** A predicate reading `body.messages`
   would be answering "did this turn use a tool", which is not the question — D3 pins on the conversation's
   history, permanently, from the first occurrence.
5. **A pin is not a preference and must not be written to `preferred_slot`, ever.** They will look
   interchangeable while both are strings naming a model-ish thing. `pinned_model` is `provider/model`;
   `preferred_slot` is a slot name. `selection._resolve_pin` already raises on a slot name being passed as a
   pin, which is the guard that will catch this if it happens.
6. **The `warning` field is not an error field.** It rides on a 200 with a real answer in it. It must not be
   routed through `to_app_error`, must not set a non-2xx status, and the frontend must not render it as a
   `TurnErrorCard`.
7. **`from_jsonb` leniency cuts both ways and that is the point.** Do not add a migration to backfill
   `messages_dropped` onto old rows. An absent key reading back as `0` is correct: those turns were rendered
   before the field existed and nobody knows whether they were truncated. Backfilling `0` would assert
   something untrue; leaving it absent asserts the default, which is the honest reading.
8. **Adding a field to `DoneEvent` is a wire change with three writers.** `orchestrator.py` builds it in two
   places (the success path and the failure path). Missing the second is how a failed stream comes back with
   a default that contradicts what it actually did — the same trap Phase 4 hit with `extraction_tier`.
9. **The scripted resolver must not import `PerceptionResolver`.** It exists to make the matrix independent
   of Phase 4's runtime. If it grows a session parameter, it has become the thing it was written to avoid.
10. **`ConversationView`'s slot seeding has a race.** The conversation loads asynchronously; a naive
    `useState(conversation?.preferred_slot ?? DEFAULT_SLOT)` captures `undefined` on first render and never
    updates, and a naive `useEffect` that syncs on every change will stomp the user's pick mid-thread. Adopt
    once, on the transition from "no conversation loaded" to "loaded".

---

## 6. Test matrix

| Layer | What | Where |
|---|---|---|
| Contract | One history × three providers × {no attachment, attachment} → six goldens, through `render()` | `tests/contract/test_cross_provider_matrix.py` (new) |
| Contract | System message position asserted structurally per provider | same |
| Contract | `document_envelope` byte-identical across the two injected providers | same |
| Unit | `pin_target` over hand-built tool blocks, and over every history v1 can store | `tests/unit/test_canonical.py` |
| Unit | `MessageMeta.messages_dropped` round-trip; absent key reads `0` | `tests/unit/test_canonical.py` |
| Unit | `is_cacheable(truncated=True)` is `False`; read side still defaults | `tests/unit/test_exact_cache.py` |
| Unit | `set_pinned` idempotent / rejects a re-pin / scoped to owner | `tests/integration/test_repo_conversations.py` |
| Integration | Three-turn thread across three providers (slot switch, then in-slot failover); turn one recalled in turn three | `tests/integration/test_chat_endpoint.py` |
| Integration | One `file_ref`, two providers, two render modes, one extraction | same |
| Integration | 200-message history into a small-context slot: answers, one marker, non-zero `messages_dropped`, no `ContextTooLong` | same |
| Integration | Truncated turn is not cached; untruncated turn still is | `tests/integration/test_chat_cache.py` |
| Integration | Pinned conversation: routing honoured *and* `warning` disclosed, both paths | `tests/integration/test_chat_endpoint.py` |
| Integration | `preferred_slot` follows the last requested slot; reflected by `GET /v1/conversations/{id}` | `tests/integration/test_conversations_endpoints.py` |
| Frontend | Indicator renders truncation count and pin warning | `frontend/tests/ModelIndicator.test.tsx` |
| Frontend | Composer opens on the thread's stored slot | `frontend/tests/` (new or in an existing view test) |

Coverage target is unchanged. The concentration for this phase is `memory/` and `db/repo/conversations.py`.

---

## 7. Documentation

| Document | Change |
|---|---|
| `docs/decisions/ADR-031-cross-provider-golden-matrix.md` | new (D31) |
| `docs/decisions/ADR-032-pinning-without-tool-calls.md` | new (D32) |
| `docs/decisions/ADR-033-truncation-disclosed-and-uncached.md` | new (D34, D35) |
| `docs/limitations.md` | tool-call reasoning promoted to a section; truncation disclosure and the D35 cache gate added |
| `docs/architecture.md` | new "one history, three shapes" section |
| `README.md` | cross-provider continuity in the feature list |
| `CLAUDE.md` | phase status, via the `update-claude-md` skill |
| `doc/reference/phase5.md` | this file — the step-by-step account, kept accurate as steps land |

---

## 8. Exit checklist

- [ ] Six goldens committed; the three pre-existing ones byte-unchanged; bless script produces no diff.
- [ ] A thread answered by Groq, then Gemini, then OpenRouter recalls its first turn in its third.
- [ ] One uploaded file, referenced once, renders native for one provider and injected for another inside
      one thread, with one extraction.
- [ ] A 200-message history into a small-context slot answers, drops messages, reports the count, and never
      raises `ContextTooLong`.
- [ ] `messages_dropped` is on the response, on the `done` event, on the stored `meta`, and in the UI.
- [ ] A truncated turn writes no cache entry; an untruncated one still does.
- [ ] Reopening a thread restores the slot last used on it.
- [ ] A pinned conversation routes to its pin and says so in `warning`, on both paths.
- [ ] `RESERVED_BLOCK_TYPES`, `memory/summarize.py` and `FitStrategy` are untouched.
- [ ] No migration in `alembic/versions/`.
- [ ] Three ADRs, the limitations section, the architecture diagram, the README.
- [ ] `make test`, `make lint`, `make typecheck`, `make frontend-test`, `make frontend-lint` all green.

---

## 9. What Phase 5 hands to Phase 6

**The last constant is now the only thing standing between the gateway and BYOK.** Every call site in both
lanes passes `scope=keys.SYSTEM_SCOPE`; Phase 5 adds no new one. `keys_resolution/resolver.py` is still an
empty package, which is Phase 6's first file.

**`preferred_slot` establishes the pattern Phase 6 needs for a personalised `/v1/models`.** A conversation
now carries user-scoped display state that the API updates and the UI seeds from. §9.7's "private keys can
unlock extra model slots" is the same shape one level up — per-user rather than per-conversation — and the
picker already knows how to render a slot it did not expect (`ModelPicker.tsx:66`).

**The disclosure surface is complete and Phase 6 must not break it.** `served_by`, `substituted`,
`attempts`, `degraded`, `extraction_tier`, `messages_dropped`, `warning` — seven fields, all rendered, all
persisted. Phase 6 adds an eighth concern (which key pool served this) and the pattern for adding it is
now established three times over: a field on `MessageMeta`, the same field on both response shapes, one
line in `provenance.ts`, one element in `ModelIndicator`.

**Left deliberately unbuilt, seams visible:** `memory/summarize.py` (§2.2.7, still the seam);
`fitting.FitStrategy` (still one implemented member); `pin_target`'s tool-block branch (D3, reachable only
from a unit test); `conversation_summaries` (§2.2.7's table, still unwritten); message pagination (Phase 7
task 6, and `list_for_conversation` stays unpaginated when it lands).
